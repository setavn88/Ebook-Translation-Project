import pytest

from booktranslator.cache import TranslationCache
from booktranslator.translator import FALLBACK_BETA, TranslationError, Translator, TranslatorConfig

from fakes import FakeClient, FakeMessages, make_message


def make_translator(tmp_path=None, responder=None, **config):
    client = FakeClient(responder)
    cache = TranslationCache(tmp_path / "c.jsonl" if tmp_path else None)
    return Translator(TranslatorConfig(**config), client=client, cache=cache), client


def test_request_shape():
    t, client = make_translator(glossary={"Bob": "Bốp", "Alice": "Ali"}, style="giọng trẻ trung")
    assert t.translate_chunk("hello") == "HELLO"
    kw = client.messages.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["cache_control"] == {"type": "ephemeral"}
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert "Alice → Ali\n- Bob → Bốp" in kw["system"]
    assert "giọng trẻ trung" in kw["system"]


def test_no_fallback_option():
    t, client = make_translator(use_fallback=False)
    t.translate_chunk("hi")
    assert "fallbacks" not in client.messages.calls[0]
    assert "betas" not in client.messages.calls[0]


def test_cache_avoids_second_call_and_persists(tmp_path):
    t, client = make_translator(tmp_path)
    t.translate_chunk("one")
    t.translate_chunk("one")
    assert len(client.messages.calls) == 1

    t2, client2 = make_translator(tmp_path)
    assert t2.translate_chunk("one") == "ONE"
    assert client2.messages.calls == []


def test_cache_ignores_truncated_line(tmp_path):
    path = tmp_path / "c.jsonl"
    cache = TranslationCache(path)
    cache.put("k", "v")
    with path.open("a") as f:
        f.write('{"key": "broken"')
    assert TranslationCache(path).get("k") == "v"


def test_context_from_previous_chunk_is_sent():
    t, client = make_translator()
    out = t.translate_chunks(["first part", "second part"])
    assert out == "FIRST PART\n\nSECOND PART"
    second_user = client.messages.calls[1]["messages"][0]["content"]
    assert "<previous_source>\nfirst part\n</previous_source>" in second_user
    assert "<previous_translation>\nFIRST PART\n</previous_translation>" in second_user
    assert "<context>" not in client.messages.calls[0]["messages"][0]["content"]


def test_refusal_raises():
    t, _ = make_translator(responder=lambda kw: make_message("", "refusal", stop_details=None))
    with pytest.raises(TranslationError):
        t.translate_chunk("text")


def test_max_tokens_splits_chunk_and_retries():
    long_chunk = "\n\n".join(f"para {i} " + "x" * 300 for i in range(6))

    def responder(kw):
        user = kw["messages"][0]["content"]
        if long_chunk in user:
            return make_message("<translation>\npartial", "max_tokens")
        return FakeMessages.default_responder(kw)

    t, client = make_translator(responder=responder)
    out = t.translate_chunk(long_chunk)
    assert out == long_chunk.upper()
    assert len(client.messages.calls) >= 3


def test_missing_tags_on_short_chunk_uses_raw_reply():
    t, _ = make_translator(responder=lambda kw: make_message("XIN CHÀO"))
    assert t.translate_chunk("hello") == "XIN CHÀO"


def test_build_glossary():
    t, client = make_translator()
    assert t.build_glossary("Alice went home.") == {"Alice": "Ali"}
    kw = client.messages.calls[0]
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert "cache_control" not in kw
