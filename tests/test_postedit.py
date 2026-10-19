import json
import re

from booktranslator import cli
from booktranslator.bible import Entity, StoryBible
from booktranslator.document import load_document
from booktranslator.postedit import (
    PostEditor,
    align_sources,
    apply_edit,
    apply_edits_file,
    edits_path,
    load_edits,
    make_units,
    paragraphs_of,
    parse_sections,
    postedit_document,
    save_edits,
)
from booktranslator.providers import Completion, Provider, ProviderChain
from booktranslator.translator import TranslatorConfig

from test_pipeline import make_epub

_LINE_RE = re.compile(r"^\[(\d+)\] (?:GỐC: .*\n    )?DỊCH: (.*)$", re.MULTILINE)


class EditProvider(Provider):
    """Hiệu đính giả: mỗi đoạn có cụm `old` thì trả về một chỗ sửa thay bằng `new`."""

    name = "fake-edit"

    def __init__(self, rules):
        super().__init__("m")
        self.rules = rules
        self.calls = []

    def complete(self, system, user, *, effort="medium", json_schema=None, max_tokens=None):
        self.calls.append({"system": system, "user": user, "json_schema": json_schema})
        edits = []
        for n, text in _LINE_RE.findall(user):
            for old, new in self.rules:
                if old in text:
                    edits.append({"p": int(n), "old": old, "new": new, "why": "thô"})
        edits.append({"p": 999, "old": "x", "new": "y", "why": "ngoài phạm vi"})  # bị lọc bỏ
        return Completion(json.dumps({"edits": edits}, ensure_ascii=False), "end", provider=self.id)


def editor(rules, **kw):
    provider = EditProvider(rules)
    return PostEditor(TranslatorConfig(), ProviderChain([provider]), **kw), provider


def test_apply_edit_maps_plain_text_back_to_html():
    raw = "He said &amp; <em>left</em>  the\n room&#8230; ok"
    assert apply_edit(raw, True, "the room…", "căn phòng…") == "He said &amp; <em>left</em>  căn phòng… ok"
    assert apply_edit(raw, True, "said &", "nói <và>") == "He nói &lt;và&gt; <em>left</em>  the\n room&#8230; ok"
    # Trùm qua thẻ: chỉ thay chữ khác nhau, giữ nguyên thẻ định dạng.
    assert apply_edit(raw, True, "said & left", "nói & left") == "He nói &amp; <em>left</em>  the\n room&#8230; ok"
    caps = 'Ở<span class="sc"> MỘT HỆ CHIỀU XA XÔI</span>, trên'
    assert apply_edit(caps, True, "Ở MỘT HỆ CHIỀU XA XÔI, trên", "Ở MỘT VŨ TRỤ XA XÔI, trên") == (
        'Ở<span class="sc"> MỘT VŨ TRỤ XA XÔI</span>, trên')
    assert apply_edit("a <i>b</i> c", True, "a b c", "x") == "x<i></i>"
    assert apply_edit(raw, True, "không có", "x") is None
    assert apply_edit("a  b\nc", False, "b c", "B") == "a  B"


def test_paragraphs_units_and_alignment(tmp_path):
    md = tmp_path / "b.md"
    md.write_text("# Một\n\nCâu một.\n\nCâu hai\ndòng hai.\n\n# Hai\n\nCâu ba.", encoding="utf-8")
    doc = load_document(md)
    paras = paragraphs_of(doc)
    assert [p.text for p in paras] == ["# Một", "Câu một.", "Câu hai dòng hai.", "# Hai", "Câu ba."]
    units = make_units(paras, None, chars=1000, limit=2)
    assert [u.indices for u in units] == [[0, 1], [2], [3, 4]]  # không vượt chương
    assert [u.indices for u in make_units(paras, {doc.sections[1].id})] == [[3, 4]]
    assert parse_sections("2", doc) == {doc.sections[1].id}
    # Lệch số đoạn cả cuốn: ghép theo từng chương khớp số đoạn, chương không khớp thì bỏ trống.
    assert align_sources(paras, paras[:3]) == ["# Một", "Câu một.", "Câu hai dòng hai.", "", ""]


