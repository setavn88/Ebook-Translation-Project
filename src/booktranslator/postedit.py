"""Hiệu đính bản dịch đã có: AI chỉ trả về các chỗ cần sửa (cũ → mới), không viết lại cả cuốn.

Tiết kiệm token: đầu ra chỉ gồm những câu bị sửa, nên một chương ít lỗi tốn rất ít.
Mọi chỗ sửa được ghi vào <kết quả>.edits.json để duyệt từng chỗ (nhận / bỏ) rồi xuất lại.
"""

from __future__ import annotations

import difflib
import html as html_lib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .bible import StoryBible
from .cache import TranslationCache
from .consistency import contains, contains_target
from .document import Document, load_document, write_document
from .genres import BUILTIN as BUILTIN_GENRES, GenreProfile
from .providers import ProviderChain
from .styles import BUILTIN as BUILTIN_STYLES, StyleProfile
from .translator import TranslationError, TranslatorConfig, _profile_parts, parse_json

log = logging.getLogger(__name__)

POSTEDIT_VERSION = "1"
UNIT_CHARS = 3500
UNIT_PARAGRAPHS = 25

LEVELS = {
    "nhe": (
        "Nhẹ",
        "Chỉ sửa lỗi rõ ràng: hiểu sai nghĩa, dịch sót ý, câu tối nghĩa hoặc thô hẳn, tên riêng/xưng hô sai sổ tay, "
        "lỗi chính tả. Câu đã đọc được thì để nguyên. Mỗi đoạn thường 0–1 chỗ sửa.",
    ),
    "vua": (
        "Vừa",
        "Sửa lỗi rõ ràng, và trau lại những câu còn \"văn dịch\": nghĩa đen, cứng, sát chữ tiếng Anh, thành ngữ dịch "
        "word-by-word, lời thoại không giống người Việt nói, câu đùa mất duyên. Giữ phần lớn bản dịch; sửa theo cụm "
        "hoặc theo câu.",
    ),
    "sau": (
        "Sâu",
        "Biên tập như một dịch giả hàng đầu chấp bút lại: viết lại mạnh tay mọi câu chưa hay — sắp lại trật tự, "
        "đổi cách diễn đạt, chọn từ đắt hơn, làm câu đùa buồn cười hơn — miễn đúng ý bản gốc và đúng sổ tay. "
        "Có thể sửa cả câu dài; vẫn không gộp hay tách đoạn.",
    ),
}

_BLOCK_RE = re.compile(r"<(p|h[1-6]|li|dt|dd)\b[^>]*>(.*?)</\1\s*>", re.DOTALL | re.IGNORECASE)
_TEXT_PARA_RE = re.compile(r"\S(?:.*?\S)??(?=[ \t]*\n\s*\n|\s*\Z)", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>")
_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|\w+);")

Progress = Callable[[str, str, int, int], None]


# ---- đoạn văn ----------------------------------------------------------


@dataclass
class Paragraph:
    section_id: str
    title: str
    start: int  # vị trí phần nội dung trong thân chương
    end: int
    raw: str  # nội dung gốc (HTML bên trong thẻ, hoặc chữ thuần)
    is_html: bool

    @property
    def text(self) -> str:
        return _plain(self.raw, self.is_html)[0]


def _plain(raw: str, is_html: bool) -> tuple[str, list[int]]:
    """Chữ hiển thị (gộp khoảng trắng) + vị trí trong `raw` của từng ký tự, để áp chỗ sửa ngược lại vào HTML."""
    out: list[str] = []
    pos: list[int] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if is_html and ch == "<":
            j = raw.find(">", i)
            if j == -1:
                j = n - 1
            if out and out[-1] != " " and re.match(r"</?(br|p|div)\b", raw[i : j + 1], re.IGNORECASE):
                out.append(" ")
                pos.append(i)
            i = j + 1
            continue
        if is_html and ch == "&":
            m = _ENTITY_RE.match(raw, i)
            if m:
                for c in html_lib.unescape(m.group(0)):
                    out.append(" " if c.isspace() else c)
                    pos.append(i)
                i = m.end()
                continue
        if ch.isspace():
            if out and out[-1] != " ":
                out.append(" ")
                pos.append(i)
            i += 1
            continue
        out.append(ch)
        pos.append(i)
        i += 1
    while out and out[-1] == " ":
        out.pop()
        pos.pop()
    pos.append(n)  # vị trí kết thúc
    return "".join(out), pos


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _html_range(raw: str, pos: list[int], i: int, j: int) -> tuple[int, int]:
    """Vùng trong `raw` ứng với ký tự hiển thị [i, j) (j == i: điểm chèn trước ký tự i)."""
    if j <= i:
        return pos[i], pos[i]
    end = pos[j - 1] + 1
    if raw[pos[j - 1]] == "&":  # ký tự cuối đến từ một thực thể &...;
        m = _ENTITY_RE.match(raw, pos[j - 1])
        end = m.end() if m else end
    return pos[i], end


