"""Học từ bản sửa: so bản dịch trước/sau khi người dùng (hoặc AI khác) hiệu đính, rút ra bài học.

- Tên riêng/thuật ngữ bị đổi cách dịch → cập nhật thẳng vào sổ tay (quyết định của người dùng thắng).
- Bài học về câu chữ + vài ví dụ tiêu biểu → nối vào cuối file hướng dẫn hiệu đính.
Chỉ tốn một lần gọi AI: phần so sánh và đếm các chỗ thay lặp lại làm bằng code.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .bible import Entity, StoryBible
from .consistency import contains_target
from .document import Document
from .postedit import align_sources, load_edits, paragraphs_of
from .providers import ProviderChain
from .translator import TranslationError, parse_json

PROMPT_CHARS = 16000
CONTEXT_WORDS = 6
MAX_EXAMPLES = 6
MAX_LESSONS = 8


@dataclass
class Change:
    before: str
    after: str
    source: str = ""
    accepted: bool = True  # False = AI đề xuất nhưng người dùng bỏ (bài học ngược)


@dataclass
class Lessons:
    names: list[dict] = field(default_factory=list)  # {"source", "old", "new"}
    lessons: list[str] = field(default_factory=list)
    examples: list[dict] = field(default_factory=list)  # {"before", "after", "note"}


def _words(text: str) -> list[str]:
    return text.split()


def diff_snippets(before: str, after: str, context: int = CONTEXT_WORDS) -> list[tuple[str, str]]:
    """Các vùng khác nhau giữa hai đoạn, kèm vài chữ xung quanh (gửi đi gọn hơn cả đoạn)."""
    a, b = _words(before), _words(after)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    groups = sm.get_grouped_opcodes(context)
    out = []
    for group in groups:
        i1, i2 = group[0][1], group[-1][2]
        j1, j2 = group[0][3], group[-1][4]
        out.append((" ".join(a[i1:i2]), " ".join(b[j1:j2])))
    return out


def changes_from_docs(raw: Document, edited: Document, source: Document | None = None) -> list[Change]:
    """So từng đoạn của bản thô và bản đã sửa (ghép theo thứ tự đoạn, hoặc theo từng chương nếu lệch số đoạn)."""
    raw_p = paragraphs_of(raw)
    edited_text = align_sources(raw_p, paragraphs_of(edited))
    src_text = align_sources(raw_p, paragraphs_of(source)) if source else [""] * len(raw_p)
    out = []
    for p, after, src in zip(raw_p, edited_text, src_text):
        before = p.text
        if after and " ".join(before.split()) != " ".join(after.split()):
            out.append(Change(before, after, src))
    return out


def changes_from_edits_file(path: Path) -> list[Change]:
    """Chỗ sửa đã duyệt: nhận = bài học thuận, bỏ = bài học ngược."""
    _, edits = load_edits(path)
    return [Change(e.old, e.new, e.source, e.accepted) for e in edits if e.status != "failed" or not e.accepted]


def repeated_substitutions(changes: list[Change], min_count: int = 2, max_words: int = 4) -> list[tuple[str, str, int]]:
    """Các cụm ngắn bị thay giống nhau nhiều lần — thường là tên riêng / thuật ngữ được thống nhất lại."""
    counter: Counter = Counter()
    for c in changes:
        if not c.accepted:
            continue
        a, b = _words(c.before), _words(c.after)
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
            if tag == "replace" and i2 - i1 <= max_words and j2 - j1 <= max_words:
                old = " ".join(a[i1:i2]).strip(".,;:!?…\"“”'‘’()")
                new = " ".join(b[j1:j2]).strip(".,;:!?…\"“”'‘’()")
                if old and new and old.casefold() != new.casefold():
                    counter[(old, new)] += 1
    return [(o, n, k) for (o, n), k in counter.most_common(40) if k >= min_count]


def _lessons_schema() -> dict:
    def obj(props: dict) -> dict:
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}

    s = {"type": "string"}
    return obj(
        {
            "names": {"type": "array", "items": obj({"source": s, "old": s, "new": s})},
            "lessons": {"type": "array", "items": s},
            "examples": {"type": "array", "items": obj({"before": s, "after": s, "note": s})},
        }
    )


_RULES = f"""Bạn là biên tập viên trưởng, đang rút kinh nghiệm từ các chỗ người dùng đã sửa trên một bản dịch.
Mục tiêu: lần dịch/hiệu đính sau, AI tự làm đúng ý người dùng mà không cần sửa tay nữa.

