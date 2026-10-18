"""Client Anthropic giả lập.

"Dịch" = thay tên theo <names_in_passage> rồi viết hoa phần chữ (giữ nguyên thẻ HTML).
Phân tích chương = trả JSON cố định hoặc do test cung cấp.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

_SOURCE_RE = re.compile(r"<source>\n(.*)\n</source>", re.DOTALL)
_NAMES_RE = re.compile(r"<names_in_passage>\n.*?\n(.*?)\n</names_in_passage>", re.DOTALL)
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
    m = _NAMES_RE.search(user_message)
    if not m:
        return {}
    pairs = (line[2:].split(" → ", 1) for line in m.group(1).splitlines() if line.startswith("- "))
    return dict(pairs)


def source_of(user_message: str) -> str:
    return _SOURCE_RE.search(user_message).group(1)


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


def _usage():
    return SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def make_message(text: str, stop_reason: str = "end_turn", **extra):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason, usage=_usage(), **extra
    )


def translation_reply(translation: str, updates: dict | None = None) -> str:
    updates = updates if updates is not None else {"entities": [], "addressing": []}
    return f"<translation>\n{translation}\n</translation>\n<bible_updates>{json.dumps(updates, ensure_ascii=False)}</bible_updates>"


def default_responder(kwargs):
    user = kwargs["messages"][-1]["content"]
    return make_message(translation_reply(fake_translate(source_of(user), names_in(user))))


class _Stream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class FakeMessages:
    def __init__(self, responder=None, analyzer=None):
        self.calls: list[dict] = []
        self.analysis_calls: list[dict] = []
        self.responder = responder or default_responder
        self.analyzer = analyzer or (lambda title, text: DEFAULT_ANALYSIS)

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _Stream(self.responder(kwargs))

    def create(self, **kwargs):
        self.analysis_calls.append(kwargs)
        title, text = _CHAPTER_RE.search(kwargs["messages"][0]["content"]).groups()
        return make_message(json.dumps(self.analyzer(title, text), ensure_ascii=False))


class FakeClient:
    def __init__(self, responder=None, analyzer=None):
        self.messages = FakeMessages(responder, analyzer)
        self.beta = SimpleNamespace(messages=self.messages)
