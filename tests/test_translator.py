import pytest

from booktranslator.bible import Entity, SectionNotes, StoryBible
from booktranslator.cache import TranslationCache
from booktranslator.document import Section
from booktranslator.genres import BUILTIN
from booktranslator.translator import FALLBACK_BETA, TranslationError, Translator, TranslatorConfig

from fakes import FakeClient, default_responder, make_message, translation_reply


def make_translator(tmp_path=None, responder=None, analyzer=None, genre="general", bible=None, **config):
    client = FakeClient(responder, analyzer)
    cache = TranslationCache(tmp_path / "c.jsonl" if tmp_path else None)
    t = Translator(TranslatorConfig(**config), BUILTIN[genre], bible or StoryBible(), client=client, cache=cache)
    return t, client


def test_request_shape_uses_genre_and_bible():
    bible = StoryBible()
    bible.add_entity(Entity("Alice", "Ali", "character"))
    t, client = make_translator(genre="romance", bible=bible, style="giọng trẻ trung")
    assert t.translate_chunk("hello Alice") == "HELLO Ali"
    kw = client.messages.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    rules_block, bible_block = kw["system"]
    rules = rules_block["text"]
    assert "Lãng mạn" in rules and "XƯNG HÔ" in rules and "giọng trẻ trung" in rules
    assert "- Alice → Ali" in bible_block["text"]
    assert bible_block["cache_control"] == {"type": "ephemeral"}
    assert "<names_in_passage>" in kw["messages"][0]["content"]


def test_different_genres_give_different_rules():
    rules = {}
    for g in ("scifi", "fantasy", "wuxia"):
        t, client = make_translator(genre=g)
        t.translate_chunk("x")
        rules[g] = client.messages.calls[0]["system"][0]["text"]
    assert "lỗ sâu" in rules["scifi"]
    assert "Chúa tể Hắc ám" in rules["fantasy"]
    assert "Hán Việt" in rules["wuxia"]


def test_no_fallback_option():
    t, client = make_translator(use_fallback=False)
    t.translate_chunk("hi")
    assert "fallbacks" not in client.messages.calls[0] and "betas" not in client.messages.calls[0]


def test_story_so_far_and_chapter_summary_in_message():
    bible = StoryBible()
    bible.set_section("c1", SectionNotes("Chương 1", "Bob chết.", ["Bob đã chết"]))
    bible.set_section("c2", SectionNotes("Chương 2", "Đám tang Bob.", []))
    t, client = make_translator(bible=bible)
    t.translate_chunk("text", section_id="c2", order=["c1", "c2"])
    user = client.messages.calls[0]["messages"][0]["content"]
    assert "<story_so_far>" in user and "Bob đã chết" in user
    assert "<current_chapter>\nChương 2: Đám tang Bob." in user


def test_bible_updates_add_new_names_but_never_override(tmp_path):
    bible = StoryBible(tmp_path / "bible.json")
    bible.add_entity(Entity("Alice", "Ali", "character"))
    updates = {
        "entities": [{"source": "Alice", "target": "Alít"}, {"source": "Gandor", "target": "Gan-đo", "type": "place"}],
        "addressing": [{"speaker": "Alice", "listener": "Gandor", "self_term": "ta", "other_term": "ngươi", "context": ""}],
    }
    t, _ = make_translator(responder=lambda kw: make_message(translation_reply("ok", updates)), bible=bible)
    t.translate_chunk("Alice in Gandor")
    assert bible.find("Alice").target == "Ali"
    assert bible.find("Gandor").target == "Gan-đo"
    assert len(bible.addressing) == 1
    assert StoryBible.load(tmp_path / "bible.json").find("Gandor") is not None  # đã lưu xuống đĩa


def test_invalid_bible_updates_are_ignored():
    reply = "<translation>\nok\n</translation><bible_updates>{not json</bible_updates>"
    t, _ = make_translator(responder=lambda kw: make_message(reply))
    assert t.translate_chunk("hi") == "ok"


def test_cache_avoids_second_call_and_persists(tmp_path):
    t, client = make_translator(tmp_path)
    t.translate_chunk("one")
    t.translate_chunk("one")
    assert len(client.messages.calls) == 1
    t2, client2 = make_translator(tmp_path)
    assert t2.translate_chunk("one") == "ONE"
    assert client2.messages.calls == []
    # đổi thể loại thì dịch lại
    t3, client3 = make_translator(tmp_path, genre="fantasy")
    t3.translate_chunk("one")
    assert len(client3.messages.calls) == 1


def test_reminder_bypasses_cache(tmp_path):
    t, client = make_translator(tmp_path)
    t.translate_chunk("one")
    t.translate_chunk("one", reminder="dùng đúng tên")
    assert len(client.messages.calls) == 2
    assert "<reminder>" in client.messages.calls[1]["messages"][0]["content"]


def test_cache_ignores_truncated_line(tmp_path):
    path = tmp_path / "c.jsonl"
    TranslationCache(path).put("k", "v")
    with path.open("a") as f:
        f.write('{"key": "broken"')
    assert TranslationCache(path).get("k") == "v"
    assert TranslationCache(path, load=False).get("k") is None


def test_refusal_raises():
    t, _ = make_translator(responder=lambda kw: make_message("", "refusal", stop_details=None))
    with pytest.raises(TranslationError):
        t.translate_chunk("text")


def test_max_tokens_splits_chunk_and_retries():
    long_chunk = "\n\n".join(f"para {i} " + "x" * 300 for i in range(6))

    def responder(kw):
        if long_chunk in kw["messages"][0]["content"]:
            return make_message("<translation>\npartial", "max_tokens")
        return default_responder(kw)

    t, client = make_translator(responder=responder)
    assert t.translate_chunk(long_chunk) == long_chunk.upper()
    assert len(client.messages.calls) >= 3


def test_missing_tags_on_short_chunk_uses_raw_reply():
    t, _ = make_translator(responder=lambda kw: make_message("XIN CHÀO"))
    assert t.translate_chunk("hello") == "XIN CHÀO"


def test_analyze_section_fills_bible_and_is_cached(tmp_path):
    t, client = make_translator(tmp_path, genre="fantasy")
    section = Section("c1", "Chapter 1", "text", ["Alice opened the door and walked out into the cold morning."])
    notes = t.analyze_section(section)
    assert notes.summary == "Ali mở cửa." and notes.events == ["Ali rời nhà"]
    assert t.bible.find("Al").target == "Ali"
    assert t.bible.addressing[0].other_term == "anh"
    kw = client.messages.analysis_calls[0]
    schema = kw["output_config"]["format"]["schema"]
    assert schema["properties"]["entities"]["items"]["properties"]["type"]["enum"] == list(BUILTIN["fantasy"].entity_types)
    assert "phép thuật" in kw["system"][0]["text"]

    t2, client2 = make_translator(tmp_path, genre="fantasy")
    t2.analyze_section(section)
    assert client2.messages.analysis_calls == []


def test_analyze_skips_empty_sections():
    t, client = make_translator()
    assert t.analyze_section(Section("c", "Cover", "html", ["<img src='a.png'/>"], "")) is None
    assert client.messages.analysis_calls == []
