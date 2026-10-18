"""Client Anthropic giả lập: "dịch" bằng cách viết hoa chữ (giữ nguyên thẻ HTML)."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

_SOURCE_RE = re.compile(r"<source>\n(.*)\n</source>", re.DOTALL)


def fake_translate(text: str) -> str:
    # Viết hoa phần chữ, không động vào thẻ HTML
    return re.sub(r"(<[^>]+>)|([^<]+)", lambda m: m.group(1) or m.group(2).upper(), text)


def _usage():
    return SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def make_message(text: str, stop_reason: str = "end_turn", **extra):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=_usage(),
        **extra,
    )


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
    def __init__(self, responder=None, glossary=None):
        self.calls: list[dict] = []
        self.responder = responder or self.default_responder
        self.glossary = glossary or {"terms": [{"source": "Alice", "target": "Ali"}]}

    @staticmethod
    def default_responder(kwargs):
        user = kwargs["messages"][-1]["content"]
        source = _SOURCE_RE.search(user).group(1)
        return make_message(f"<translation>\n{fake_translate(source)}\n</translation>")

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _Stream(self.responder(kwargs))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return make_message(json.dumps(self.glossary))


class FakeClient:
    def __init__(self, responder=None, glossary=None):
        self.messages = FakeMessages(responder, glossary)
        self.beta = SimpleNamespace(messages=self.messages)
