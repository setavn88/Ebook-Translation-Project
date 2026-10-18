import json
import zipfile
from pathlib import Path

from booktranslator import cli
from booktranslator.bible import Entity, StoryBible
from booktranslator.cache import TranslationCache
from booktranslator.document import load_document
from booktranslator.genres import BUILTIN as GENRES
from booktranslator.pipeline import preview_document
from booktranslator.styles import BUILTIN as STYLES, load_style
from booktranslator.stylelearn import learn_style
from booktranslator.translator import Translator, TranslatorConfig

from fakes import FakeProvider, default_responder, draft_of, fake_chain, reply

from booktranslator.providers import Completion, ProviderChain


def make(responder=None, style="lylan", genre="comic", tmp_path=None, **config):
    chain, provider = fake_chain(responder)
    cache = TranslationCache(tmp_path / "c.jsonl" if tmp_path else None)
    t = Translator(TranslatorConfig(**config), chain, GENRES[genre], STYLES[style], StoryBible(), cache)
    return t, provider


def test_lylan_style_prompt():
    block = STYLES["lylan"].prompt_block()
    assert "Hẻm Xéo" in block and "bồ" in block and "<example>" in block


def test_target_only_examples_render_without_source():
    from booktranslator.styles import StyleProfile

    block = StyleProfile("k", "n", "d", "p", examples=(("", "Văn mẫu."),)).prompt_block()
    assert "<translation>Văn mẫu.</translation>" in block and "<source>" not in block


def test_custom_style_inherits_and_appends_examples(tmp_path):
    path = tmp_path / "mine.json"
    path.write_text(json.dumps({"base": "lylan", "examples": [{"source": "Hi.", "target": "Chào."}]}))
    v = load_style(path=path)
    assert v.key == "mine" and v.principles == STYLES["lylan"].principles
    assert v.examples[-1] == ("Hi.", "Chào.") and len(v.examples) == len(STYLES["lylan"].examples) + 1
    path.write_text(json.dumps({"base": "lylan", "replace_examples": True, "examples": [{"target": "Chỉ văn mẫu."}]}))
    assert load_style(path=path).examples == (("", "Chỉ văn mẫu."),)


def test_example_style_file_is_valid():
    v = load_style(path=Path(__file__).parent.parent / "examples" / "phong-cach-mau.json")
    assert len(v.examples) > len(STYLES["lylan"].examples)


def test_comic_genre_rules():
    style = GENRES["comic"].style
    assert "IN HOA" in style and "footnote" in style


def test_translate_then_polish():
    def responder(system, user):
        if draft_of(user) is not None:
            return reply("<translation>\nBản đã biên tập\n</translation>")
        return default_responder(system, user)

    t, p = make(responder)
    assert t.translate_chunk("He smiled happily.") == "Bản đã biên tập"
    draft_call, polish_call = p.calls
    assert "dịch giả" in draft_call["system"] and "một cách" in draft_call["system"] and "Hẻm Xéo" in draft_call["system"]
    assert "biên tập viên" in polish_call["system"] and "GIỮ NGUYÊN mọi tên riêng" in polish_call["system"]
    assert "<source>\nHe smiled happily.\n</source>" in polish_call["user"]
    assert "<draft>\nHE SMILED HAPPILY.\n</draft>" in polish_call["user"]


def test_polish_falls_back_to_draft_on_bad_reply():
    def responder(system, user):
        return reply("", "refusal") if draft_of(user) is not None else default_responder(system, user)

    t, _ = make(responder)
    assert t.translate_chunk("hello") == "HELLO"


def test_no_polish_is_single_call():
    t, p = make(polish=False)
    t.translate_chunk("hello")
    assert len(p.calls) == 1


def test_naturalness_rules_only_for_vietnamese():
    t, p = make(target_lang="English", polish=False)
    t.translate_chunk("xin chào")
    assert "văn dịch" not in p.calls[0]["system"]


def test_style_change_invalidates_cache(tmp_path):
    t, _ = make(tmp_path=tmp_path, polish=False)
    t.translate_chunk("hello")
    t2, p2 = make(style="natural", tmp_path=tmp_path, polish=False)
    t2.translate_chunk("hello")
    assert len(p2.calls) == 1


