import json
import zipfile

import pytest

from booktranslator import cli, translator as translator_mod
from booktranslator.formats import read_sample, translate_file
from booktranslator.translator import Translator, TranslatorConfig

from fakes import FakeClient

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Book</dc:title><dc:language>en</dc:language></metadata>
  <manifest>
    <item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>"""

CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
<head><title>Chapter {n}</title></head>
<body>
<h1 id="c{n}">Chapter {n}</h1>
<p>Alice opened the <em>door</em>.</p>
<p class="x">It was <a href="#c{n}">dark</a> outside.</p>
</body>
</html>"""


def make_epub(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr("OEBPS/text/ch1.xhtml", CHAPTER.format(n=1))
        z.writestr("OEBPS/text/ch2.xhtml", CHAPTER.format(n=2))
        z.writestr("OEBPS/style.css", "p { margin: 0 }")


def make_translator():
    return Translator(TranslatorConfig(), client=FakeClient())


def test_translate_markdown(tmp_path):
    src = tmp_path / "book.md"
    src.write_text("# Title\n\nFirst paragraph.\n\nSecond **bold**.\n", encoding="utf-8")
    out = tmp_path / "out.md"
    t = make_translator()
    translate_file(src, out, t)
    assert out.read_text(encoding="utf-8") == "# TITLE\n\nFIRST PARAGRAPH.\n\nSECOND **BOLD**.\n"
    assert "Markdown" in t.client.messages.calls[0]["system"]


def test_unsupported_format(tmp_path):
    src = tmp_path / "book.pdf"
    src.write_bytes(b"%PDF")
    with pytest.raises(ValueError):
        translate_file(src, tmp_path / "o.pdf", make_translator())


def test_translate_epub(tmp_path):
    src = tmp_path / "book.epub"
    make_epub(src)
    out = tmp_path / "book.vi.epub"
    translate_file(src, out, make_translator(), workers=2, lang_code="vi")

    with zipfile.ZipFile(out) as z:
        infos = z.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        ch1 = z.read("OEBPS/text/ch1.xhtml").decode()
        assert '<p class="x">IT WAS <a href="#c1">DARK</a> OUTSIDE.</p>' in ch1
        assert "<title>Chapter 1</title>" in ch1  # phần head không bị gửi đi dịch
        assert 'xml:lang="vi" lang="vi"' in ch1
        assert "<dc:language>vi</dc:language>" in z.read("OEBPS/content.opf").decode()
        assert z.read("OEBPS/style.css") == b"p { margin: 0 }"


def test_read_sample_epub(tmp_path):
    src = tmp_path / "book.epub"
    make_epub(src)
    sample = read_sample(src)
    assert "Alice opened the door ." in sample
    assert "<p" not in sample


def test_cli_end_to_end(tmp_path, monkeypatch, capsys):
    fake = FakeClient()
    monkeypatch.setattr(translator_mod.anthropic, "Anthropic", lambda **kw: fake)
    src = tmp_path / "story.txt"
    src.write_text("Alice smiled.\n\nThe end.", encoding="utf-8")

    assert cli.main(["translate", str(src), "--auto-glossary"]) == 0
    out = tmp_path / "story.vi.txt"
    assert out.read_text(encoding="utf-8") == "ALICE SMILED.\n\nTHE END.\n"
    glossary = json.loads((tmp_path / "story.vi.glossary.json").read_text(encoding="utf-8"))
    assert glossary == {"Alice": "Ali"}
    assert "Alice → Ali" in fake.messages.calls[-1]["system"]

    # Chạy lại: mọi đoạn lấy từ cache, không gọi thêm API dịch
    n_calls = len(fake.messages.calls)
    assert cli.main(["translate", str(src), "--glossary", str(tmp_path / "story.vi.glossary.json")]) == 0
    assert len(fake.messages.calls) == n_calls
    assert "Tiếp tục từ" in capsys.readouterr().err


def test_cli_missing_file(tmp_path):
    assert cli.main(["translate", str(tmp_path / "nope.txt")]) == 2