def _tokens(s: str) -> list[str]:
    return re.findall(r"\S+|\s+", s)


def apply_edit(raw: str, is_html: bool, old: str, new: str) -> str | None:
    """Thay `old` (chữ hiển thị) bằng `new` trong nội dung đoạn; None nếu không tìm thấy `old`.

    Với HTML, chỉ thay những chữ thật sự khác (so theo từng từ) nên các thẻ định dạng nằm giữa câu
    (in nghiêng, chữ hoa nhỏ...) được giữ nguyên.
    """
    old, new = _norm_ws(old), _norm_ws(new)
    if not old:
        return None
    text, pos = _plain(raw, is_html)
    idx = text.find(old)
    if idx == -1:
        return None
    esc = (lambda x: html_lib.escape(x, quote=False)) if is_html else (lambda x: x)
    start, end = _html_range(raw, pos, idx, idx + len(old))
    if not is_html or "<" not in raw[start:end]:
        return raw[:start] + esc(new) + raw[end:]
    a, b = _tokens(old), _tokens(new)
    offs = [0]
    for t in a:
        offs.append(offs[-1] + len(t))
    out = raw
    ops = difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in reversed(ops):  # từ cuối lên: vị trí phía trước không bị lệch
        if tag == "equal":
            continue
        s, e = _html_range(raw, pos, idx + offs[i1], idx + offs[i2])
        piece = esc("".join(b[j1:j2]))
        tags = "".join(_TAG_RE.findall(raw[s:e]))  # vùng sửa trùm qua thẻ: giữ thẻ, chữ mới đặt trước
        out = out[:s] + piece + tags + out[e:]
    return out


def section_body(section) -> str:
    return section.sep.join(section.chunks)


def paragraphs_of(doc: Document) -> list[Paragraph]:
    paras = []
    for s in doc.sections:
        body = section_body(s)
        if s.kind == "html":
            for m in _BLOCK_RE.finditer(body):
                p = Paragraph(s.id, s.title, m.start(2), m.end(2), m.group(2), True)
                if p.text:
                    paras.append(p)
        else:
            for m in _TEXT_PARA_RE.finditer(body):
                paras.append(Paragraph(s.id, s.title, m.start(), m.end(), m.group(0), False))
    return paras


def align_sources(translated: list[Paragraph], source: list[Paragraph] | None) -> list[str]:
    """Bản gốc tương ứng từng đoạn dịch (chuỗi rỗng nếu không ghép được)."""
    if not source:
        return [""] * len(translated)
    if len(source) == len(translated):
        return [p.text for p in source]
    # Số đoạn cả cuốn lệch nhau: ghép theo từng chương cùng tên file (EPUB) nếu chương đó khớp số đoạn.
    by_sec_src: dict[str, list[Paragraph]] = {}
    for p in source:
        by_sec_src.setdefault(p.section_id, []).append(p)
    by_sec_dst: dict[str, int] = {}
    for p in translated:
        by_sec_dst[p.section_id] = by_sec_dst.get(p.section_id, 0) + 1
    out, seen = [], {}
    for p in translated:
        i = seen.get(p.section_id, 0)
        seen[p.section_id] = i + 1
        src = by_sec_src.get(p.section_id, [])
        out.append(src[i].text if len(src) == by_sec_dst[p.section_id] else "")
    return out


# ---- chỗ sửa -----------------------------------------------------------