def test_preview_document(tmp_path):
    src = tmp_path / "s.md"
    src.write_text("# Chapter 1\n\n" + "Alice laughed. " * 100 + "\n\nSecond part.\n\nThird.", encoding="utf-8")
    t, p = make(tmp_path=tmp_path)
    t.bible.add_entity(Entity("Alice", "Ali", "character"))
    md = preview_document(t, load_document(src, chunk_chars=800), chunks=2)
    assert "# Dịch thử: Chapter 1" in md and "Alice → **Ali**" in md
    assert md.count("### Bản gốc") == 2 and md.count("### Bản biên tập") == 2
    assert len(p.analysis_calls) == 1


def test_cli_preview_then_translate_reuses_drafts(tmp_path, monkeypatch):
    chain, provider = fake_chain()
    monkeypatch.setattr(cli, "build_chain", lambda *a, **kw: chain)
    src = tmp_path / "book.md"
    src.write_text("# Chapter 1\n\n" + "Alice laughed. " * 100, encoding="utf-8")
    args = [str(src), "-g", "comic", "-p", "lylan"]
    assert cli.main(["dich-thu", *args]) == 0
    assert (tmp_path / "book.vi.md.preview.md").exists()
    n = len(provider.calls)
    assert cli.main(["dich", *args]) == 0
    assert len(provider.calls) == n  # nháp và bản biên tập đều lấy từ cache
    assert (tmp_path / "book.vi.md").exists()


# ---- học phong cách từ sách tham chiếu ----------------------------------


class StyleLearner(FakeProvider):
    """Trả kết quả dóng hàng hoặc tổng hợp tùy theo prompt."""

    def complete(self, system, user, **kw):
        self.analysis_calls.append({"system": system, "user": user, **kw})
        if "<original>" in user:
            data = {
                "pairs": [{"source": "He grinned.", "target": "Nó cười toe.", "why": "dịch thoát"}],
                "names": [{"source": "Crooked Lane", "target": "Ngõ Quẹo"}],
            }
        else:
            data = {
                "description": "Giọng hóm hỉnh",
                "principles": "- Câu ngắn",
                "naming": "- Việt hóa tên có nghĩa",
                "addressing": "- tớ/bồ",
                "samples": ["Một đoạn văn mẫu."],
            }
        return Completion(json.dumps(data, ensure_ascii=False), "end", provider=self.id)


def _book(path: Path, paragraphs: list[str]):
    path.write_text("\n\n".join(f"# Chapter {i}\n\n{p * 120}" for i, p in enumerate(paragraphs, 1)), encoding="utf-8")
    return load_document(path)


def test_learn_style_with_original(tmp_path):
    vi = _book(tmp_path / "vi.md", [f"Đoạn văn tiếng Việt số {i}. " for i in range(6)])
    en = _book(tmp_path / "en.md", [f"English paragraph number {i}. " for i in range(6)])
    provider = StyleLearner()
    profile = learn_style(ProviderChain([provider]), vi, en, name="Lý Lan", key="lylan-hp", samples=3)
    assert len(provider.analysis_calls) == 4  # 3 lần dóng hàng + 1 lần tổng hợp
    assert profile.name == "Lý Lan" and profile.principles == "- Câu ngắn"
    assert ("He grinned.", "Nó cười toe.") in profile.examples
    assert "Crooked Lane → Ngõ Quẹo" in profile.naming
    synth = provider.analysis_calls[-1]["user"]
    assert "<aligned_pairs>" in synth and "<excerpt index=\"3\">" in synth


def test_learn_style_translation_only_uses_samples(tmp_path):
    vi = _book(tmp_path / "vi.md", ["Văn. "] * 3 + ["Đủ dài để học phong cách dịch. "] * 3)
    provider = StyleLearner()
    profile = learn_style(ProviderChain([provider]), vi, None, samples=2)
    assert len(provider.analysis_calls) == 1
    assert profile.examples == (("", "Một đoạn văn mẫu."),)


def test_cli_learn_style_roundtrip(tmp_path, monkeypatch):
    provider = StyleLearner()
    monkeypatch.setattr(cli, "build_chain", lambda *a, **kw: ProviderChain([provider]))
    vi = tmp_path / "hp_vi.md"
    _book(vi, ["Đủ dài để học phong cách dịch. "] * 3)
    out = tmp_path / "lylan-hp.json"
    assert cli.main(["hoc-phong-cach", str(vi), "-o", str(out), "--ten", "Lý Lan - HP"]) == 0
    profile = load_style(path=out)
    assert profile.name == "Lý Lan - HP" and profile.key == "lylan-hp"
    assert profile.examples == (("", "Một đoạn văn mẫu."),)
