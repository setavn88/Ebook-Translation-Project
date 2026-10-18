"""Đọc/ghi các định dạng sách: TXT, Markdown, EPUB."""

from __future__ import annotations

import logging
import posixpath
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree as ET

from .chunker import DEFAULT_CHUNK_CHARS, split_html, split_text
from .translator import Translator

log = logging.getLogger(__name__)

SUPPORTED = (".txt", ".md", ".markdown", ".epub")

_NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
}
_BODY_RE = re.compile(r"(<body\b[^>]*>)(.*)(</body\s*>)", re.DOTALL | re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_LANG_ATTR_RE = re.compile(r'(\b(?:xml:)?lang=")[^"]*(")', re.IGNORECASE)
_DC_LANG_RE = re.compile(r"(<dc:language\b[^>]*>)[^<]*(</dc:language>)", re.IGNORECASE)

Progress = Callable[[str, int, int], None]


def default_output_path(input_path: Path, lang_code: str) -> Path:
    return input_path.with_name(f"{input_path.stem}.{lang_code}{input_path.suffix}")


def read_sample(input_path: Path, max_chars: int = 30000) -> str:
    """Lấy một đoạn văn bản thuần đại diện cho cuốn sách (dùng để tạo bảng thuật ngữ)."""
    if input_path.suffix.lower() != ".epub":
        return input_path.read_text(encoding="utf-8")[:max_chars]
    texts = []
    with zipfile.ZipFile(input_path) as zf:
        for name in _spine_documents(zf):
            html = zf.read(name).decode("utf-8", errors="replace")
            m = _BODY_RE.search(html)
            text = re.sub(r"<[^>]+>", " ", m.group(2) if m else html)
            texts.append(re.sub(r"\s+", " ", text).strip())
            if sum(map(len, texts)) >= max_chars:
                break
    return "\n\n".join(texts)[:max_chars]


def translate_file(
    input_path: Path,
    output_path: Path,
    translator: Translator,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    workers: int = 4,
    lang_code: str | None = None,
    progress: Progress | None = None,
) -> None:
    suffix = input_path.suffix.lower()
    if suffix == ".epub":
        translate_epub(input_path, output_path, translator, chunk_chars, workers, lang_code, progress)
        return
    if suffix not in SUPPORTED:
        raise ValueError(f"Định dạng {suffix} chưa được hỗ trợ (hỗ trợ: {', '.join(SUPPORTED)})")
    kind = "text" if suffix == ".txt" else "markdown"
    text = input_path.read_text(encoding="utf-8")
    chunks = split_text(text, chunk_chars)
    on_progress = (lambda done, total: progress(input_path.name, done, total)) if progress else None
    result = translator.translate_chunks(chunks, kind, on_progress=on_progress)
    output_path.write_text(result + "\n", encoding="utf-8")


# ---- EPUB --------------------------------------------------------------


def _opf_path(zf: zipfile.ZipFile) -> str:
    root = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = root.find(".//c:rootfile", _NS)
    if rootfile is None:
        raise ValueError("EPUB không hợp lệ: thiếu rootfile trong container.xml")
    return rootfile.attrib["full-path"]


def _spine_documents(zf: zipfile.ZipFile) -> list[str]:
    """Đường dẫn trong zip của các tài liệu XHTML theo thứ tự đọc (spine)."""
    opf = _opf_path(zf)
    base = posixpath.dirname(opf)
    root = ET.fromstring(zf.read(opf))
    manifest = {
        item.attrib["id"]: item.attrib
        for item in root.iterfind(".//opf:manifest/opf:item", _NS)
    }
    docs = []
    for ref in root.iterfind(".//opf:spine/opf:itemref", _NS):
        item = manifest.get(ref.attrib.get("idref", ""))
        if item and item.get("media-type") in ("application/xhtml+xml", "text/html"):
            docs.append(posixpath.normpath(posixpath.join(base, item["href"])))
    return docs


def _translate_xhtml(
    html: str, translator: Translator, chunk_chars: int, lang_code: str | None, on_progress
) -> str:
    m = _BODY_RE.search(html)
    if not m:
        return html
    chunks = split_html(m.group(2), chunk_chars)
    body = translator.translate_chunks(chunks, "html", sep="", on_progress=on_progress)
    out = html[: m.start(2)] + body + html[m.end(2) :]
    if lang_code:
        out = _HTML_TAG_RE.sub(
            lambda tag: _LANG_ATTR_RE.sub(lambda a: a.group(1) + lang_code + a.group(2), tag.group(0)), out, count=1
        )
    return out


def translate_epub(
    input_path: Path,
    output_path: Path,
    translator: Translator,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    workers: int = 4,
    lang_code: str | None = None,
    progress: Progress | None = None,
) -> None:
    with zipfile.ZipFile(input_path) as zin:
        docs = _spine_documents(zin)
        opf = _opf_path(zin)
        sources = {name: zin.read(name).decode("utf-8") for name in docs}

        def work(name: str) -> tuple[str, str]:
            cb = (lambda d, t: progress(name, d, t)) if progress else None
            return name, _translate_xhtml(sources[name], translator, chunk_chars, lang_code, cb)

        # Các chương dịch song song; trong một chương dịch tuần tự để giữ mạch văn.
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            translated = dict(pool.map(work, docs))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w") as zout:
            # Theo chuẩn EPUB, "mimetype" phải là mục đầu tiên và không nén.
            if "mimetype" in zin.namelist():
                zout.writestr(zin.getinfo("mimetype"), zin.read("mimetype"), compress_type=zipfile.ZIP_STORED)
            for info in zin.infolist():
                if info.filename == "mimetype":
                    continue
                if info.filename in translated:
                    data = translated[info.filename].encode("utf-8")
                elif info.filename == opf and lang_code:
                    text = zin.read(info).decode("utf-8")
                    data = _DC_LANG_RE.sub(lambda mm: mm.group(1) + lang_code + mm.group(2), text).encode("utf-8")
                else:
                    data = zin.read(info)
                zout.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