@dataclass
class Edit:
    id: int
    section: str
    title: str
    p: int  # chỉ số đoạn trong cả cuốn
    old: str
    new: str
    why: str = ""
    status: str = "applied"  # lần áp gần nhất: "applied" | "failed" (không thấy câu cũ) | "skipped" (bị bỏ)
    accepted: bool = True  # lựa chọn của người dùng
    context: str = ""  # đoạn dịch trước khi sửa (để duyệt)
    source: str = ""  # đoạn gốc tương ứng

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _edit_schema() -> dict:
    s = {"type": "string"}
    item = {
        "type": "object",
        "properties": {"p": {"type": "integer"}, "old": s, "new": s, "why": s},
        "required": ["p", "old", "new", "why"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"edits": {"type": "array", "items": item}},
        "required": ["edits"],
        "additionalProperties": False,
    }


def build_postedit_rules(
    config: TranslatorConfig, genre: GenreProfile, style: StyleProfile, level: str, guide: str, has_source: bool
) -> str:
    name, how = LEVELS[level]
    parts = [
        f"Bạn là biên tập viên văn học kỳ cựu, hiệu đính bản dịch {genre.name} sang {config.target_lang} "
        "để bản cuối đọc như văn chương viết thẳng bằng tiếng mẹ đẻ, đúng phong cách mô tả dưới đây.",
        "",
        "Bạn nhận các đoạn đánh số [n]" + (", mỗi đoạn có GỐC (bản gốc) và DỊCH (bản dịch hiện tại)." if has_source
                                          else ", mỗi đoạn là DỊCH (bản dịch hiện tại; không có bản gốc)."),
        f"Mức hiệu đính: {name}. {how}",
        "- " + ("Đối chiếu với GỐC: sửa chỗ hiểu sai, dịch sót, dịch thừa; không thêm bớt ý." if has_source
                else "Không có bản gốc: chỉ sửa câu văn, không đoán thêm hay bớt ý."),
        "- GIỮ NGUYÊN tên riêng, thuật ngữ, xưng hô như <so_tay>; chỗ nào lệch sổ tay thì sửa về đúng sổ tay.",
        "- Đại từ người kể dùng cho mỗi nhân vật phải cố định trong cùng cảnh.",
        "- Chỉ sửa nội dung trong từng đoạn; không gộp, tách, xóa hay thêm đoạn; không thêm chú thích người dịch.",
        *_profile_parts(config, genre, style),
    ]
    if guide.strip():
        parts += ["", "HƯỚNG DẪN HIỆU ĐÍNH CỦA NGƯỜI DÙNG (ưu tiên cao nhất, trừ quy tắc định dạng đầu ra):", guide.strip()]
    parts += [
        "",
        "Định dạng trả lời (bắt buộc): một JSON object {\"edits\": [{\"p\": số đoạn, \"old\": ..., \"new\": ..., \"why\": ...}]}",
        "- \"old\": chuỗi chép NGUYÊN VĂN từ DỊCH của đoạn p (đúng từng chữ, dấu câu, hoa thường), đủ dài để chỉ xuất hiện "
        "một lần trong đoạn — thường là một cụm hoặc một câu; không chép phần GỐC.",
        "- \"new\": chuỗi thay thế cho \"old\". \"why\": lý do ngắn, tối đa 12 từ (vd. \"nghĩa đen\", \"sai tên sổ tay\").",
        "- Hai chỗ sửa trong cùng đoạn không được chồng lên nhau. Đoạn nào đã tốt thì không ghi gì.",
        "- Không có gì cần sửa thì trả về {\"edits\": []}.",
    ]
    return "\n".join(parts)


@dataclass
class Unit:
    indices: list[int]  # chỉ số đoạn trong cả cuốn
    title: str


def make_units(paras: list[Paragraph], selected: set[str] | None, chars: int = UNIT_CHARS, limit: int = UNIT_PARAGRAPHS) -> list[Unit]:
    units: list[Unit] = []
    current: list[int] = []
    size, sec = 0, None
    for i, p in enumerate(paras):
        if selected is not None and p.section_id not in selected:
            continue
        n = len(p.text)
        if current and (p.section_id != sec or size + n > chars or len(current) >= limit):
            units.append(Unit(current, paras[current[0]].title))
            current, size = [], 0
        current.append(i)
        size += n
        sec = p.section_id
    if current:
        units.append(Unit(current, paras[current[0]].title))
    return units


