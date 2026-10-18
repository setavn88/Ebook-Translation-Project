"""Gọi Claude để phân tích chương và dịch từng đoạn theo thể loại + sổ tay truyện."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from .bible import Addressing, Entity, SectionNotes, StoryBible
from .cache import TranslationCache
from .chunker import split_html, split_text
from .consistency import contains
from .document import Section
from .genres import BUILTIN, GenreProfile

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CONTEXT_TAIL_CHARS = 1500
MIN_SPLIT_CHARS = 400
ANALYSIS_PART_CHARS = 100_000
CACHE_VERSION = "2"

_TRANSLATION_RE = re.compile(r"<translation>\n?(.*?)\n?</translation>", re.DOTALL)
_UPDATES_RE = re.compile(r"<bible_updates>(.*?)</bible_updates>", re.DOTALL)


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

_OUTPUT_RULES = """Định dạng trả lời (bắt buộc):
<translation>
...toàn bộ bản dịch...
</translation>
<bible_updates>
{"entities": [{"source": "tên gốc", "target": "bản dịch", "type": "loại", "note": "vai trò ngắn gọn"}],
 "addressing": [{"speaker": "tên gốc", "listener": "tên gốc", "self_term": "xưng", "other_term": "gọi", "context": "hoàn cảnh"}]}
