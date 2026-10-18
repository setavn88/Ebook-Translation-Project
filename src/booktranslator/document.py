"""Đọc sách thành các chương (section) → các đoạn (chunk), và ghi bản dịch trở lại đúng định dạng."""

from __future__ import annotations

import html as html_lib
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from .chunker import DEFAULT_CHUNK_CHARS, split_html, split_text

SUPPORTED = (".txt", ".md", ".markdown", ".epub")
SECTION_MAX_CHARS = 60000

_NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
}
_BODY_RE = re.compile(r"(<body\b[^>]*>)(.*)(</body\s*>)", re.DOTALL | re.IGNORECASE)
_TITLE_RE = re.compile(r"<(?:title|h1|h2)\b[^>]*>(.*?)</(?:title|h1|h2)>", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_LANG_ATTR_RE = re.compile(r'(\b(?:xml:)?lang=")[^"]*(")', re.IGNORECASE)
_DC_LANG_RE = re.compile(r"(<dc:language\b[^>]*>)[^<]*(</dc:language>)", re.IGNORECASE)
_HEADING_RE = re.compile(
    r"^\s*(?:#{1,3}\s+\S"
    r"|(?:chapter|chương|chuong|chapitre|kapitel|capítulo|part|phần|hồi|quyển|book|prologue|epilogue|lời mở đầu|vĩ thanh)\b"
    r"|第[0-9一二三四五六七八九十百千零〇两]+[章回节卷部])",
    re.IGNORECASE,
)


@dataclass
class Section:
    id: str
    title: str
    kind: str  # "text" | "markdown" | "html"
    chunks: list[str]
    sep: str = "\n\n"

    def plain_text(self) -> str:
        text = self.sep.join(self.chunks)
        if self.kind == "html":
            text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\s*\n\s*", "\n", text)
        return text.strip()


@dataclass
class Document:
    path: Path
    format: str  # "text" | "markdown" | "epub"
    sections: list[Section]
    epub_sources: dict[str, str] = field(default_factory=dict)

    @property
    def order(self) -> list[str]:
        return [s.id for s in self.sections]


def default_output_path(input_path: Path, lang_code: str) -> Path:
    return input_path.with_name(f"{input_path.stem}.{lang_code}{input_path.suffix}")


def load_document(path: Path, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> Document:
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return _load_epub(path, chunk_chars)
    if suffix not in SUPPORTED:
        raise ValueError(f"Định dạng {suffix} chưa được hỗ trợ (hỗ trợ: {', '.join(SUPPORTED)})")
    kind = "text" if suffix == ".txt" else "markdown"
    text = path.read_text(encoding="utf-8")
    sections = [
        Section(id=f"sec-{i:04d}", title=title, kind=kind, chunks=split_text(body, chunk_chars))
        for i, (title, body) in enumerate(_split_sections(text), start=1)
    ]
    return Document(path=path, format=kind, sections=[s for s in sections if s.chunks])


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Tách văn bản thành chương theo dòng tiêu đề; chương quá dài thì cắt thêm."""
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    sections: list[tuple[str, list[str]]] = []
    title, current, size = "", [], 0
    for p in paragraphs:
        first_line = p.strip().splitlines()[0]
        is_heading = len(first_line) < 120 and _HEADING_RE.match(first_line)
        if current and (is_heading or size + len(p) > SECTION_MAX_CHARS):
            sections.append((title, current))
            current, size = [], 0
            if not is_heading and title:
                title = f"{title} (tiếp)"
        if is_heading:
            title = first_line.lstrip("#").strip()
        current.append(p)
        size += len(p)
    if current:
        sections.append((title, current))
    return [(t or f"Phần {i}", "\n\n".join(ps)) for i, (t, ps) in enumerate(sections, start=1)]


# ---- EPUB --------------------------------------------------------------


def _opf_path(zf: zipfile.ZipFile) -> str:
    root = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = root.find(".//c:rootfile", _NS)
    if rootfile is None:
        raise ValueError("EPUB không hợp lệ: thiếu rootfile trong container.xml")
    return rootfile.attrib["full-path"]


def _spine_documents(zf: zipfile.ZipFile) -> list[str]:
    opf = _opf_path(zf)
    base = posixpath.dirname(opf)
    root = ET.fromstring(zf.read(opf))
    manifest = {item.attrib["id"]: item.attrib for item in root.iterfind(".//opf:manifest/opf:item", _NS)}
    docs = []
    for ref in root.iterfind(".//opf:spine/opf:itemref", _NS):
        item = manifest.get(ref.attrib.get("idref", ""))
        if item and item.get("media-type") in ("application/xhtml+xml", "text/html"):
            docs.append(posixpath.normpath(posixpath.join(base, item["href"])))
    return docs


def _load_epub(path: Path, chunk_chars: int) -> Document:
    sections, sources = [], {}
    with zipfile.ZipFile(path) as zf:
        for name in _spine_documents(zf):
            html = zf.read(name).decode("utf-8")
            sources[name] = html
            m = _BODY_RE.search(html)
            if not m:
                continue
            t = _TITLE_RE.search(html)
            title = html_lib.unescape(re.sub(r"<[^>]+>|\s+", " ", t.group(1))).strip() if t else ""
            sections.append(
                Section(id=name, title=title or posixpath.basename(name), kind="html", chunks=split_html(m.group(2), chunk_chars), sep="")
            )
    return Document(path=path, format="epub", sections=sections, epub_sources=sources)


def write_document(doc: Document, translated: dict[str, str], output: Path, lang_code: str | None = None) -> None:
    """`translated` ánh xạ section.id → nội dung đã dịch (cùng dạng với các chunk ghép lại)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    if doc.format != "epub":
        body = "\n\n".join(translated[s.id] for s in doc.sections)
        output.write_text(body + "\n", encoding="utf-8")
        return

    def replace_body(name: str) -> str:
        html = doc.epub_sources[name]
        m = _BODY_RE.search(html)
        out = html[: m.start(2)] + translated[name] + html[m.end(2) :]
        if lang_code:
            out = _HTML_TAG_RE.sub(
                lambda tag: _LANG_ATTR_RE.sub(lambda a: a.group(1) + lang_code + a.group(2), tag.group(0)), out, count=1
            )
        return out

    with zipfile.ZipFile(doc.path) as zin, zipfile.ZipFile(output, "w") as zout:
        opf = _opf_path(zin)
        # Theo chuẩn EPUB, "mimetype" phải là mục đầu tiên và không nén.
        if "mimetype" in zin.namelist():
            zout.writestr(zin.getinfo("mimetype"), zin.read("mimetype"), compress_type=zipfile.ZIP_STORED)
        for info in zin.infolist():
            if info.filename == "mimetype":
                continue
            if info.filename in translated:
                data = replace_body(info.filename).encode("utf-8")
            elif info.filename == opf and lang_code:
                text = zin.read(info).decode("utf-8")
                data = _DC_LANG_RE.sub(lambda mm: mm.group(1) + lang_code + mm.group(2), text).encode("utf-8")
            else:
                data = zin.read(info)
            zout.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