@dataclass
class PostEditResult:
    edits: list[Edit] = field(default_factory=list)  # mọi chỗ sửa của cả cuốn (kể cả các chương làm trước)
    units: int = 0
    new: int = 0  # số chỗ sửa vừa tìm được ở lần chạy này

    @property
    def applied(self) -> int:
        return sum(1 for e in self.edits if e.status == "applied")


class PostEditor:
    def __init__(
        self,
        config: TranslatorConfig,
        llm: ProviderChain,
        genre: GenreProfile | None = None,
        style: StyleProfile | None = None,
        bible: StoryBible | None = None,
        cache: TranslationCache | None = None,
        level: str = "vua",
        guide: str = "",
    ):
        if level not in LEVELS:
            raise ValueError(f"Mức hiệu đính không hợp lệ: {level} (chọn: {', '.join(LEVELS)})")
        self.config = config
        self.llm = llm
        self.genre = genre or BUILTIN_GENRES["general"]
        self.style = style or BUILTIN_STYLES["natural"]
        self.bible = bible or StoryBible()
        self.cache = cache if cache is not None else TranslationCache(None)
        self.level = level
        self.guide = guide

    def _so_tay(self, source_text: str, translated_text: str) -> str:
        if source_text.strip():
            return self.bible.passage_block(source_text, contains)
        # Không có bản gốc: lấy các tên có bản dịch xuất hiện trong đoạn dịch.
        entities = [e for e in self.bible.entities.values() if contains_target(translated_text, e.target)]
        return self.bible.prompt_block(True, entities, []) if entities else ""

    def edit_unit(self, texts: list[str], sources: list[str]) -> list[dict]:
        """Gửi một nhóm đoạn; trả về các chỗ sửa (p đánh số từ 1 trong nhóm)."""
        has_source = any(s.strip() for s in sources)
        system = build_postedit_rules(self.config, self.genre, self.style, self.level, self.guide, has_source)
        lines = []
        for n, (dst, src) in enumerate(zip(texts, sources), start=1):
            if has_source:
                lines += [f"[{n}] GỐC: {src or '(không có)'}", f"    DỊCH: {dst}"]
            else:
                lines.append(f"[{n}] DỊCH: {dst}")
        so_tay = self._so_tay("\n".join(sources), "\n".join(texts))
        user = (f"<so_tay>\n{so_tay}\n</so_tay>\n\n" if so_tay else "") + "<doan>\n" + "\n".join(lines) + "\n</doan>"
        key = TranslationCache.make_key("postedit", POSTEDIT_VERSION, self.llm.id, self.level, system, user)
        cached = self.cache.get(key)
        if cached is not None:
            return json.loads(cached)
        result = self.llm.complete(system, user, effort=self.config.effort, json_schema=_edit_schema(), max_tokens=16000)
        if result.stop_reason == "refusal":
            log.warning("Mô hình từ chối hiệu đính một nhóm đoạn (%s), bỏ qua", result.provider)
            return []
        try:
            data = parse_json(result.text)
            edits = data.get("edits", []) if isinstance(data, dict) else []
        except ValueError as e:
            raise TranslationError(f"Kết quả hiệu đính không hợp lệ ({result.provider}, {result.stop_reason}).") from e
        clean = []
        for e in edits if isinstance(edits, list) else []:
            if not isinstance(e, dict):
                continue
            try:
                p = int(e.get("p"))
            except (TypeError, ValueError):
                continue
            old, new = str(e.get("old") or ""), str(e.get("new") or "")
            if 1 <= p <= len(texts) and old.strip() and _norm_ws(old) != _norm_ws(new):
                clean.append({"p": p, "old": old, "new": new, "why": str(e.get("why") or "")})
        self.cache.put(key, json.dumps(clean, ensure_ascii=False))
        return clean


