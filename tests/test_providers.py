from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import openai
import pytest

from booktranslator.providers import (
    FALLBACK_BETA,
    ClaudeCodeProvider,
    ClaudeProvider,
    OpenAICompatProvider,
    ProviderChain,
    ProviderUnavailable,
    parse_spec,
)

from fakes import FakeProvider, reply


# ---- Claude -----------------------------------------------------------


class _Stream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def claude_client(message=None, error=None):
    calls = []

    def stream(**kw):
        calls.append(kw)
        if error:
            raise error
        return _Stream(message)

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream))), calls


def claude_message(text="hi", stop_reason="end_turn"):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=90, cache_creation_input_tokens=0)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason, usage=usage, stop_details=None)


def test_claude_request_shape_and_usage():
    client, calls = claude_client(claude_message())
    p = ClaudeProvider("claude-opus-5-5", client=client)
    result = p.complete("SYS", "USER", effort="high", json_schema={"type": "object"})
    kw = calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert kw["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert result.text == "hi" and result.stop_reason == "end"
    assert p.usage.input_tokens == 100 and p.usage.cached_input_tokens == 90


def test_claude_no_fallback_and_stop_reasons():
    client, calls = claude_client(claude_message(stop_reason="max_tokens"))
    p = ClaudeProvider(client=client, use_fallback=False)
    assert p.complete("s", "u").stop_reason == "max_tokens"
    assert "fallbacks" not in calls[0] and "betas" not in calls[0]
    client, _ = claude_client(claude_message("", "refusal"))
    assert ClaudeProvider(client=client).complete("s", "u").stop_reason == "refusal"


def _status_error(cls, status, body="err"):
    request = httpx.Request("POST", "https://example.com")
    return cls(body, response=httpx.Response(status, request=request), body=None)


def test_claude_rate_limit_means_unavailable():
    client, _ = claude_client(error=_status_error(anthropic.RateLimitError, 429))
    with pytest.raises(ProviderUnavailable):
        ClaudeProvider(client=client).complete("s", "u")
    client, _ = claude_client(error=_status_error(anthropic.BadRequestError, 400, "Your credit balance is too low"))
    with pytest.raises(ProviderUnavailable):
        ClaudeProvider(client=client).complete("s", "u")


# ---- OpenAI-compatible ------------------------------------------------


def openai_client(finish_reason="stop", content="xin chào", error=None):
    calls = []

    def create(**kw):
        calls.append(kw)
        if error:
            raise error
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, prompt_cache_hit_tokens=80)
        choice = SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))
        return SimpleNamespace(choices=[choice], usage=usage)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


def test_deepseek_request_shape():
    client, calls = openai_client()
    p = OpenAICompatProvider("deepseek", client=client)
    result = p.complete("SYS", "USER", json_schema={"type": "object"})
    kw = calls[0]
    assert kw["model"] == "deepseek-chat" and kw["max_tokens"] == 8192
    assert kw["response_format"] == {"type": "json_object"}
    assert kw["messages"][0]["role"] == "system" and "schema" in kw["messages"][0]["content"]
    assert result.text == "xin chào" and p.usage.cached_input_tokens == 80


def test_openai_uses_reasoning_effort_and_completion_tokens():
    client, calls = openai_client(finish_reason="length")
    p = OpenAICompatProvider("openai", "gpt-5", client=client)
    assert p.complete("s", "u", effort="max").stop_reason == "max_tokens"
    assert calls[0]["reasoning_effort"] == "high" and "max_completion_tokens" in calls[0]
    client, _ = openai_client(finish_reason="content_filter")
    assert OpenAICompatProvider("openai", client=client).complete("s", "u").stop_reason == "refusal"


def test_openai_quota_errors_mean_unavailable():
    client, _ = openai_client(error=_status_error(openai.RateLimitError, 429))
    with pytest.raises(ProviderUnavailable):
        OpenAICompatProvider("openai", client=client).complete("s", "u")
    client, _ = openai_client(error=_status_error(openai.APIStatusError, 402))
    with pytest.raises(ProviderUnavailable):
        OpenAICompatProvider("deepseek", client=client).complete("s", "u")


def test_missing_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ProviderUnavailable):
        OpenAICompatProvider("deepseek")


# ---- chuỗi dự phòng ---------------------------------------------------


class Exhausted(FakeProvider):
    def complete(self, *a, **kw):
        self.calls.append(kw)
        raise ProviderUnavailable("hết quota")


def test_chain_switches_permanently_when_provider_exhausted():
    first, second = Exhausted(model="a"), FakeProvider(lambda s, u: reply("ok"), model="b")
    chain = ProviderChain([first, second])
    assert chain.complete("s", "u").text == "ok"
    assert chain.complete("s", "u").text == "ok"
    assert len(first.calls) == 1  # không thử lại cái đã hết quota
    assert chain.active is second and chain.id == "fake:a"  # id ổn định cho cache