Bạn nhận:
- <thay_lap_lai>: các cụm bị thay giống nhau nhiều lần (đếm bằng máy), thường là tên riêng/thuật ngữ được thống nhất.
- <so_tay>: tên riêng hiện có trong sổ tay (tên gốc → bản dịch).
- <da_sua>: các chỗ sửa người dùng CHẤP NHẬN (TRƯỚC → SAU, có thể kèm GỐC tiếng Anh).
- <bi_bo>: các chỗ AI đề xuất nhưng người dùng BỎ (không muốn sửa như vậy) — rút bài học ngược.

Trả về JSON:
- "names": tên riêng/loài/địa danh/thuật ngữ mà người dùng đổi cách dịch một cách nhất quán. "old" = cách dịch cũ,
  "new" = cách dịch người dùng muốn, "source" = tên gốc tiếng Anh nếu biết (lấy từ <so_tay> hoặc GỐC), không thì "".
  Chỉ ghi khi chắc chắn là tên/thuật ngữ, không ghi từ thường.
- "lessons": tối đa {MAX_LESSONS} bài học cụ thể, viết thành quy tắc ngắn có ví dụ trong ngoặc
  (vd. "Không dịch sát 'second-hand' thành 'đã qua sử dụng'; dùng so sánh dân dã ('cũ như đồ sang tay')").
  Không ghi bài học chung chung kiểu "dịch tự nhiên hơn". Không lặp lại tên đã ghi trong "names".