def parse_sections(spec: str | None, doc: Document) -> set[str] | None:
    """"1,3-5" → id các chương tương ứng (đánh số từ 1). None = cả cuốn."""
    if not spec or not spec.strip():
        return None
    chosen: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            chosen.update(range(int(a), int(b) + 1))
        else:
            chosen.add(int(part))
    bad = [n for n in chosen if not 1 <= n <= len(doc.sections)]
    if bad:
        raise ValueError(f"Chương {bad[0]} không có (sách có {len(doc.sections)} chương)")
    return {doc.sections[n - 1].id for n in chosen}


def postedit_document(
    editor: PostEditor,
    translated: Document,
    source: Document | None,
    output: Path,
    sections: set[str] | None = None,
    workers: int = 4,
    progress: Progress | None = None,
    existing: list[Edit] | None = None,
) -> PostEditResult:
    """Hiệu đính các chương `sections` (None = cả cuốn), gộp với chỗ sửa sẵn có của các chương khác,
    rồi ghi lại TOÀN BỘ cuốn sách với mọi chỗ sửa đang được nhận."""
    paras = paragraphs_of(translated)
    sources = align_sources(paras, paragraphs_of(source) if source else None)
    units = make_units(paras, sections)
    done = [0]

    def work(unit: Unit):
        found = editor.edit_unit([paras[i].text for i in unit.indices], [sources[i] for i in unit.indices])
        done[0] += 1
        if progress:
            progress("Hiệu đính", unit.title, done[0], len(units))
        return [(unit.indices[e["p"] - 1], e) for e in found]

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(work, units))

    fresh = []
    for found in results:
        for gi, e in found:
            p = paras[gi]
            fresh.append(Edit(0, p.section_id, p.title, gi, e["old"], e["new"], e["why"], context=p.text, source=sources[gi]))
    processed = sections if sections is not None else {p.section_id for p in paras}
    edits = merge_edits(existing or [], fresh, processed)
    _apply_and_write(translated, paras, edits, output)
    return PostEditResult(edits, len(units), len(fresh))


def merge_edits(existing: list[Edit], fresh: list[Edit], processed: set[str]) -> list[Edit]:
    """Chỗ sửa của chương vừa hiệu đính thay cho chỗ sửa cũ của chương đó; chương khác giữ nguyên.
    Chỗ sửa trùng hệt lần trước giữ số thứ tự và lựa chọn nhận/bỏ của người dùng."""
    old_by_key = {(e.p, e.old, e.new): e for e in existing if e.section in processed}
    kept = [e for e in existing if e.section not in processed]
    next_id = max((e.id for e in existing), default=0) + 1
    for e in fresh:
        prev = old_by_key.get((e.p, e.old, e.new))
        if prev:
            e.id, e.accepted = prev.id, prev.accepted
        else:
            e.id, next_id = next_id, next_id + 1
    return sorted(kept + fresh, key=lambda e: (e.p, e.id))


def _apply_and_write(doc: Document, paras: list[Paragraph], edits: list[Edit], output: Path) -> None:
    """Áp lần lượt các chỗ sửa được nhận lên bản dịch, ghi trạng thái từng chỗ, rồi ghi sách."""
    current = {i: p.raw for i, p in enumerate(paras)}
    for e in edits:
        if not e.accepted:
            e.status = "skipped"
            continue
        fixed = apply_edit(current[e.p], paras[e.p].is_html, e.old, e.new)
        e.status = "failed" if fixed is None else "applied"
        if fixed is not None:
            current[e.p] = fixed
    bodies = {s.id: section_body(s) for s in doc.sections}
    # Thay từ cuối lên để vị trí các đoạn phía trước không bị lệch.
    for i in sorted(current, key=lambda i: -paras[i].start):
        p = paras[i]
        if current[i] != p.raw:
            body = bodies[p.section_id]
            bodies[p.section_id] = body[: p.start] + current[i] + body[p.end :]
    write_document(doc, bodies, output)


# ---- file duyệt .edits.json --------------------------------------------


def default_output(translated: Path) -> Path:
    return translated.with_name(f"{translated.stem}.hieu-dinh{translated.suffix}")


def edits_path(output: Path) -> Path:
    return output.with_name(f"{output.name}.edits.json")


def _rel(base: Path, target: Path) -> str:
    """Đường dẫn tương đối so với thư mục file chỗ sửa (để chuyển cả thư mục sang máy khác vẫn dùng được)."""
    try:
        return target.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(target.resolve())


