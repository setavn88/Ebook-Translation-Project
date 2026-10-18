import pytest

from booktranslator.bible import Addressing, Entity, SectionNotes, StoryBible
from booktranslator.cache import TranslationCache
from booktranslator.document import Section
from booktranslator.genres import BUILTIN
from booktranslator.translator import TranslationError, Translator, TranslatorConfig

from fakes import default_responder, fake_chain, reply, translation_reply


def make_translator(tmp_path=None, responder=None, analyzer=None, genre="general", bible=None, **config):
    chain, provider = fake_chain(responder, analyzer)
    cache = TranslationCache(tmp_path / "c.jsonl" if tmp_path else None)
    config.setdefault("polish", False)  # các test dưới đây chỉ kiểm bước dịch nháp
    t = Translator(TranslatorConfig(**config), chain, BUILTIN[genre], None, bible or StoryBible(), cache)
    return t, provider


def test_prompt_uses_genre_and_only_relevant_bible_entries():
    bible = StoryBible()
    bible.add_entity(Entity("Alice", "Ali", "character"))
    bible.add_entity(Entity("Zork", "Dọc", "place"))
    bible.add_addressing(Addressing("Alice", "Bob", "em", "anh"))
    bible.add_addressing(Addressing("Zork", "Mork", "ta", "ngươi"))
    t, p = make_translator(genre="romance", bible=bible, note="giọng trẻ trung")
    assert t.translate_chunk("hello Alice") == "HELLO Ali"
    call = p.calls[0]
    assert "Lãng mạn" in call["system"] and "XƯNG HÔ" in call["system"] and "giọng trẻ trung" in call["system"]
    assert "Ali" not in call["system"]  # prompt hệ thống cố định, không chứa sổ tay → luôn trúng cache
    assert "- Alice → Ali" in call["user"] and 'xưng "em"' in call["user"]
    assert "Zork" not in call["user"] and "Mork" not in call["user"]
    assert call["effort"] == "medium"


def test_system_prompt_identical_across_chunks():
    bible = StoryBible()
    bible.add_entity(Entity("Alice", "Ali", "character"))
    t, p = make_translator(bible=bible)
    t.translate_chunk("Alice one")
    t.translate_chunk("two")
    assert p.calls[0]["system"] == p.calls[1]["system"]


def test_different_genres_give_different_rules():
    rules = {}
    for g in ("scifi", "fantasy", "wuxia", "comic"):
        t, p = make_translator(genre=g)
        t.translate_chunk("x")
        rules[g] = p.calls[0]["system"]
    assert "lỗ sâu" in rules["scifi"]
    assert "Chúa tể Hắc ám" in rules["fantasy"]
    assert "Hán Việt" in rules["wuxia"]
    assert "IN HOA" in rules["comic"]


def test_story_so_far_and_chapter_summary_in_message():
    bible = StoryBible()
    bible.set_section("c1", SectionNotes("Chương 1", "Bob chết.", ["Bob đã chết"]))
    bible.set_section("c2", SectionNotes("Chương 2", "Đám tang Bob.", []))
    t, p = make_translator(bible=bible)
    t.translate_chunk("text", section_id="c2", order=["c1", "c2"])
    user = p.calls[0]["user"]
    assert "<story_so_far>" in user and "Bob đã chết" in user
    assert "<current_chapter>\nChương 2: Đám tang Bob." in user


def test_bible_updates_add_new_names_but_never_override(tmp_path):
    bible = StoryBible(tmp_path / "bible.json")
    bible.add_entity(Entity("Alice", "Ali", "character"))
    updates = {
        "entities": [{"source": "Alice", "target": "Alít"}, {"source": "Gandor", "target": "Gan-đo", "type": "place"}],
        "addressing": [{"speaker": "Alice", "listener": "Gandor", "self_term": "ta", "other_term": "ngươi", "context": ""}],
    }
    t, _ = make_translator(responder=lambda s, u: reply(translation_reply("ok", updates)), bible=bible)
    t.translate_chunk("Alice in Gandor")
    assert bible.find("Alice").target == "Ali"
    assert bible.find("Gandor").target == "Gan-đo"
    assert len(bible.addressing) == 1
    assert StoryBible.load(tmp_path / "bible.json").find("Gandor") is not None


