"""Kiểm tra nhất quán tên riêng giữa bản gốc và bản dịch, dựa trên sổ tay truyện."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .bible import Entity, StoryBible

_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


# Loại thực thể hay được thay bằng đại từ trong câu ("anh ta", "nó"...): không thấy tên đã dịch chưa chắc là lỗi.
PRONOUN_TYPES = {"character", "nickname", "ai", "alias"}


@lru_cache(maxsize=4096)
def _pattern(name: str, target: bool = False) -> re.Pattern:
    # Tên có chữ CJK không có khoảng trắng phân tách → so khớp chuỗi con.
    if _CJK_RE.search(name):
        return re.compile(re.escape(name))
    if target:
        # Bản dịch tiếng Việt: không phân biệt hoa thường ("Quỷ Đá" / "quỷ đá" vẫn là một tên).
        return re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
    # Bản gốc tiếng Anh: chấp nhận số nhiều / sở hữu (trolls, Rincewind's). Tên viết thường là danh từ chung của
    # thế giới truyện (troll, sea troll) → không phân biệt hoa thường để bắt cả "Troll" ở đầu câu.
    flags = re.IGNORECASE if name == name.lower() else 0
    return re.compile(r"(?<!\w)" + re.escape(name) + r"(?:'s|’s|s|es)?(?!\w)", flags)


def contains(text: str, name: str) -> bool:
    """Tên (trong bản gốc) xuất hiện như một từ độc lập; tên viết hoa thì phân biệt hoa thường."""
    return bool(name.strip()) and _pattern(name).search(text) is not None


def contains_target(text: str, name: str) -> bool:
    """Tên đã dịch xuất hiện trong bản dịch (không phân biệt hoa thường)."""
    return bool(name.strip()) and _pattern(name, True).search(text) is not None


@dataclass
class Issue:
    section_id: str
    chunk_index: int
    entity: Entity
    problem: str  # "untranslated": tên gốc còn sót | "missing": không thấy bản dịch của tên

    @property
    def fixable(self) -> bool:
        """Lỗi chắc chắn, nên tự sửa: còn sót tên gốc, hoặc địa danh/loài/chức danh... bị dịch thành biến thể khác.
        Nhân vật thì "không thấy tên" thường chỉ là câu dùng đại từ, nên chỉ báo cáo."""
        return self.problem == "untranslated" or self.entity.type not in PRONOUN_TYPES

    def describe(self) -> str:
        if self.problem == "untranslated":
            return f"còn sót tên gốc “{self.entity.source}” (phải là “{self.entity.target}”)"
        if self.fixable:
            return f"không thấy “{self.entity.target}” cho “{self.entity.source}” (có thể bị dịch thành tên khác)"
        return f"không thấy “{self.entity.target}” cho “{self.entity.source}” (có thể đã thay bằng đại từ)"


def check_chunk(bible: StoryBible, section_id: str, index: int, source: str, translation: str) -> list[Issue]:
    issues = []
    # Xét tên dài trước rồi che đi, để "troll" nằm trong "sea troll" không bị tính là một lần xuất hiện riêng.
    found = sorted(bible.entities_in(source, contains), key=lambda e: -max(len(n) for n in e.names()))
    masked = source
    present = []
    for entity in found:
        if any(contains(masked, n) for n in entity.names()):
            present.append(entity)
        for n in entity.names():
            masked = _pattern(n).sub(" ", masked)
    for entity in present:
        target = entity.target
        if target.casefold() != entity.source.casefold():
            # Bỏ các chỗ đã dịch đúng (bản dịch có thể chứa tên gốc, vd. "Trường Hogwarts").
            rest = _pattern(target, True).sub(" ", translation)
            if contains(rest, entity.source):
                issues.append(Issue(section_id, index, entity, "untranslated"))
                continue
        if not contains_target(translation, target):
            issues.append(Issue(section_id, index, entity, "missing"))
    return issues


def format_report(issues: list[Issue], titles: dict[str, str], fixed: int) -> str:
    lines = ["# Báo cáo nhất quán tên riêng", ""]
    untranslated = [i for i in issues if i.problem == "untranslated"]
    missing = [i for i in issues if i.problem == "missing"]
    variants = [i for i in missing if i.fixable]
    pronouns = [i for i in missing if not i.fixable]
    lines.append(f"- Đoạn đã tự soát và sửa tên: **{fixed}**")
    lines.append(f"- Còn sót tên gốc sau khi sửa: **{len(untranslated)}**")
    lines.append(f"- Địa danh/loài/chức danh còn lệch tên sau khi sửa: **{len(variants)}**")
    lines.append(f"- Nhân vật không thấy tên (thường do dùng đại từ, nên xem lại): **{len(pronouns)}**")
    for heading, group in (("Còn sót tên gốc", untranslated), ("Còn lệch tên", variants), ("Cần xem lại", pronouns)):
        if not group:
            continue
        lines += ["", f"## {heading}", ""]
        for issue in group:
            title = titles.get(issue.section_id, issue.section_id)
            lines.append(f"- {title}, đoạn {issue.chunk_index + 1}: {issue.describe()}")
    return "\n".join(lines) + "\n"
