import json
import threading
import zipfile

import pytest

from booktranslator import cli
from booktranslator.bible import Entity, StoryBible
from booktranslator.document import load_document, write_document
from booktranslator.genres import BUILTIN
from booktranslator.pipeline import analyze_document, translate_document
from booktranslator.translator import Translator, TranslatorConfig

from fakes import DEFAULT_ANALYSIS, default_responder, fake_chain, fake_translate, reply, source_of, translation_reply

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

STORY = """# Chapter 1

Alice met Bob at the station. Alice smiled.

# Chapter 2

Bob waited for Alice in the rain."""


def make_epub(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr("OEBPS/text/ch1.xhtml", CHAPTER.format(n=1))
        z.writestr("OEBPS/text/ch2.xhtml", CHAPTER.format(n=2))
        z.writestr("OEBPS/style.css", "p { margin: 0 }")


def make_translator(responder=None, analyzer=None, bible=None, polish=True):
    chain, provider = fake_chain(responder, analyzer)
    t = Translator(TranslatorConfig(polish=polish), chain, BUILTIN["general"], None, bible or StoryBible())
    return t, provider


@pytest.fixture
def fake_api(monkeypatch):
    """Thay mọi nhà cung cấp API của CLI bằng một provider giả lập dùng chung."""
    chain, provider = fake_chain()
    monkeypatch.setattr(cli, "build_chain", lambda *a, **kw: chain)
    return provider


# ---- document ---------------------------------------------------------


def test_text_split_into_chapters(tmp_path):
    src = tmp_path / "s.md"
    src.write_text("Lời tựa ngắn.\n\n" + STORY, encoding="utf-8")
    doc = load_document(src)
    assert [s.title for s in doc.sections] == ["Phần 1", "Chapter 1", "Chapter 2"]
    out = tmp_path / "o.md"
    write_document(doc, {s.id: s.sep.join(s.chunks) for s in doc.sections}, out)
    assert out.read_text(encoding="utf-8") == "Lời tựa ngắn.\n\n" + STORY + "\n"


def test_chinese_chapter_headings(tmp_path):
    src = tmp_path / "s.txt"
    src.write_text("第一章 开始\n\n张无忌来了。\n\n第二章 结束\n\n他走了。", encoding="utf-8")
    assert [s.title for s in load_document(src).sections] == ["第一章 开始", "第二章 结束"]


def test_epub_load_and_write(tmp_path):
    src = tmp_path / "book.epub"
    make_epub(src)
    doc = load_document(src)
    assert [s.title for s in doc.sections] == ["Chapter 1", "Chapter 2"]
    assert "Alice opened the door ." in doc.sections[0].plain_text()
    out = tmp_path / "out.epub"
    write_document(doc, {s.id: fake_translate(s.sep.join(s.chunks)) for s in doc.sections}, out, "vi")
    with zipfile.ZipFile(out) as z:
        infos = z.infolist()
        assert infos[0].filename == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED
        ch1 = z.read("OEBPS/text/ch1.xhtml").decode()
        assert '<p class="x">IT WAS <a href="#c1">DARK</a> OUTSIDE.</p>' in ch1
        assert "<title>Chapter 1</title>" in ch1
        assert 'xml:lang="vi" lang="vi"' in ch1
        assert "<dc:language>vi</dc:language>" in z.read("OEBPS/content.opf").decode()
        assert z.read("OEBPS/style.css") == b"p { margin: 0 }"


# ---- pipeline ---------------------------------------------------------


def test_analysis_merge_is_in_reading_order(tmp_path):
    """Chương 2 phân tích xong trước nhưng tên do chương 1 đặt vẫn thắng."""
    src = tmp_path / "s.md"
    src.write_text(STORY, encoding="utf-8")
    done_ch2 = threading.Event()

    def analyzer(title, text):
        if title == "Chapter 1":
            done_ch2.wait(2)
            target = "Ali"
        else:
            target = "A-li-xơ"
            done_ch2.set()
        return DEFAULT_ANALYSIS | {
            "summary": f"tóm tắt {title}",
            "entities": [{"source": "Alice", "target": target, "type": "character", "aliases": [], "gender": "", "note": ""}],
        }

    t, _ = make_translator(analyzer=analyzer)
    doc = load_document(src)
    analyze_document(t, doc, workers=2)
    assert t.bible.find("Alice").target == "Ali"
    assert t.bible.sections[doc.sections[1].id].summary == "tóm tắt Chapter 2"


def test_translate_document_fixes_untranslated_names(tmp_path):
    src = tmp_path / "s.md"
    src.write_text(STORY, encoding="utf-8")
    bible = StoryBible()
    bible.add_entity(Entity("Alice", "Ali", "character"))
    bible.add_entity(Entity("Bob", "Bốp", "character"))

    def sloppy(system, user):
        # Lần đầu "quên" dịch tên Bob; khi có <reminder> thì dịch đúng.
        if "<reminder>" in user or "<draft>" in user:
            return default_responder(system, user)
        return reply(translation_reply(fake_translate(source_of(user), {"Alice": "Ali"}).replace("BOB", "Bob")))

    t, _ = make_translator(sloppy, bible=bible)
    out = tmp_path / "o.md"
    result = translate_document(t, load_document(src), out, workers=2)
    text = out.read_text(encoding="utf-8")
    assert "Bob" not in text and text.count("Bốp") == 2 and text.count("Ali") == 3
    assert result.fixed_chunks == 2
    assert [i for i in result.issues if i.problem == "untranslated"] == []
    assert "Đoạn đã tự dịch lại để sửa tên: **2**" in result.report


def test_translate_document_no_fix_reports_only(tmp_path):
    src = tmp_path / "s.md"
    src.write_text(STORY, encoding="utf-8")
    bible = StoryBible()
    bible.add_entity(Entity("Bob", "Bốp", "character"))
    t, _ = make_translator(lambda s, u: reply(translation_reply(source_of(u))), bible=bible, polish=False)
    result = translate_document(t, load_document(src), tmp_path / "o.md", fix=False)
    assert result.fixed_chunks == 0
    assert {i.problem for i in result.issues} == {"untranslated"}
    assert "Còn sót tên gốc" in result.report


# ---- CLI --------------------------------------------------------------


def test_cli_catalogs(capsys):
    assert cli.main(["the-loai"]) == 0 and cli.main(["phong-cach"]) == 0
    out = capsys.readouterr().out
    for key in ("scifi", "fantasy", "romance", "wuxia", "comic", "lylan"):
        assert key in out


def test_cli_analyze_then_translate_and_resume(tmp_path, fake_api, capsys):
    src = tmp_path / "story.md"
    src.write_text(STORY, encoding="utf-8")

    # 1) lập sổ tay để duyệt
    assert cli.main(["phan-tich", str(src), "-g", "romance"]) == 0
    bible_path = tmp_path / "story.bible.json"
    data = json.loads(bible_path.read_text(encoding="utf-8"))
    assert data["entities"][0]["target"] == "Ali" and len(data["sections"]) == 2
    # người dùng sửa tay: thêm tên Bob
    data["entities"].append({"source": "Bob", "target": "Bảo", "type": "character"})
    bible_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # 2) dịch với sổ tay đã sửa: không phân tích lại
    n_analysis = len(fake_api.analysis_calls)
    assert cli.main(["dich", str(src), "-g", "romance", "--so-tay", str(bible_path)]) == 0
    assert len(fake_api.analysis_calls) == n_analysis
    out = (tmp_path / "story.vi.md").read_text(encoding="utf-8")
    assert "Bảo" in out and "Bob" not in out and "Ali" in out
    assert "Lãng mạn" in fake_api.calls[0]["system"]
    assert (tmp_path / "story.vi.md.consistency.md").exists()

    # 3) chạy lại: mọi đoạn lấy từ cache
    n_calls = len(fake_api.calls)
    assert cli.main(["translate", str(src), "--genre", "romance", "--bible", str(bible_path)]) == 0
    assert len(fake_api.calls) == n_calls
    assert "Chưa gọi API lần nào" in capsys.readouterr().err

    # 4) --fresh: dịch lại
    assert cli.main(["dich", str(src), "-g", "romance", "--so-tay", str(bible_path), "--fresh"]) == 0
    assert len(fake_api.calls) > n_calls


def test_cli_presets_and_effort(tmp_path, fake_api):
    src = tmp_path / "s.txt"
    src.write_text("Hello.", encoding="utf-8")
    assert cli.main(["dich", str(src), "-g", "wuxia", "--no-analyze"]) == 0
    assert fake_api.calls[-1]["effort"] == "high"  # theo thể loại
    n = len(fake_api.calls)
    assert cli.main(["dich", str(src), "-g", "wuxia", "--no-analyze", "--muc", "tiet-kiem", "--fresh"]) == 0
    assert fake_api.calls[-1]["effort"] == "medium" and len(fake_api.calls) == n + 1  # không biên tập
    assert cli.main(["dich", str(src), "-g", "general", "--no-analyze", "--muc", "cao-cap", "--fresh"]) == 0
    assert fake_api.calls[-1]["effort"] == "high"
    assert cli.main(["dich", str(src), "--no-analyze", "--effort", "low", "--no-polish", "--fresh"]) == 0
    assert fake_api.calls[-1]["effort"] == "low"


def test_cli_chains_use_preset_models(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "build_chain", lambda spec, fb, override: seen.append((spec, override)) or fake_chain()[0])
    args = cli.build_parser().parse_args(["dich", "x.epub", "--muc", "tiet-kiem", "--providers", "claude,deepseek"])
    cli._chains(args)
    (t_spec, t_over), (a_spec, a_over) = seen
    assert t_over["claude"] == t_over["claude-code"] == "claude-sonnet-5-5"
    assert a_over["claude"] == a_over["claude-code"] == "claude-haiku-5-5"
    assert t_spec == [("claude", None), ("deepseek", None)]
    seen.clear()
    args = cli.build_parser().parse_args(["dich", "x.epub", "--model", "claude-fable-5-1"])
    cli._chains(args)
    assert seen[0][0] == [("claude", "claude-fable-5-1")] and seen[1][0] == [("claude", None)]


def test_cli_missing_file(tmp_path):
    assert cli.main(["dich", str(tmp_path / "nope.txt")]) == 2