- "examples": tối đa {MAX_EXAMPLES} cặp TRƯỚC/SAU tiêu biểu nhất (ngắn, chép từ dữ liệu), "note" là điều rút ra.
Không có gì đáng học thì trả về mảng rỗng."""


def _render(changes: list[Change], budget: int) -> str:
    """Các chỗ sửa dạng gọn, rải đều cả cuốn cho vừa ngân sách ký tự."""
    lines: list[str] = []
    snippets = []
    for c in changes:
        for before, after in diff_snippets(c.before, c.after) if len(c.before) > 300 else [(c.before, c.after)]:
            snippets.append((before, after, c.source))
    if not snippets:
        return ""
    step = max(1, len(snippets) * 400 // max(budget, 1))
    used = 0
    for before, after, src in snippets[::step]:
        item = (f"- GỐC: {src[:300]}\n  " if src else "- ") + f"TRƯỚC: {before}\n  SAU: {after}"
        if used + len(item) > budget:
            break
        lines.append(item)
        used += len(item)
    return "\n".join(lines)


def learn_from_changes(llm: ProviderChain, changes: list[Change], bible: StoryBible, effort: str = "medium") -> Lessons:
    if not changes:
        return Lessons()
    accepted = [c for c in changes if c.accepted]
    rejected = [c for c in changes if not c.accepted]
    subs = repeated_substitutions(changes)
    text = " ".join(c.before for c in accepted)
    known = [e for e in bible.entities.values() if contains_target(text, e.target) or any(o == e.target for o, _, _ in subs)]
    parts = []
    if subs:
        parts.append("<thay_lap_lai>\n" + "\n".join(f"- \"{o}\" → \"{n}\" ({k} lần)" for o, n, k in subs) + "\n</thay_lap_lai>")
    if known:
        parts.append("<so_tay>\n" + "\n".join(f"- {e.source} → {e.target}" for e in known[:200]) + "\n</so_tay>")
    budget = PROMPT_CHARS if not rejected else PROMPT_CHARS * 3 // 4
    parts.append("<da_sua>\n" + _render(accepted, budget) + "\n</da_sua>")
    if rejected:
        parts.append("<bi_bo>\n" + _render(rejected, PROMPT_CHARS // 4) + "\n</bi_bo>")
    result = llm.complete(_RULES, "\n\n".join(parts), effort=effort, json_schema=_lessons_schema(), max_tokens=8000)
    if result.stop_reason == "refusal":
        raise TranslationError(f"Mô hình từ chối rút bài học ({result.provider}).")
    try:
        data = parse_json(result.text)
    except ValueError as e:
        raise TranslationError(f"Kết quả rút bài học không hợp lệ ({result.provider}).") from e
    if not isinstance(data, dict):
        return Lessons()

    def items(key, fields):
        return [{f: str(x.get(f) or "").strip() for f in fields} for x in data.get(key) or [] if isinstance(x, dict)]

    names = [n for n in items("names", ("source", "old", "new")) if n["new"] and (n["old"] or n["source"])]
    lessons = [str(x).strip() for x in data.get("lessons") or [] if str(x).strip()][:MAX_LESSONS]
    examples = [x for x in items("examples", ("before", "after", "note")) if x["before"] and x["after"]][:MAX_EXAMPLES]
    return Lessons(names, lessons, examples)


def _split(value: str) -> list[str]:
    """AI đôi khi gộp nhiều tên vào một ô: "sea troll / water troll"."""
    return [x.strip() for x in re.split(r"\s*/\s*|\s*;\s*", value or "") if x.strip()]


def update_bible(bible: StoryBible, names: list[dict]) -> list[str]:
    """Áp các tên người dùng đã thống nhất vào sổ tay. Trả về mô tả từng thay đổi."""
    report = []
    for n in names:
        new = n["new"]
        sources, olds = _split(n.get("source", "")), _split(n.get("old", ""))
        targets = []
        for name in sources:
            if (e := bible.find(name)) and e not in targets:
                targets.append(e)
        for old in olds if not targets else []:
            targets += [e for e in bible.find_by_target(old) if e not in targets]
        if targets:
            for e in targets:
                before = e.target
                if bible.set_target(e.source, new) == "updated":
                    report.append(f"{e.source}: “{before}” → “{new}”")
        elif sources:
            first, *rest = sources
            if bible.add_entity(Entity(first, new, "term", rest)):
                report.append(f"{', '.join(sources)}: thêm “{new}”")
        else:
            report.append(f"(chưa rõ tên gốc, chưa ghi vào sổ tay) “{n.get('old', '')}” → “{new}”")
    return report


def append_guide(path: Path, lessons: Lessons, origin: str) -> int:
    """Nối bài học vào file hướng dẫn hiệu đính (tạo file nếu chưa có). Trả về số dòng mới."""
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Hướng dẫn hiệu đính\n"
    seen = {line.strip() for line in existing.splitlines()}
    lines = []
    for lesson in lessons.lessons:
        line = f"- {lesson}"
        if line not in seen:
            lines.append(line)
    names = [f"- {n['old'] or n['source']} → {n['new']}" for n in lessons.names]
    names = [x for x in names if x not in seen]
    examples = [f"- “{x['before']}” → “{x['after']}”" + (f" — {x['note']}" if x["note"] else "") for x in lessons.examples]
    examples = [x for x in examples if x not in seen]
    if not (lines or names or examples):
        return 0
    block = ["", f"## Bài học tự rút ra — {time.strftime('%Y-%m-%d')} (từ {origin})", *lines]
    if names:
        block += ["", "Tên/thuật ngữ thống nhất:", *names]
    if examples:
        block += ["", "Ví dụ:", *examples]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing.rstrip("\n") + "\n" + "\n".join(block) + "\n", encoding="utf-8")
    return len(lines) + len(names) + len(examples)


def summarize(lessons: Lessons, bible_report: list[str]) -> str:
    out = [f"Rút ra {len(lessons.lessons)} bài học, {len(lessons.examples)} ví dụ, {len(lessons.names)} tên thống nhất."]
    out += [f"  sổ tay: {r}" for r in bible_report]
    out += [f"  bài học: {x}" for x in lessons.lessons]
    return "\n".join(out)


def dumps(lessons: Lessons) -> str:
    return json.dumps(lessons.__dict__, ensure_ascii=False, indent=1)


