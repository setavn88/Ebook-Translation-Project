"""Chia văn bản thành các đoạn (chunk) vừa với một lần gọi mô hình.

Ranh giới luôn rơi vào giữa các đoạn văn (dòng trống), hoặc với HTML là sau
thẻ đóng của một khối, để không cắt ngang câu hay thẻ.
"""

from __future__ import annotations

import re

DEFAULT_CHUNK_CHARS = 6000

_BLOCK_END = re.compile(
    r"(</(?:p|h[1-6]|div|li|ul|ol|blockquote|pre|table|tr|section|figure|figcaption)>|<br\s*/?>|<hr\s*/?>)",
    re.IGNORECASE,
)


def _pack(pieces: list[str], max_chars: int, sep: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for piece in pieces:
        if current and size + len(piece) + len(sep) > max_chars:
            chunks.append(sep.join(current))
            current, size = [], 0
        current.append(piece)
        size += len(piece) + len(sep)
    if current:
        chunks.append(sep.join(current))
    return chunks


def _split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    """Đoạn văn quá dài thì tách theo câu."""
    if len(paragraph) <= max_chars:
        return [paragraph]
    sentences = re.split(r"(?<=[.!?。！？…])\s+", paragraph)
    return _pack(sentences, max_chars, " ")


def split_text(text: str, max_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """Chia văn bản thuần/Markdown theo đoạn văn, ghép lại đến tối đa max_chars."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for paragraph in paragraphs:
        pieces.extend(_split_long_paragraph(paragraph.strip("\n"), max_chars))
    return _pack(pieces, max_chars, "\n\n")


def split_html(html: str, max_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """Chia nội dung HTML ngay sau thẻ đóng khối. Ghép các chunk lại ra đúng chuỗi gốc."""
    parts = _BLOCK_END.split(html)
    # re.split giữ nhóm bắt: [văn bản, thẻ đóng, văn bản, thẻ đóng, ...]
    blocks: list[str] = []
    for i in range(0, len(parts), 2):
        block = parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
        if block:
            blocks.append(block)
    return _pack(blocks, max_chars, "")