CHAPTER_REVIEW, CHAPTER_APPROVED, CHAPTER_SKIPPED = "review", "approved", "skipped"


def save_edits(
    path: Path, translated: Path, source: Path | None, output: Path, level: str, edits: list[Edit],
    chapters: dict | None = None,
) -> None:
    """`chapters`: id chương → {"title", "status": review | approved | skipped, "at"} — tiến độ duyệt từng chương."""
    base = path.parent
    data = {
        "version": 1,
        "translated": _rel(base, translated),
        "source": _rel(base, source) if source else "",
        "output": _rel(base, output),
        "level": level,
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "chapters": chapters or {},
        "edits": [e.to_dict() for e in edits],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_edits(path: Path) -> tuple[dict, list[Edit]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    fields = set(Edit.__dataclass_fields__)
    return data, [Edit(**{k: v for k, v in e.items() if k in fields}) for e in data.get("edits", [])]


def _resolve(base: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else base / p


def existing_edits(path: Path, translated: Path) -> tuple[list[Edit], dict]:
    """Chỗ sửa và tiến độ duyệt từng chương đã có của cùng bản dịch (để hiệu đính tiếp từng chương)."""
    if not path.exists():
        return [], {}
    data, edits = load_edits(path)
    if _resolve(path.parent, data.get("translated", "")).resolve() != translated.resolve():
        return [], {}
    return edits, data.get("chapters") or {}


def mark_chapters(chapters: dict, ids, status: str, titles: dict[str, str] | None = None) -> dict:
    for sid in ids:
        entry = chapters.setdefault(sid, {})
        entry.update(status=status, at=time.strftime("%Y-%m-%d %H:%M"))
        if titles and sid in titles:
            entry["title"] = titles[sid]
    return chapters


def chapter_overview(doc: Document, chapters: dict, edits: list[Edit]) -> list[dict]:
    """Danh sách chương (đánh số như --chuong) kèm số đoạn, số chỗ sửa và trạng thái duyệt."""
    paras = paragraphs_of(doc)
    count = {}
    for p in paras:
        count[p.section_id] = count.get(p.section_id, 0) + 1
    out = []
    for n, s in enumerate(doc.sections, start=1):
        mine = [e for e in edits if e.section == s.id]
        out.append({
            "number": n, "id": s.id, "title": s.title, "paragraphs": count.get(s.id, 0),
            "edits": len(mine), "accepted": sum(1 for e in mine if e.accepted),
            "status": (chapters.get(s.id) or {}).get("status", ""),
        })
    return out


def apply_edits_file(
    path: Path, decisions: dict[int, bool] | None = None, output: Path | None = None,
    chapter_status: dict[str, str] | None = None,
) -> tuple[int, int]:
    """Xuất lại CẢ CUỐN theo lựa chọn nhận/bỏ từng chỗ sửa (và ghi trạng thái duyệt chương nếu có).
    Trả về (số chỗ đã áp, số chỗ không áp được)."""
    data, edits = load_edits(path)
    chapters = data.get("chapters") or {}
    for sid, status in (chapter_status or {}).items():
        if status in (CHAPTER_REVIEW, CHAPTER_APPROVED, CHAPTER_SKIPPED):
            mark_chapters(chapters, [sid], status)
    data["chapters"] = chapters
    for e in edits:
        if decisions and e.id in decisions:
            e.accepted = bool(decisions[e.id])
    translated = _resolve(path.parent, data["translated"])
    out = output or _resolve(path.parent, data["output"])
    doc = load_document(translated)
    paras = paragraphs_of(doc)
    for e in edits:
        if e.p >= len(paras):
            raise ValueError(f"{path.name}: bản dịch {translated.name} đã thay đổi, không khớp file chỗ sửa nữa")
    _apply_and_write(doc, paras, edits, out)
    data["edits"] = [e.to_dict() for e in edits]
    data["updated"] = time.strftime("%Y-%m-%d %H:%M")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    applied = sum(1 for e in edits if e.accepted and e.status == "applied")
    return applied, sum(1 for e in edits if e.accepted and e.status == "failed")

