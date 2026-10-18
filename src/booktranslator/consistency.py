"""Kiểm tra nhất quán tên riêng giữa bản gốc và bản dịch, dựa trên sổ tay truyện."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .bible import Entity, StoryBible

_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


@lru_cache(maxsize=4096)
def _pattern(name: str) -> re.Pattern:
    # Tên có chữ CJK không có khoảng trắng phân tách → so khớp chuỗi con.
    if _CJK_RE.search(name):
        return re.compile(re.escape(name))
    return re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)")


def contains(text: str, name: str) -> bool:
    """Tên xuất hiện như một từ độc lập (phân biệt hoa thường, vì tên riêng viết hoa)."""
    return bool(name.strip()) and _pattern(name).search(text) is not None


@dataclass
class Issue:
    section_id: str
    chunk_index: int
    entity: Entity
    problem: str  # "untranslated": tên gốc còn sót | "missing": không thấy bản dịch của tên

    def describe(self) -> str:
        if self.problem == "untranslated":
            return f"còn sót tên gốc “{self.entity.source}” (phải là “{self.entity.target}”)"
        return f"không thấy “{self.entity.target}” cho “{self.entity.source}” (có thể đã thay bằng đại từ)"


def check_chunk(bible: StoryBible, section_id: str, index: int, source: str, translation: str) -> list[Issue]:
    issues = []
    for entity in bible.entities_in(source, contains):
        target = entity.target
        if target != entity.source:
            # Bỏ các chỗ đã dịch đúng (bản dịch có thể chứa tên gốc, vd. "Trường Hogwarts").
            rest = _pattern(target).sub(" ", translation)
            if contains(rest, entity.source):
                issues.append(Issue(section_id, index, entity, "untranslated"))
                continue
        if not contains(translation, target):
            issues.append(Issue(section_id, index, entity, "missing"))
    return issues


def format_report(issues: list[Issue], titles: dict[str, str], fixed: int) -> str:
    lines = ["# Báo cáo nhất quán tên riêng", ""]
    untranslated = [i for i in issues if i.problem == "untranslated"]
    missing = [i for i in issues if i.problem == "missing"]
    lines.append(f"- Đoạn đã tự dịch lại để sửa tên: **{fixed}**")
    lines.append(f"- Còn sót tên gốc sau khi sửa: **{len(untranslated)}**")
    lines.append(f"- Không thấy bản dịch của tên (thường do dùng đại từ, nên xem lại): **{len(missing)}**")
    for heading, group in (("Còn sót tên gốc", untranslated), ("Cần xem lại", missing)):
        if not group:
            continue
        lines += ["", f"## {heading}", ""]
        for issue in group:
            title = titles.get(issue.section_id, issue.section_id)
            lines.append(f"- {title}, đoạn {issue.chunk_index + 1}: {issue.describe()}")
    return "\n".join(lines) + "\n"