def test_chain_retries_refusal_on_next_provider_only_for_that_request():
    first = FakeProvider(lambda s, u: reply("", "refusal") if "nhạy cảm" in u else reply("A"), model="a")
    second = FakeProvider(lambda s, u: reply("B"), model="b")
    chain = ProviderChain([first, second])
    assert chain.complete("s", "đoạn nhạy cảm").text == "B"
    assert chain.complete("s", "đoạn thường").text == "A"
    assert chain.active is first


def test_chain_raises_when_all_exhausted():
    with pytest.raises(ProviderUnavailable):
        ProviderChain([Exhausted(), Exhausted()]).complete("s", "u")


def test_parse_spec():
    assert parse_spec("claude, deepseek:deepseek-reasoner,chatgpt") == [
        ("claude", None), ("deepseek", "deepseek-reasoner"), ("openai", None)
    ]
    with pytest.raises(ValueError):
        parse_spec("gemini")


def test_claude_missing_key_is_unavailable():
    client, _ = claude_client(error=TypeError('"Could not resolve authentication method. Expected one of api_key"'))
    with pytest.raises(ProviderUnavailable):
        ClaudeProvider(client=client).complete("s", "u")


# ---- Claude Code (claude -p) ------------------------------------------

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
system = open(args[args.index("--system-prompt-file") + 1], encoding="utf-8").read()
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "limit":
    print(json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached"}))
elif mode == "crash":
    sys.stderr.write("boom")
    sys.exit(3)
else:
    payload = {"args": args, "prompt": prompt, "system": system, "has_key": "ANTHROPIC_API_KEY" in os.environ}
    reply = {
        "type": "result", "is_error": False, "stop_reason": "end_turn",
        "result": "Đây là JSON: " + json.dumps(payload),
        "usage": {"input_tokens": 3, "cache_read_input_tokens": 7, "output_tokens": 4},
    }
    if "--json-schema" in args:
        reply["structured_output"] = payload
    print(json.dumps(reply))
"""


@pytest.fixture
def fake_claude(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text(FAKE_CLAUDE)
    exe.chmod(0o755)
    return str(exe)


def test_claude_code_call(fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    p = ClaudeCodeProvider("claude-sonnet-5-5", executable=fake_claude)
    result = p.complete("HỆ THỐNG", "Dịch: hello", effort="high", json_schema={"type": "object"})
    data = __import__("json").loads(result.text)
    args = data["args"]
    assert args[:3] == ["-p", "--output-format", "json"]
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--model") + 1] == "claude-sonnet-5-5" and args[args.index("--effort") + 1] == "high"
    assert data["prompt"] == "Dịch: hello" and data["system"] == "HỆ THỐNG"
    assert __import__("json").loads(args[args.index("--json-schema") + 1]) == {"type": "object"}
    assert data["has_key"] is False  # không để Claude Code dùng API key → không tính tiền API
    assert p.usage.input_tokens == 10 and p.usage.cached_input_tokens == 7


def test_claude_code_limit_and_crash_mean_unavailable(fake_claude, monkeypatch):
    p = ClaudeCodeProvider(executable=fake_claude)
    monkeypatch.setenv("FAKE_MODE", "limit")
    with pytest.raises(ProviderUnavailable, match="usage limit"):
        p.complete("s", "u")
    monkeypatch.setenv("FAKE_MODE", "crash")
    with pytest.raises(ProviderUnavailable, match="boom"):
        p.complete("s", "u")


def test_claude_code_not_installed(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ProviderUnavailable):
        ClaudeCodeProvider()


def test_parse_spec_claude_code_alias():
    assert parse_spec("cc,claude-code:claude-opus-5-5") == [("claude-code", None), ("claude-code", "claude-opus-5-5")]


def test_find_claude_uses_env_and_common_locations(tmp_path, monkeypatch):
    from booktranslator.providers import find_claude

    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("BT_CLAUDE_PATH", raising=False)
    assert find_claude() is None or not find_claude().startswith(str(tmp_path))
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "claude.exe").write_text("")
    assert find_claude() == str(local / "claude.exe")  # chỗ trình cài Windows hay đặt, chưa có trong PATH
    custom = tmp_path / "khac" / "claude"
    custom.parent.mkdir()
    custom.write_text("")
    monkeypatch.setenv("BT_CLAUDE_PATH", f'"{custom}"')
    assert find_claude() == str(custom)


def test_check_claude_code(fake_claude, monkeypatch):
    from booktranslator.providers import check_claude_code

    monkeypatch.setenv("BT_CLAUDE_PATH", fake_claude)
    ok, msg = check_claude_code()
    assert ok and fake_claude in msg
    monkeypatch.setenv("FAKE_MODE", "limit")
    ok, msg = check_claude_code()
    assert not ok and "usage limit" in msg
