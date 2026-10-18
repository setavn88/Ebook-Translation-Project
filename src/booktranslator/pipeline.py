"""Quy trình dịch 3 bước: phân tích → dịch → kiểm tra nhất quán và sửa."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .consistency import Issue, check_chunk, contains, format_report
from .document import Document, html_to_text, write_document
from .translator import CONTEXT_TAIL_CHARS, Translator

log = logging.getLogger(__name__)

Progress = Callable[[str, str, int, int], None]  # (bước, tên chương, đã xong, tổng)


@dataclass
class RunResult:
    issues: list[Issue]
    fixed_chunks: int
    report: str


def analyze_document(translator: Translator, doc: Document, workers: int = 4, progress: Progress | None = None) -> None:
    """Lập sổ tay cho các chương chưa được phân tích.

    Các chương được phân tích song song, nhưng kết quả được gộp vào sổ tay theo
    thứ tự đọc, nên khi hai chương đặt tên khác nhau cho cùng một nhân vật thì
    chương xuất hiện trước luôn thắng — kết quả không phụ thuộc thứ tự chạy.
    """
    todo = [s for s in doc.sections if s.id not in translator.bible.sections]
    if not todo:
        return
    bible = translator.bible
    staging: dict[str, Translator] = {}

    def work(section):
        # Mỗi chương ghi vào một sổ tay tạm; gộp sau theo thứ tự đọc.
        t = translator.fork(bible.snapshot())
        t.analyze_section(section)
        staging[section.id] = t
        if progress:
            progress("Phân tích", section.title, len(staging), len(todo))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(work, todo))

    for section in todo:
        tmp = staging[section.id].bible
        for e in tmp.entities.values():
            bible.add_entity(e)
        for a in tmp.addressing:
            bible.add_addressing(a)
        if section.id in tmp.sections:
            bible.set_section(section.id, tmp.sections[section.id])
    bible.save()


def translate_document(
    translator: Translator,
    doc: Document,
    output: Path,
    workers: int = 4,
    lang_code: str | None = None,
    fix: bool = True,
    progress: Progress | None = None,
) -> RunResult:
    order = doc.order

    def work(section):
        cb = (lambda d, t: progress("Dịch", section.title, d, t)) if progress else None
        return section.id, translator.translate_section(section, order, cb)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        translations: dict[str, list[str]] = dict(pool.map(work, doc.sections))

    issues, fixed = _check_and_fix(translator, doc, translations, fix, progress)
    write_document(doc, {s.id: s.sep.join(translations[s.id]) for s in doc.sections}, output, lang_code)
    report = format_report(issues, {s.id: s.title for s in doc.sections}, fixed)
    return RunResult(issues=issues, fixed_chunks=fixed, report=report)


def _check_and_fix(translator: Translator, doc: Document, translations, fix: bool, progress):
    def check_all():
        found = []
        for s in doc.sections:
            for i, (src, dst) in enumerate(zip(s.chunks, translations[s.id])):
                found += check_chunk(translator.bible, s.id, i, src, dst)
        return found

    issues = check_all()
    if not fix:
        return issues, 0

    # Chỉ tự sửa lỗi chắc chắn (tên gốc còn sót); lỗi "missing" thường là do dùng đại từ.
    to_fix: dict[tuple[str, int], list[Issue]] = {}
    for issue in issues:
        if issue.problem == "untranslated":
            to_fix.setdefault((issue.section_id, issue.chunk_index), []).append(issue)
    sections = {s.id: s for s in doc.sections}
    for n, ((sid, idx), group) in enumerate(to_fix.items(), start=1):
        section = sections[sid]
        names = "\n".join(f"- {i.entity.source} → {i.entity.target}" for i in group)
        reminder = (
            "Bản dịch trước của đoạn này để sót tên gốc chưa dịch. Lần này bắt buộc dùng đúng các bản dịch sau "
            f"ở mọi chỗ xuất hiện:\n{names}"
        )
        context = None
        if idx > 0:
            context = (section.chunks[idx - 1][-CONTEXT_TAIL_CHARS:], translations[sid][idx - 1][-CONTEXT_TAIL_CHARS:])
        translations[sid][idx] = translator.translate_chunk(
            section.chunks[idx], section.kind, context, sid, doc.order, reminder=reminder
        )
        if progress:
            progress("Sửa nhất quán", section.title, n, len(to_fix))
    return (check_all() if to_fix else issues), len(to_fix)


def preview_document(translator: Translator, doc: Document, section_number: int | None = None, chunks: int = 2) -> str:
    """Dịch thử vài đoạn đầu của một chương; trả về Markdown so sánh gốc / nháp / biên tập.

    Chỉ phân tích đúng chương được dịch thử nên rẻ; bản nháp được cache nên lần
    `translate` sau với cùng tùy chọn sẽ không phải trả tiền dịch lại các đoạn này.
    """
    if not doc.sections:
        raise ValueError("Sách không có nội dung để dịch thử")
    if section_number:
        if not 1 <= section_number <= len(doc.sections):
            raise ValueError(f"--section phải trong khoảng 1..{len(doc.sections)}")
        section = doc.sections[section_number - 1]
    else:
        section = next((s for s in doc.sections if len(s.plain_text()) >= 1000), doc.sections[0])
    if section.id not in translator.bible.sections:
        translator.analyze_section(section)

    show = html_to_text if section.kind == "html" else str.strip
    g, v = translator.genre, translator.style
    lines = [
        f"# Dịch thử: {section.title}",
        "",
        f"- Thể loại: {g.name}",
        f"- Phong cách: {v.name}",
        f"- Model: {translator.llm.active.id} · effort: {translator.config.effort}",
        "",
    ]
    names = translator.bible.entities_in(section.plain_text(), contains)
    if names:
        lines += ["## Tên riêng trong chương (từ sổ tay — sửa trong file .bible.json nếu chưa ưng)", ""]
        lines += [f"- {e.source} → **{e.target}** ({e.type})" for e in sorted(names, key=lambda e: e.source)]
        lines.append("")

    context = None
    for i, chunk in enumerate(section.chunks[:chunks], start=1):
        draft = translator.draft_chunk(chunk, section.kind, context, section.id, doc.order)
        final = translator.polish_chunk(chunk, draft, section.kind, context, section.id, doc.order) if translator.config.polish else draft
        context = (chunk[-CONTEXT_TAIL_CHARS:], final[-CONTEXT_TAIL_CHARS:])
        lines += [f"## Đoạn {i}", "", "### Bản gốc", "", show(chunk), "", "### Bản nháp", "", show(draft), ""]
        if translator.config.polish:
            lines += ["### Bản biên tập (bản cuối)", "", show(final), ""]
    return "\n".join(lines)