def test_invalid_bible_updates_are_ignored():
    text = "<translation>\nok\n</translation><bible_updates>{not json</bible_updates>"
    t, _ = make_translator(responder=lambda s, u: reply(text))
    assert t.translate_chunk("hi") == "ok"


def test_cache_avoids_second_call_and_persists(tmp_path):
    t, p = make_translator(tmp_path)
    t.translate_chunk("one")
    t.translate_chunk("one")
    assert len(p.calls) == 1
    t2, p2 = make_translator(tmp_path)
    assert t2.translate_chunk("one") == "ONE"
    assert p2.calls == []
    t3, p3 = make_translator(tmp_path, genre="fantasy")  # đổi thể loại thì dịch lại
    t3.translate_chunk("one")
    assert len(p3.calls) == 1


def test_reminder_bypasses_cache(tmp_path):
    t, p = make_translator(tmp_path)
    t.translate_chunk("one")
    t.translate_chunk("one", reminder="dùng đúng tên")
    assert len(p.calls) == 2 and "<reminder>" in p.calls[1]["user"]


def test_cache_ignores_truncated_line(tmp_path):
    path = tmp_path / "c.jsonl"
    TranslationCache(path).put("k", "v")
    with path.open("a") as f:
        f.write('{"key": "broken"')
    assert TranslationCache(path).get("k") == "v"
    assert TranslationCache(path, load=False).get("k") is None


def test_refusal_raises():
    t, _ = make_translator(responder=lambda s, u: reply("", "refusal"))
    with pytest.raises(TranslationError):
        t.translate_chunk("text")


def test_max_tokens_splits_chunk_and_retries():
    long_chunk = "\n\n".join(f"para {i} " + "x" * 300 for i in range(6))

    def responder(system, user):
        if long_chunk in user:
            return reply("<translation>\npartial", "max_tokens")
        return default_responder(system, user)

    t, p = make_translator(responder=responder)
    assert t.translate_chunk(long_chunk) == long_chunk.upper()
    assert len(p.calls) >= 3


def test_missing_tags_on_short_chunk_uses_raw_reply():
    t, _ = make_translator(responder=lambda s, u: reply("XIN CHÀO"))
    assert t.translate_chunk("hello") == "XIN CHÀO"


def test_analyze_section_fills_bible_and_is_cached(tmp_path):
    t, p = make_translator(tmp_path, genre="fantasy")
    section = Section("c1", "Chapter 1", "text", ["Alice opened the door and walked out into the cold morning."])
    notes = t.analyze_section(section)
    assert notes.summary == "Ali mở cửa." and notes.events == ["Ali rời nhà"]
    assert t.bible.find("Al").target == "Ali"
    assert t.bible.addressing[0].other_term == "anh"
    call = p.analysis_calls[0]
    assert call["json_schema"]["properties"]["entities"]["items"]["properties"]["type"]["enum"] == list(BUILTIN["fantasy"].entity_types)
    assert "phép thuật" in call["system"] and call["effort"] == "low"
    t2, p2 = make_translator(tmp_path, genre="fantasy")
    t2.analyze_section(section)
    assert p2.analysis_calls == []


def test_analysis_sends_known_names_and_tolerates_sloppy_json():
    bible = StoryBible()
    bible.add_entity(Entity("Bob", "Bốp", "character"))
    sloppy = {"summary": "x", "entities": [{"source": "Eve", "target": "Ê-va"}, "rác"], "addressing": [{"speaker": "Eve"}]}
    t, p = make_translator(analyzer=lambda title, text: sloppy, bible=bible)
    t.analyze_section(Section("c", "C", "text", ["Eve and Bob walked a long, long way home."]))
    assert "<known_names>\n- Bob → Bốp" in p.analysis_calls[0]["user"]
    assert bible.find("Eve").target == "Ê-va"


def test_analyze_skips_empty_sections():
    t, p = make_translator()
    assert t.analyze_section(Section("c", "Cover", "html", ["<img src='a.png'/>"], "")) is None
    assert p.analysis_calls == []