</bible_updates>
Trong bible_updates chỉ ghi tên riêng/thuật ngữ MỚI chưa có trong sổ tay, và cách xưng hô mới xuất hiện hoặc vừa thay đổi
trong đoạn này (kèm lý do thay đổi trong context). Không có gì mới thì ghi {"entities": [], "addressing": []}."""


def build_rules(config: TranslatorConfig, genre: GenreProfile, kind: str) -> str:
    parts = [
        f"Bạn là một dịch giả văn học chuyên nghiệp, chuyên dịch thể loại {genre.name}. "
        f"Nhiệm vụ: dịch sách từ ngôn ngữ nguồn ({config.source_lang}) sang {config.target_lang}.",
        "",
        "Nguyên tắc chung:",
        "- Dịch đầy đủ, trung thành với ý nghĩa, giọng văn và cảm xúc; không tóm tắt, không bỏ sót câu, "
        "không thêm chú thích hay lời bình của người dịch.",
        f"- Câu văn tự nhiên như được viết bằng {config.target_lang} bởi người bản ngữ.",
        "- NHẤT QUÁN LÀ ƯU TIÊN CAO NHẤT: mọi tên riêng, thuật ngữ, cách xưng hô có trong SỔ TAY TRUYỆN phải dùng đúng "
        "như sổ tay ở mọi lần xuất hiện. Diễn biến phải khớp với phần <story_so_far> (ai còn sống, ai đã biết bí mật gì, "
        "quan hệ đang ở giai đoạn nào).",
        "- Tên mới chưa có trong sổ tay: đặt bản dịch theo quy tắc của thể loại và khai báo trong bible_updates.",
        "- Nếu một phần đã ở sẵn ngôn ngữ đích, giữ nguyên phần đó.",
        f"- {_FORMAT_RULES[kind]}",
        "- Các phần <story_so_far>, <current_chapter>, <names_in_passage>, <context> chỉ để tham khảo; không dịch lại chúng.",
        "",
        genre.prompt_block(),
    ]
    if config.style:
        parts += ["", "Yêu cầu riêng của người dùng (ưu tiên hơn quy tắc thể loại):", config.style]
    parts += ["", _OUTPUT_RULES]
    return "\n".join(parts)


def _analysis_schema(genre: GenreProfile) -> dict:
    def obj(props: dict) -> dict:
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}

    s = {"type": "string"}
    return obj(
        {
            "summary": s,
            "events": {"type": "array", "items": s},
            "entities": {
                "type": "array",
                "items": obj(
                    {
                        "source": s,
                        "target": s,
                        "type": {"type": "string", "enum": list(genre.entity_types)},
                        "aliases": {"type": "array", "items": s},
                        "gender": s,
                        "note": s,
                    }
                ),
            },
            "addressing": {
                "type": "array",
                "items": obj({"speaker": s, "listener": s, "self_term": s, "other_term": s, "context": s}),
            },
        }
    )


class Translator:
    def __init__(
        self,
        config: TranslatorConfig,
        genre: GenreProfile | None = None,
        bible: StoryBible | None = None,
        client: anthropic.Anthropic | None = None,
        cache: TranslationCache | None = None,
    ):
        self.config = config
        self.genre = genre or BUILTIN["general"]
        self.bible = bible if bible is not None else StoryBible()
        self.client = client or anthropic.Anthropic(max_retries=5)
        self.cache = cache if cache is not None else TranslationCache(None)
        self.usage = Usage()

    # ---- gọi API -------------------------------------------------------

    def _base_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.config.effort},
        }
        if self.config.use_fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _system(self, rules: str) -> list[dict]:
        # Quy tắc thể loại gần như cố định, sổ tay chỉ đổi khi có tên mới → cả hai được prompt-cache.
        return [
            {"type": "text", "text": rules},
            {
                "type": "text",
                "text": self.bible.prompt_block(self.genre.track_addressing),
                "cache_control": {"type": "ephemeral"},
            },
        ]

    @staticmethod
    def _check_refusal(message: Any, what: str) -> None:
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise TranslationError(f"Mô hình từ chối {what} (category={category}).")

    def _stream(self, system: list[dict], user: str) -> tuple[str, str]:
        kwargs = self._base_kwargs() | {
            "max_tokens": self.config.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        with self.client.beta.messages.stream(**kwargs) as stream:
            message = stream.get_final_message()
        self.usage.add(message.usage)
        self._check_refusal(message, "dịch đoạn này")
        return "".join(b.text for b in message.content if b.type == "text"), message.stop_reason

    # ---- phân tích chương (lập sổ tay) --------------------------------

    def analyze_section(self, section: Section) -> SectionNotes | None:
        """Đọc một chương, ghi nhân vật/tên riêng/xưng hô/sự kiện vào sổ tay. Kết quả được cache."""
        text = section.plain_text()
        if len(text) < 20:  # trang bìa, trang chỉ có ảnh
            return None
        parts = split_text(text, ANALYSIS_PART_CHARS) if len(text) > ANALYSIS_PART_CHARS else [text]
        results = [self._analyze_text(section.title, p) for p in parts]
        for r in results:
            for e in r["entities"]:
                self.bible.add_entity(
                    Entity(e["source"], e["target"], e["type"], e.get("aliases", []), e.get("gender", ""), e.get("note", ""))
                )
            for a in r["addressing"]:
                self.bible.add_addressing(Addressing(**a))
        notes = SectionNotes(
            title=section.title,
            summary=" ".join(r["summary"] for r in results).strip(),
            events=[ev for r in results for ev in r["events"]],
        )
        self.bible.set_section(section.id, notes)
        return notes

    def _analyze_text(self, title: str, text: str) -> dict:
        c, g = self.config, self.genre
        key = TranslationCache.make_key("analysis", CACHE_VERSION, c.model, g.key, c.target_lang, text)
        cached = self.cache.get(key)
        if cached is not None:
            return json.loads(cached)
        rules = "\n".join(
            [
                f"Bạn là biên tập viên chuẩn bị dịch một cuốn sách thể loại {g.name} sang {c.target_lang}. "
                "Đọc chương được cung cấp và lập ghi chú cho sổ tay truyện để bản dịch nhất quán.",
                "",
                f"Cần theo dõi: {g.analysis_focus}",
                "",
                "Yêu cầu:",
                f"- summary: tóm tắt diễn biến chương bằng {c.target_lang}, 3–8 câu, dùng tên đã dịch.",
                "- events: các sự kiện then chốt ảnh hưởng tới các chương sau (cái chết, bị thương, tiết lộ bí mật, "
                "đổi tên/danh hiệu, thay đổi quan hệ, ai biết điều gì). Mỗi sự kiện một câu ngắn.",
                "- entities: tên riêng và thuật ngữ đặc thù lặp lại (không lấy từ thông dụng). source là đúng chữ trong "
                "bản gốc; aliases là các cách gọi khác trong bản gốc; target theo quy tắc đặt tên dưới đây. "
                "Tên đã có trong sổ tay thì dùng lại đúng bản dịch đó.",
                "- gender: giới tính nếu là nhân vật (nam/nữ/không rõ), để trống nếu không phải.",
                "- addressing: cách từng cặp nhân vật xưng hô với nhau trong chương (dùng tên gốc cho speaker/listener), "
                "phù hợp tuổi tác, vai vế, mức thân mật và văn phong thể loại."
                if g.track_addressing
                else "- addressing: để mảng rỗng.",
                "",
                g.prompt_block(),
            ]
        )
        kwargs = self._base_kwargs() | {
            "max_tokens": 16000,
            "system": self._system(rules),
            "messages": [{"role": "user", "content": f"<chapter title=\"{title}\">\n{text}\n</chapter>"}],
        }
        kwargs["output_config"] = kwargs["output_config"] | {
            "format": {"type": "json_schema", "schema": _analysis_schema(g)}
        }
        message = self.client.beta.messages.create(**kwargs)
        self.usage.add(message.usage)
        self._check_refusal(message, "phân tích chương")
        raw = "".join(b.text for b in message.content if b.type == "text")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as e:
            raise TranslationError(f"Kết quả phân tích chương không hợp lệ (stop_reason={message.stop_reason}).") from e
        self.cache.put(key, json.dumps(result, ensure_ascii=False))
        return result

    # ---- dịch ----------------------------------------------------------

    def _cache_key(self, chunk: str, kind: str) -> str:
        c = self.config
        return TranslationCache.make_key(
            "translate", CACHE_VERSION, c.model, c.source_lang, c.target_lang, self.genre.key, c.style, kind, chunk
        )

    def _build_user_message(
        self,
        chunk: str,
        context: tuple[str, str] | None,
        section_id: str | None,
        order: list[str] | None,
        reminder: str | None,
    ) -> str:
        parts = []
        if section_id and order:
            so_far = self.bible.story_so_far(order, section_id)
            if so_far:
                parts.append(f"<story_so_far>\n{so_far}\n</story_so_far>")
            notes = self.bible.sections.get(section_id)
            if notes and notes.summary:
                parts.append(f"<current_chapter>\n{notes.title}: {notes.summary}\n</current_chapter>")
        names = self.bible.entities_in(chunk, contains)
        if names:
            lines = "\n".join(f"- {e.source} → {e.target}" for e in sorted(names, key=lambda e: e.source))
            parts.append(f"<names_in_passage>\nTên trong đoạn này và bản dịch bắt buộc:\n{lines}\n</names_in_passage>")
        if context and context[0]:
            src_tail, dst_tail = context
            parts.append(
                "<context>\n"
                f"<previous_source>\n{src_tail}\n</previous_source>\n"
                f"<previous_translation>\n{dst_tail}\n</previous_translation>\n"
                "</context>"
            )
        if reminder:
            parts.append(f"<reminder>\n{reminder}\n</reminder>")
        parts.append(f"Dịch đoạn sau:\n<source>\n{chunk}\n</source>")
        return "\n\n".join(parts)

    def _apply_updates(self, reply: str) -> None:
        match = _UPDATES_RE.search(reply)
        if not match:
            return
        try:
            updates = json.loads(match.group(1))
        except json.JSONDecodeError:
            log.warning("Bỏ qua bible_updates không phải JSON hợp lệ")
            return
        changed = False
        for e in updates.get("entities", []) if isinstance(updates, dict) else []:
            if isinstance(e, dict) and isinstance(e.get("source"), str) and isinstance(e.get("target"), str):
                changed |= self.bible.add_entity(
                    Entity(e["source"], e["target"], str(e.get("type") or "term"), note=str(e.get("note") or ""))
                )
        for a in updates.get("addressing", []) if isinstance(updates, dict) else []:
            if isinstance(a, dict):
                try:
                    changed |= self.bible.add_addressing(
                        Addressing(**{k: str(a.get(k) or "") for k in ("speaker", "listener", "self_term", "other_term", "context")})
                    )
                except TypeError:
                    continue
        if changed:
            self.bible.save()

    def translate_chunk(
        self,
        chunk: str,
        kind: str = "text",
        context: tuple[str, str] | None = None,
        section_id: str | None = None,
        order: list[str] | None = None,
        reminder: str | None = None,
    ) -> str:
        if not chunk.strip():
            return chunk
        key = self._cache_key(chunk, kind)
        if reminder is None:  # dịch lại để sửa lỗi thì bỏ qua cache
            cached = self.cache.get(key)
            if cached is not None:
                return cached

        system = self._system(build_rules(self.config, self.genre, kind))
        reply, stop_reason = self._stream(system, self._build_user_message(chunk, context, section_id, order, reminder))
        translation = _TRANSLATION_RE.search(reply)
        translation = translation.group(1) if translation else None

        if translation is None:
            if stop_reason != "max_tokens" and reply.strip() and len(chunk) < MIN_SPLIT_CHARS:
                translation = reply.strip()  # mô hình quên thẻ, lấy nguyên câu trả lời
            elif len(chunk) < MIN_SPLIT_CHARS:
                raise TranslationError(f"Không dịch được đoạn ngắn (stop_reason={stop_reason}).")
            else:
                # Bản dịch bị cắt hoặc sai định dạng: chia nhỏ chunk rồi dịch lại.
                log.warning("Chunk %d ký tự bị cắt/sai định dạng, chia đôi để dịch lại", len(chunk))
                splitter = split_html if kind == "html" else split_text
                pieces = splitter(chunk, max(len(chunk) // 2, MIN_SPLIT_CHARS))
                if len(pieces) < 2:
                    raise TranslationError("Không thể chia nhỏ đoạn để dịch lại.")
                out, ctx = [], context
                for piece in pieces:
                    t = self.translate_chunk(piece, kind, ctx, section_id, order, reminder)
                    out.append(t)
                    ctx = (piece[-CONTEXT_TAIL_CHARS:], t[-CONTEXT_TAIL_CHARS:])
                return ("" if kind == "html" else "\n\n").join(out)

        self._apply_updates(reply)
        self.cache.put(key, translation)
        return translation

    def translate_section(
        self,
        section: Section,
        order: list[str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[str]:
        """Dịch tuần tự các đoạn của một chương, mỗi đoạn nhận đuôi đoạn trước làm ngữ cảnh."""
        out: list[str] = []
        context: tuple[str, str] | None = None
        for i, chunk in enumerate(section.chunks):
            translated = self.translate_chunk(chunk, section.kind, context, section.id, order)
            out.append(translated)
            context = (chunk[-CONTEXT_TAIL_CHARS:], translated[-CONTEXT_TAIL_CHARS:])
            if on_progress:
                on_progress(i + 1, len(section.chunks))
        return out