def test_postedit_epub_writes_book_and_review_file(tmp_path):
    src = tmp_path / "book.epub"
    make_epub(src)  # dùng luôn làm "bản dịch" để kiểm tra áp vào HTML
    out = tmp_path / "book.hieu-dinh.epub"
    pe, provider = editor([("outside", "ngoài kia"), ("Alice opened", "Ali mở")],
                          bible=StoryBible(), guide="Ưu tiên câu ngắn.")
    doc = load_document(src)
    result = postedit_document(pe, doc, load_document(src), out, workers=1)
    assert provider.calls and "Ưu tiên câu ngắn." in provider.calls[0]["system"]
    assert "GỐC:" in provider.calls[0]["user"] and provider.calls[0]["json_schema"]
    assert result.applied == 4 and len(result.edits) == 4  # 2 chương × 2 chỗ sửa; p=999 bị lọc

    text = load_document(out).sections[0].chunks
    body = "".join(text)
    assert 'Ali mở the <em>door</em>.' in body and '<a href="#c1">dark</a> ngoài kia.' in body

    log = edits_path(out)
    save_edits(log, src, src, out, "vua", result.edits)
    data, edits = load_edits(log)
    assert data["translated"] == "book.epub" and edits[0].context
    # Bỏ chỗ sửa thứ nhất rồi xuất lại.
    first = next(e for e in edits if e.old == "Alice opened")
    applied, failed = apply_edits_file(log, {first.id: False})
    assert (applied, failed) == (3, 0)
    sections = {s.id: "".join(s.chunks) for s in load_document(out).sections}
    assert "Alice opened the" in sections[first.section] and "ngoài kia" in sections[first.section]
    assert sum("Ali mở" in b for b in sections.values()) == 1

    # Chạy lại: lấy từ cache, không gọi AI nữa.
    n = len(provider.calls)
    postedit_document(pe, doc, None, tmp_path / "again.epub", workers=1)
    assert len(provider.calls) > n  # không có bản gốc → prompt khác → gọi lại
    m = len(provider.calls)
    postedit_document(pe, doc, None, tmp_path / "again.epub", workers=1)
    assert len(provider.calls) == m


def test_postedit_without_source_uses_target_names_from_bible(tmp_path):
    md = tmp_path / "t.md"
    md.write_text("Gã Rincewind gặp quỷ lùn.", encoding="utf-8")
    bible = StoryBible()
    bible.add_entity(Entity("Rincewind", "Rincewind", "character"))
    bible.add_entity(Entity("troll", "Quỷ Đá", "creature"))
    pe, provider = editor([("quỷ lùn", "Quỷ Đá")], bible=bible)
    result = postedit_document(pe, load_document(md), None, tmp_path / "o.md")
    assert "Rincewind" in provider.calls[0]["user"] and "GỐC" not in provider.calls[0]["user"]
    assert result.applied == 1
    assert (tmp_path / "o.md").read_text(encoding="utf-8").strip() == "Gã Rincewind gặp Quỷ Đá."


def test_cli_postedit_and_apply(tmp_path, monkeypatch, capsys):
    provider = EditProvider([("thô", "mượt")])
    monkeypatch.setattr(cli, "build_chain", lambda *a, **kw: ProviderChain([provider]))
    vi = tmp_path / "s.vi.md"
    vi.write_text("Câu thô một.\n\nCâu hay.\n\nCâu thô hai.", encoding="utf-8")
    guide = tmp_path / "g.md"
    guide.write_text("Sửa nhẹ tay.", encoding="utf-8")
    assert cli.main(["hieu-dinh", str(vi), "--huong-dan-hieu-dinh", str(guide), "--muc-hieu-dinh", "nhe"]) == 0
    out = tmp_path / "s.vi.hieu-dinh.md"
    assert out.read_text(encoding="utf-8").count("mượt") == 2
    assert "Mức hiệu đính: Nhẹ" in provider.calls[0]["system"] and "Sửa nhẹ tay." in provider.calls[0]["system"]
    log = edits_path(out)
    assert len(json.loads(log.read_text(encoding="utf-8"))["edits"]) == 2
    assert cli.main(["ap-hieu-dinh", str(log), "--bo", "1"]) == 0
    assert out.read_text(encoding="utf-8") == "Câu thô một.\n\nCâu hay.\n\nCâu mượt hai.\n"
    assert cli.main(["hieu-dinh", str(vi), "-o", str(vi)]) == 2
