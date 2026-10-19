import json

from booktranslator import cli
from booktranslator.bible import Entity, StoryBible
from booktranslator.learnedits import (
    Lessons,
    append_guide,
    changes_from_docs,
    diff_snippets,
    learn_from_changes,
    repeated_substitutions,
    update_bible,
)
from booktranslator.document import load_document
from booktranslator.providers import Completion, Provider, ProviderChain

RAW = "Gã gặp quỷ lùn ở cầu.\n\nCâu giữ nguyên.\n\nHai con quỷ lùn cãi nhau trên Thế giới Đĩa."
EDITED = "Gã gặp Quỷ Đá ở cầu.\n\nCâu giữ nguyên.\n\nHai con Quỷ Đá cãi nhau trên Đĩa Giới."

LESSON = {
    "names": [{"source": "troll", "old": "quỷ lùn", "new": "Quỷ Đá"}, {"source": "", "old": "Thế giới Đĩa", "new": "Đĩa Giới"},
              {"source": "", "old": "lạ hoắc", "new": "lạ lắm"}],
    "lessons": ["Bỏ \"một cách\" (\"cười một cách vui vẻ\" → \"cười tươi\")."],
    "examples": [{"before": "quỷ lùn", "after": "Quỷ Đá", "note": "tên loài"}],
}


class LessonProvider(Provider):
    name = "fake-lesson"

    def __init__(self):
        super().__init__("m")
        self.calls = []

    def complete(self, system, user, *, effort="medium", json_schema=None, max_tokens=None):
        self.calls.append(user)
        return Completion(json.dumps(LESSON, ensure_ascii=False), "end", provider=self.id)


def docs(tmp_path):
    raw, edited = tmp_path / "raw.md", tmp_path / "edited.md"
    raw.write_text(RAW, encoding="utf-8")
    edited.write_text(EDITED, encoding="utf-8")
    return raw, edited


def test_changes_and_repeated_substitutions(tmp_path):
    raw, edited = docs(tmp_path)
    changes = changes_from_docs(load_document(raw), load_document(edited))
    assert len(changes) == 2
    subs = repeated_substitutions(changes)
    assert subs[0] == ("quỷ lùn", "Quỷ Đá", 2)
    long = " ".join(["chữ"] * 40)
    assert diff_snippets(long + " cũ " + long, long + " mới " + long) == [
        (" ".join(["chữ"] * 6 + ["cũ"] + ["chữ"] * 6), " ".join(["chữ"] * 6 + ["mới"] + ["chữ"] * 6))
    ]


def test_update_bible_user_choice_wins():
    bible = StoryBible()
    bible.add_entity(Entity("troll", "quỷ lùn", "creature"))
    bible.add_entity(Entity("Discworld", "Thế giới Đĩa", "place"))
    report = update_bible(bible, LESSON["names"])
    assert bible.find("troll").target == "Quỷ Đá" and bible.find("Discworld").target == "Đĩa Giới"
    assert any("chưa rõ tên gốc" in r for r in report)
    update_bible(bible, [{"source": "sea troll / water troll", "old": "", "new": "Quỷ Nước"}])
    assert bible.find("sea troll").target == "Quỷ Nước" and bible.find("water troll") is bible.find("sea troll")


def test_append_guide_is_idempotent(tmp_path):
    guide = tmp_path / "g.md"
    lessons = Lessons(LESSON["names"][:1], LESSON["lessons"], LESSON["examples"])
    assert append_guide(guide, lessons, "x.epub") == 3
    text = guide.read_text(encoding="utf-8")
    assert "Bài học tự rút ra" in text and "- quỷ lùn → Quỷ Đá" in text and "“quỷ lùn” → “Quỷ Đá” — tên loài" in text
    assert append_guide(guide, lessons, "x.epub") == 0


def test_learn_from_changes_prompt():
    chain = ProviderChain([LessonProvider()])
    bible = StoryBible()
    bible.add_entity(Entity("troll", "quỷ lùn", "creature"))
    from booktranslator.learnedits import Change

    lessons = learn_from_changes(chain, [Change("con quỷ lùn", "con Quỷ Đá"), Change("a b", "a c", accepted=False)], bible)
    user = chain.providers[0].calls[0]
    assert "<so_tay>\n- troll → quỷ lùn" in user and "<bi_bo>" in user and "TRƯỚC: con quỷ lùn" in user
    assert len(lessons.names) == 3 and lessons.lessons


def test_cli_learn_edits(tmp_path, monkeypatch, capsys):
    provider = LessonProvider()
    monkeypatch.setattr(cli, "build_chain", lambda *a, **kw: ProviderChain([provider]))
    raw, edited = docs(tmp_path)
    bible_path, guide = tmp_path / "b.json", tmp_path / "g.md"
    b = StoryBible(bible_path)
    b.add_entity(Entity("troll", "quỷ lùn", "creature"))
    b.save()
    assert cli.main(["hoc-tu-ban-sua", "--ban-tho", str(raw), "--ban-sua", str(edited),
                     "--so-tay", str(bible_path), "--huong-dan-hieu-dinh", str(guide)]) == 0
    assert StoryBible.load(bible_path).find("troll").target == "Quỷ Đá"
    assert "Bài học tự rút ra" in guide.read_text(encoding="utf-8")
    assert "quỷ lùn\" → \"Quỷ Đá\" (2 lần)" in provider.calls[0]
    assert cli.main(["hoc-tu-ban-sua", "--ban-tho", str(raw)]) == 2
