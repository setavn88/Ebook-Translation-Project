"""Gọi Claude để dịch từng chunk, giữ mạch văn và thuật ngữ thống nhất."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic

from .cache import TranslationCache
from .chunker import split_html, split_text

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CONTEXT_TAIL_CHARS = 1500
MIN_SPLIT_CHARS = 400

ChunkKind = Literal["text", "markdown", "html"]

_TRANSLATION_RE = re.compile(r"<translation>\n?(.*?)\n?</translation>", re.DOTALL)


class TranslationError(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    requests: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, usage: Any) -> None:
        with self._lock:
            self._add(usage)

    def _add(self, usage: Any) -> None:
        self.requests += 1
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.cache_read_input_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_creation_input_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0


@dataclass
class TranslatorConfig:
    target_lang: str = "Tiếng Việt"
    source_lang: str = "tự nhận diện"
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    style: str = ""
    glossary: dict[str, str] = field(default_factory=dict)
    use_fallback: bool = True
    max_tokens: int = 64000


_FORMAT_RULES = {
    "text": "Văn bản là chữ thuần. Giữ nguyên cách ngắt đoạn và xuống dòng.",
    "markdown": (
        "Văn bản là Markdown. Giữ nguyên toàn bộ cú pháp Markdown (tiêu đề #, danh sách, "
        "in đậm/nghiêng, liên kết, khối code). Không dịch nội dung trong khối code hay URL."
    ),
    "html": (
        "Văn bản là một đoạn XHTML trích từ sách EPUB. Chỉ dịch phần chữ hiển thị và các "
        "thuộc tính alt/title. Giữ nguyên chính xác mọi thẻ, thuộc tính, id, class, href, "
        "thực thể HTML và thứ tự thẻ; không thêm, bớt hay đóng/mở thẻ nào khác với bản gốc."
    ),
}


def build_system_prompt(config: TranslatorConfig, kind: ChunkKind) -> str:
    parts = [
        f"Bạn là một dịch giả văn học chuyên nghiệp. Nhiệm vụ: dịch sách từ ngôn ngữ nguồn "
        f"({config.source_lang}) sang {config.target_lang}.",
        "",
        "Nguyên tắc:",
        "- Dịch đầy đủ, trung thành với ý nghĩa, giọng văn và cảm xúc của tác giả; không tóm tắt, "
        "không bỏ sót câu nào, không thêm chú thích hay lời bình của người dịch.",
        f"- Câu văn phải tự nhiên, trôi chảy như được viết bằng {config.target_lang} bởi người bản ngữ.",
        "- Giữ thống nhất tên riêng, xưng hô và thuật ngữ trong suốt cuốn sách.",
        "- Nếu một phần đã ở sẵn ngôn ngữ đích, giữ nguyên phần đó.",
        f"- {_FORMAT_RULES[kind]}",
        "- Phần <context> (nếu có) chỉ để tham khảo mạch truyện và cách xưng hô; không dịch lại nó.",
        "- Trả lời duy nhất bằng bản dịch đặt trong cặp thẻ <translation></translation>.",
    ]
    if config.style:
        parts += ["", "Yêu cầu văn phong của người dùng:", config.style]
    if config.glossary:
        # sort để prompt ổn định byte-by-byte, giữ được prompt cache giữa các chunk
        lines = [f"- {src} → {dst}" for src, dst in sorted(config.glossary.items())]
        parts += ["", "Bảng thuật ngữ bắt buộc dùng:", *lines]
    return "\n".join(parts)


class Translator:
    def __init__(
        self,
        config: TranslatorConfig,
        client: anthropic.Anthropic | None = None,
        cache: TranslationCache | None = None,
    ):
        self.config = config
        self.client = client or anthropic.Anthropic(max_retries=5)
        self.cache = cache if cache is not None else TranslationCache(None)
        self.usage = Usage()

    # ---- gọi API -------------------------------------------------------

    def _request_kwargs(self, system: str, messages: list[dict]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.config.effort},
            "system": system,
            "cache_control": {"type": "ephemeral"},
            "messages": messages,
        }
        if self.config.use_fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _call(self, system: str, user: str) -> tuple[str, str]:
        """Trả về (văn bản trả lời, stop_reason)."""
        kwargs = self._request_kwargs(system, [{"role": "user", "content": user}])
        with self.client.beta.messages.stream(**kwargs) as stream:
            message = stream.get_final_message()
        self.usage.add(message.usage)
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise TranslationError(f"Mô hình từ chối dịch đoạn này (category={category}).")
        text = "".join(b.text for b in message.content if b.type == "text")
        return text, message.stop_reason

    # ---- dịch ----------------------------------------------------------

    @staticmethod
    def _extract(reply: str) -> str | None:
        match = _TRANSLATION_RE.search(reply)
        return match.group(1) if match else None

    def _build_user_message(self, chunk: str, context: tuple[str, str] | None) -> str:
        parts = []
        if context and context[0]:
            src_tail, dst_tail = context
            parts.append(
                "<context>\n"
                f"<previous_source>\n{src_tail}\n</previous_source>\n"
                f"<previous_translation>\n{dst_tail}\n</previous_translation>\n"
                "</context>\n"
            )
        parts.append(f"Dịch đoạn sau:\n<source>\n{chunk}\n</source>")
        return "\n".join(parts)

    def _cache_key(self, chunk: str, kind: ChunkKind) -> str:
        c = self.config
        glossary = json.dumps(c.glossary, sort_keys=True, ensure_ascii=False)
        return TranslationCache.make_key(c.model, c.source_lang, c.target_lang, c.style, glossary, kind, chunk)

    def translate_chunk(
        self, chunk: str, kind: ChunkKind = "text", context: tuple[str, str] | None = None
    ) -> str:
        if not chunk.strip():
            return chunk
        key = self._cache_key(chunk, kind)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        system = build_system_prompt(self.config, kind)
        reply, stop_reason = self._call(system, self._build_user_message(chunk, context))
        translation = self._extract(reply)

        if stop_reason == "max_tokens" or translation is None:
            # Bản dịch bị cắt hoặc sai định dạng: chia nhỏ chunk rồi dịch lại.
            if len(chunk) < MIN_SPLIT_CHARS:
                if translation is None and stop_reason != "max_tokens" and reply.strip():
                    translation = reply.strip()  # mô hình quên thẻ, lấy nguyên câu trả lời
                else:
                    raise TranslationError(f"Không dịch được đoạn ngắn (stop_reason={stop_reason}).")
            else:
                log.warning("Chunk %d ký tự bị cắt/sai định dạng, chia đôi để dịch lại", len(chunk))
                splitter = split_html if kind == "html" else split_text
                pieces = splitter(chunk, max(len(chunk) // 2, MIN_SPLIT_CHARS))
                if len(pieces) < 2:
                    raise TranslationError("Không thể chia nhỏ đoạn để dịch lại.")
                return self.translate_chunks(pieces, kind, context=context, sep="" if kind == "html" else "\n\n")

        self.cache.put(key, translation)
        return translation

    def translate_chunks(
        self,
        chunks: list[str],
        kind: ChunkKind = "text",
        context: tuple[str, str] | None = None,
        sep: str = "\n\n",
        on_progress=None,
    ) -> str:
        """Dịch tuần tự, mỗi chunk nhận đuôi của chunk trước làm ngữ cảnh."""
        out: list[str] = []
        for i, chunk in enumerate(chunks):
            translated = self.translate_chunk(chunk, kind, context)
            out.append(translated)
            context = (chunk[-CONTEXT_TAIL_CHARS:], translated[-CONTEXT_TAIL_CHARS:])
            if on_progress:
                on_progress(i + 1, len(chunks))
        return sep.join(out)

    # ---- bảng thuật ngữ -----------------------------------------------

    def build_glossary(self, sample: str, max_terms: int = 80) -> dict[str, str]:
        """Trích tên riêng/thuật ngữ từ một mẫu của sách và đề xuất cách dịch."""
        schema = {
            "type": "object",
            "properties": {
                "terms": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "target": {"type": "string"},
                        },
                        "required": ["source", "target"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["terms"],
            "additionalProperties": False,
        }
        system = (
            f"Bạn là biên tập viên chuẩn bị dịch một cuốn sách sang {self.config.target_lang}. "
            f"Từ đoạn trích, liệt kê tối đa {max_terms} tên nhân vật, địa danh, tổ chức và thuật ngữ "
            "đặc thù xuất hiện lặp lại, kèm cách dịch/phiên âm nên dùng thống nhất. "
            "Bỏ qua từ thông dụng. Nếu tên nên giữ nguyên thì target giống source."
        )
        kwargs = self._request_kwargs(system, [{"role": "user", "content": f"<excerpt>\n{sample}\n</excerpt>"}])
        kwargs["output_config"] = {"effort": self.config.effort, "format": {"type": "json_schema", "schema": schema}}
        kwargs["max_tokens"] = 16000
        del kwargs["cache_control"]
        message = self.client.beta.messages.create(**kwargs)
        self.usage.add(message.usage)
        if message.stop_reason == "refusal":
            raise TranslationError("Mô hình từ chối tạo bảng thuật ngữ.")
        text = "".join(b.text for b in message.content if b.type == "text")
        try:
            terms = json.loads(text)["terms"]
        except (json.JSONDecodeError, KeyError) as e:
            raise TranslationError(f"Kết quả bảng thuật ngữ không hợp lệ (stop_reason={message.stop_reason}).") from e
        return {t["source"]: t["target"] for t in terms if t["source"].strip()}
