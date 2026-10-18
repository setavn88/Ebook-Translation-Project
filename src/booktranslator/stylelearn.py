"""Học phong cách dịch từ một cuốn sách tham chiếu (vd. bản Harry Potter do Lý Lan dịch).

Đầu vào: bản dịch tiếng Việt (bắt buộc) và bản gốc tiếng Anh (khuyên dùng).
Chỉ đọc vài trích đoạn rải đều trong sách — không đọc cả cuốn — để tốn ít token:
  1. Có bản gốc: với mỗi trích đoạn, nhờ mô hình tìm các cặp đoạn gốc ↔ dịch tương ứng
     và các tên riêng được Việt hóa (vd. Diagon Alley → Hẻm Xéo).
  2. Tổng hợp: mô tả cụ thể văn phong, cách đặt tên, cách xưng hô, chọn đoạn mẫu.
Kết quả là một file hồ sơ phong cách JSON, dùng lại cho mọi lần dịch (--phong-cach-file).
File này chứa trích đoạn sách của bạn — chỉ để dùng cá nhân, đừng đăng công khai.
"""

from __future__ import annotations

import logging
from typing import Callable

from .chunker import split_text
from .document import Document
from .providers import ProviderChain
from .styles import StyleProfile
from .translator import TranslationError, parse_json

log = logging.getLogger(__name__)

MIN_SECTION_CHARS = 3000
EXAMPLES_BUDGET_CHARS = 3500  # đoạn mẫu nằm trong prompt hệ thống của mọi request → giữ gọn

_PAIRS_SCHEMA = {
    "type": "object",
    "properties": {
        "pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"source": {"type": "string"}, "target": {"type": "string"}, "why": {"type": "string"}},
                "required": ["source", "target", "why"],
                "additionalProperties": False,
            },
        },
        "names": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"source": {"type": "string"}, "target": {"type": "string"}},
                "required": ["source", "target"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["pairs", "names"],
    "additionalProperties": False,
}

_STYLE_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "principles": {"type": "string"},
        "naming": {"type": "string"},
        "addressing": {"type": "string"},
        "samples": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["description", "principles", "naming", "addressing", "samples"],
    "additionalProperties": False,
}


def _content_sections(doc: Document) -> list[str]:
    texts = [s.plain_text() for s in doc.sections]
    return [t for t in texts if len(t) >= MIN_SECTION_CHARS]


def _spread(n_total: int, n_pick: int) -> list[int]:
    """Chọn n_pick vị trí rải đều (tránh chương đầu thường là lời tựa)."""
    if n_total <= n_pick:
        return list(range(n_total))
    step = n_total / (n_pick + 1)
    return sorted({min(n_total - 1, round(step * (i + 1))) for i in range(n_pick)})


def _excerpt(text: str, chars: int) -> str:
    return split_text(text, chars)[0] if len(text) > chars else text


def learn_style(
    llm: ProviderChain,
    translated: Document,
    original: Document | None = None,
    name: str = "Phong cách tham chiếu",
    key: str = "reference",
    samples: int = 4,
    excerpt_chars: int = 6000,
    effort: str = "high",
    progress: Callable[[str, int, int], None] | None = None,
) -> StyleProfile:
    vi_sections = _content_sections(translated)
    if not vi_sections:
        raise ValueError("Sách tham chiếu không có chương nào đủ dài để học phong cách")
    en_sections = _content_sections(original) if original else []
    picks = _spread(len(vi_sections), samples)

    excerpts: list[str] = []
    pairs: list[dict] = []
    names: dict[str, str] = {}
    for n, idx in enumerate(picks, start=1):
        vi = _excerpt(vi_sections[idx], excerpt_chars)
        excerpts.append(vi)
        if en_sections:
            # Hai ấn bản thường cùng số chương; lấy chương ở cùng vị trí tương đối, rộng hơn một chút để bao trọn.
            en_idx = min(len(en_sections) - 1, round(idx * len(en_sections) / len(vi_sections)))
            en = _excerpt(en_sections[en_idx], int(excerpt_chars * 1.2))
            found = _align(llm, en, vi, effort)
            pairs += found.get("pairs", [])
            for item in found.get("names", []):
                if item.get("source") and item.get("target"):
                    names.setdefault(item["source"], item["target"])
        if progress:
            progress("Học phong cách", n, len(picks) + 1)

    result = _synthesize(llm, excerpts, pairs, names, effort)
    if progress:
        progress("Học phong cách", len(picks) + 1, len(picks) + 1)

    examples: list[tuple[str, str]] = []
    budget = EXAMPLES_BUDGET_CHARS
    candidates = [(p["source"], p["target"]) for p in pairs] or [("", s) for s in result.get("samples", [])]
    for src, dst in candidates:
        size = len(src) + len(dst)
        if size <= budget and dst.strip():
            examples.append((src, dst))
            budget -= size

    naming = result.get("naming", "").strip()
    if names:
        listed = "\n".join(f"  {s} → {t}" for s, t in list(names.items())[:25])
        naming += f"\n- Ví dụ cách Việt hóa tên trong sách tham chiếu (chỉ để học cách đặt tên, không dùng lại cho sách khác):\n{listed}"
    return StyleProfile(
        key=key,
        name=name,
        description=result.get("description", "").strip(),
        principles=result.get("principles", "").strip(),
        naming=naming,
        addressing=result.get("addressing", "").strip(),
        examples=tuple(examples),
    )


