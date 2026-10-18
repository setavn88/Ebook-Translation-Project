import json

import pytest

from booktranslator.bible import Addressing, Entity, SectionNotes, StoryBible
from booktranslator.consistency import check_chunk, contains
from booktranslator.genres import BUILTIN, load_genre


@pytest.mark.parametrize("key", list(BUILTIN))
def test_builtin_genres_are_complete(key):
    g = BUILTIN[key]
    assert g.key == key
    assert g.style.strip() and g.naming.strip() and g.analysis_focus
    assert "character" in g.entity_types
    assert g.effort in {"low", "medium", "high", "xhigh", "max"}
    assert g.name in g.prompt_block()


def test_required_genres_exist():
    assert {"scifi", "fantasy", "romance", "wuxia", "mystery"} <= set(BUILTIN)


def test_custom_genre_file_extends_base(tmp_path):
    path = tmp_path / "cyberpunk.json"
    path.write_text(json.dumps({"base": "scifi", "name": "Cyberpunk", "style": "Giọng bụi bặm, tiếng lóng đường phố."}))
    g = load_genre(path=path)
    assert g.key == "cyberpunk" and g.name == "Cyberpunk"
    assert "tiếng lóng" in g.style
    assert g.entity_types == BUILTIN["scifi"].entity_types


def test_unknown_genre():
    with pytest.raises(KeyError):
        load_genre("nope")


def test_entity_translation_never_overridden():
    b = StoryBible()
    assert b.add_entity(Entity("Alice", "Ali", "character", ["Al"]))
    assert not b.add_entity(Entity("Alice", "A-li-xơ", "character", ["Ally"], note="nữ chính"))
    e = b.find("Ally")
    assert e.target == "Ali" and e.note == "nữ chính"
    assert b.find("al").source == "Alice"  # không phân biệt hoa thường khi tra
    # tên mới trùng alias của tên cũ cũng không tạo bản dịch thứ hai
    assert not b.add_entity(Entity("Al", "Anh Al"))
    assert len(b.entities) == 1


def test_bible_roundtrip_and_prompt(tmp_path):
    b = StoryBible(tmp_path / "b.json")
    b.add_entity(Entity("Zed", "Zed", "character"))
    b.add_entity(Entity("Hogwarts", "Trường Hogwarts", "place"))
    b.add_addressing(Addressing("Zed", "Hogwarts", "ta", "ngươi", "khi tức giận"))
    assert not b.add_addressing(Addressing("Zed", "Hogwarts", "ta", "ngươi"))
    b.set_section("c1", SectionNotes("Chương 1", "Zed đến.", ["Zed bị thương"]))
    b.save()
    b2 = StoryBible.load(tmp_path / "b.json")
    assert b2.to_dict() == b.to_dict()
    prompt = b2.prompt_block()
    assert "- Hogwarts → Trường Hogwarts" in prompt
    assert 'Zed → Hogwarts: xưng "ta" / gọi "ngươi" — khi tức giận' in prompt
    assert "xưng hô" not in b2.prompt_block(track_addressing=False)


def test_story_so_far_only_previous_sections():
    b = StoryBible()
    for i in range(1, 4):
        b.set_section(f"c{i}", SectionNotes(f"Chương {i}", f"tóm tắt {i}", [f"sự kiện {i}"]))
    text = b.story_so_far(["c1", "c2", "c3"], "c3")
    assert "tóm tắt 1" in text and "tóm tắt 2" in text and "tóm tắt 3" not in text
    assert text.index("sự kiện 1") < text.index("sự kiện 2")
    assert b.story_so_far(["c1", "c2", "c3"], "c1") == ""


def test_contains_word_boundaries_and_cjk():
    assert contains("Ann went", "Ann")
    assert not contains("Annabel went", "Ann")
    assert not contains("hope is", "Hope")
    assert contains("张无忌说", "张无忌")


def test_check_chunk_detects_untranslated_and_missing():
    b = StoryBible()
    b.add_entity(Entity("Hogwarts", "Trường Hogwarts", "place"))
    b.add_entity(Entity("Kingslayer", "Kẻ Giết Vua", "title"))
    b.add_entity(Entity("Bob", "Bob", "character"))
    ok = check_chunk(b, "s", 0, "Hogwarts, Bob", "Trường Hogwarts, Bob")
    assert ok == []
    issues = check_chunk(b, "s", 0, "The Kingslayer and Bob", "Kingslayer và anh ấy")
    assert {(i.entity.source, i.problem) for i in issues} == {("Kingslayer", "untranslated"), ("Bob", "missing")}
