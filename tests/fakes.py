"""Nhà cung cấp mô hình giả lập.

"Dịch" = thay tên theo <so_tay> rồi viết hoa phần chữ (giữ nguyên thẻ HTML).
"Biên tập" = trả lại nguyên bản nháp. "Phân tích chương" = JSON cố định hoặc do test cung cấp.
"""

from __future__ import annotations

import json
import re

from booktranslator.providers import Completion, Provider, ProviderChain

_SOURCE_RE = re.compile(r"<source>\n(.*?)\n</source>", re.DOTALL)
_SO_TAY_RE = re.compile(r"<so_tay>\n(.*?)\n</so_tay>", re.DOTALL)
_DRAFT_RE = re.compile(r"<draft>\n(.*)\n</draft>", re.DOTALL)
_CHAPTER_RE = re.compile(r"<chapter title=\"(.*?)\">\n(.*)\n</chapter>", re.DOTALL)

DEFAULT_ANALYSIS = {
    "summary": "Ali mở cửa.",
    "events": ["Ali rời nhà"],
    "entities": [
        {"source": "Alice", "target": "Ali", "type": "character", "aliases": ["Al"], "gender": "nữ", "note": "nhân vật chính"}
    ],
    "addressing": [{"speaker": "Alice", "listener": "Bob", "self_term": "em", "other_term": "anh", "context": "người yêu"}],
}


def names_in(user_message: str) -> dict[str, str]:
    m = _SO_TAY_RE.search(user_message)
    if not m:
        return {}
    out = {}
    for line in m.group(1).split("[xưng hô]")[0].splitlines():
        if line.startswith("- ") and " → " in line:
            src, dst = line[2:].split(" → ", 1)
            out[src] = dst.split(" (", 1)[0]
    return out


def source_of(user_message: str) -> str:
    return _SOURCE_RE.search(user_message).group(1)


def draft_of(user_message: str) -> str | None:
    m = _DRAFT_RE.search(user_message)
    return m.group(1) if m else None


def fake_translate(text: str, mapping: dict[str, str] | None = None) -> str:
    placeholders = {}
    for i, (src, dst) in enumerate(sorted((mapping or {}).items(), key=lambda kv: -len(kv[0]))):
        ph = f"\x00{i}\x00"
        text = text.replace(src, ph)
        placeholders[ph] = dst
    text = re.sub(r"(<[^>]+>)|([^<]+)", lambda m: m.group(1) or m.group(2).upper(), text)
    for ph, dst in placeholders.items():
        text = text.replace(ph, dst)
    return text


def translation_reply(translation: str, updates: dict | None = None) -> str:
    updates = updates if updates is not None else {"entities": [], "addressing": []}
    return f"<translation>\n{translation}\n</translation>\n<bible_updates>{json.dumps(updates, ensure_ascii=False)}</bible_updates>"


_FIX_RE = re.compile(r"<names>\n(.*?)\n</names>.*<translation_to_fix>\n(.*)\n</translation_to_fix>", re.DOTALL)


def fix_names_reply(user: str) -> Completion | None:
    """Bước soát tên: thay tên gốc / biến thể bằng đúng bản dịch trong danh sách."""
    m = _FIX_RE.search(user)
    if not m:
        return None
    text = m.group(2)
    for line in m.group(1).splitlines():
        src, dst = line[2:].split(" → ", 1)
        text = re.sub(re.escape(src), dst, text, flags=re.IGNORECASE)
    return Completion(f"<translation>\n{text}\n</translation>", "end")


def default_responder(system: str, user: str) -> Completion:
    if (fixed := fix_names_reply(user)) is not None:
        return fixed
    draft = draft_of(user)
    if draft is not None:  # bước biên tập: giữ nguyên bản nháp
        return Completion(f"<translation>\n{draft}\n</translation>", "end")
    return Completion(translation_reply(fake_translate(source_of(user), names_in(user))), "end")


def reply(text: str, stop_reason: str = "end") -> Completion:
    return Completion(text, stop_reason, "x" if stop_reason == "refusal" else None)


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, responder=None, analyzer=None, model="m"):
        super().__init__(model)
        self.calls: list[dict] = []
        self.analysis_calls: list[dict] = []
        self.responder = responder or default_responder
        self.analyzer = analyzer or (lambda title, text: DEFAULT_ANALYSIS)

    def complete(self, system, user, *, effort="medium", json_schema=None, max_tokens=None):
        call = {"system": system, "user": user, "effort": effort, "json_schema": json_schema}
        if json_schema is not None:
            self.analysis_calls.append(call)
            m = _CHAPTER_RE.search(user)
            data = self.analyzer(*m.groups()) if m else {"pairs": [], "names": []}
            return Completion(json.dumps(data, ensure_ascii=False), "end", provider=self.id)
        self.calls.append(call)
        result = self.responder(system, user)
        result.provider = self.id
        return result


def fake_chain(responder=None, analyzer=None) -> tuple[ProviderChain, FakeProvider]:
    provider = FakeProvider(responder, analyzer)
    return ProviderChain([provider]), provider