def _call_json(llm: ProviderChain, system: str, user: str, schema: dict, effort: str) -> dict:
    result = llm.complete(system, user, effort=effort, json_schema=schema, max_tokens=16000)
    if result.stop_reason == "refusal":
        raise TranslationError(f"Mô hình từ chối phân tích sách tham chiếu ({result.refusal_category}).")
    try:
        data = parse_json(result.text)
    except ValueError as e:
        raise TranslationError(f"Kết quả phân tích phong cách không hợp lệ ({result.provider}).") from e
    return data if isinstance(data, dict) else {}


def _align(llm: ProviderChain, en: str, vi: str, effort: str) -> dict:
    system = (
        "Bạn là nhà nghiên cứu dịch thuật. Cho một trích đoạn bản gốc tiếng Anh và một trích đoạn bản dịch tiếng Việt "
        "lấy từ cùng một chương (có thể lệch nhau một chút, có thể không trùng hết).\n"
        "- pairs: chọn tối đa 3 cặp đoạn tương ứng chắc chắn (mỗi đoạn 2–6 câu), CHÉP NGUYÊN VĂN từ hai trích đoạn. "
        "Ưu tiên đoạn thể hiện rõ tài dịch: lời thoại có giọng riêng, câu đùa/chơi chữ, thành ngữ được Việt hóa, câu "
        "được dịch thoát ý. why: một câu giải thích kỹ thuật dịch đáng học trong cặp đó.\n"
        "- names: các tên riêng/thuật ngữ được dịch hoặc Việt hóa (bỏ qua tên giữ nguyên).\n"
        "Không tìm được cặp chắc chắn thì trả mảng rỗng; tuyệt đối không bịa."
    )
    user = f"<original>\n{en}\n</original>\n\n<translation>\n{vi}\n</translation>"
    try:
        return _call_json(llm, system, user, _PAIRS_SCHEMA, effort)
    except TranslationError as e:
        log.warning("Bỏ qua một trích đoạn khi dóng hàng: %s", e)
        return {}


def _synthesize(llm: ProviderChain, excerpts: list[str], pairs: list[dict], names: dict[str, str], effort: str) -> dict:
    system = (
        "Bạn là nhà phê bình dịch thuật văn học. Hãy phân tích phong cách của dịch giả qua các trích đoạn bản dịch "
        "(và các cặp gốc–dịch nếu có), rồi viết hướng dẫn để một dịch giả khác tái tạo đúng phong cách đó khi dịch "
        "SÁCH KHÁC từ tiếng Anh sang tiếng Việt.\n"
        "- description: một câu mô tả phong cách.\n"
        "- principles: 8–14 gạch đầu dòng (bắt đầu bằng \"- \"), CỤ THỂ và có thể làm theo được: nhịp câu, độ dài câu, "
        "mức Việt hóa, cách xử lý lời thoại và tiểu từ tình thái, thành ngữ, chơi chữ, từ láy, từ Hán Việt hay thuần "
        "Việt, giọng kể, cách thể hiện giọng từng loại nhân vật. Mỗi ý kèm một ví dụ ngắn trích từ bản dịch.\n"
        "- naming: gạch đầu dòng về quy luật đặt/giữ tên riêng (khi nào giữ nguyên, khi nào dịch nghĩa, khi nào Việt "
        "hóa, độ dài, giọng điệu của tên).\n"
        "- addressing: gạch đầu dòng về quy luật xưng hô theo quan hệ (bạn bè, thầy trò, gia đình, kẻ thù, người lạ).\n"
        "- samples: 3–5 đoạn tiêu biểu nhất (mỗi đoạn 2–5 câu) CHÉP NGUYÊN VĂN từ các trích đoạn bản dịch.\n"
        "Chỉ nêu điều thực sự quan sát được trong văn bản; không nêu nhận xét chung chung."
    )
    parts = [f"<excerpt index=\"{i}\">\n{e}\n</excerpt>" for i, e in enumerate(excerpts, start=1)]
    if pairs:
        parts.append(
            "<aligned_pairs>\n"
            + "\n".join(f"- EN: {p.get('source', '')}\n  VI: {p.get('target', '')}\n  ({p.get('why', '')})" for p in pairs)
            + "\n</aligned_pairs>"
        )
    if names:
        parts.append("<names>\n" + "\n".join(f"- {s} → {t}" for s, t in names.items()) + "\n</names>")
    return _call_json(llm, system, "\n\n".join(parts), _STYLE_SCHEMA, effort)
