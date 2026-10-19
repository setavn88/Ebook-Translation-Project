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


# ---- chờ hạn mức rồi dịch tiếp ------------------------------------------

from booktranslator.providers import PROBE_SECONDS, ProviderLimitReached, _TEMPORARY_RE, _limit_reset_time


class Flaky(FakeProvider):
    """Hết hạn mức `fails` lần đầu, sau đó dịch bình thường."""

    def __init__(self, fails, retry_in=None, model="a"):
        super().__init__(lambda s, u: reply("ok"), model=model)
        self.fails = fails
        self.retry_in = retry_in

    def complete(self, system, user, **kw):
        if self.fails > 0:
            self.fails -= 1
            import time as _t
            raise ProviderLimitReached("usage limit", _t.time() + self.retry_in if self.retry_in else None)
        return super().complete(system, user, **kw)


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr("booktranslator.providers.time.time", lambda: now[0])
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    return now, slept, sleep


def test_chain_waits_for_reset_time_then_continues(clock):
    now, slept, sleep = clock
    chain = ProviderChain([Flaky(1, retry_in=3600)], wait_hours=12, sleep=sleep)
    assert chain.complete("s", "u").text == "ok"
    assert slept == [3660]  # đúng giờ reset + 1 phút dự phòng


def test_chain_probes_every_15_minutes_without_reset_time(clock):
    now, slept, sleep = clock
    chain = ProviderChain([Flaky(3)], wait_hours=12, sleep=sleep)
    assert chain.complete("s", "u").text == "ok"
    assert slept == [PROBE_SECONDS] * 3


def test_chain_gives_up_after_max_wait(clock):
    now, slept, sleep = clock
    chain = ProviderChain([Flaky(10**6)], wait_hours=1, sleep=sleep)
    with pytest.raises(ProviderLimitReached):
        chain.complete("s", "u")
    assert sum(slept) == 3600


def test_chain_no_wait_stops_immediately(clock):
    now, slept, sleep = clock
    with pytest.raises(ProviderLimitReached):
        ProviderChain([Flaky(1)], wait_hours=0, sleep=sleep).complete("s", "u")
    assert slept == []


def test_chain_uses_fallback_before_waiting_and_returns_to_primary(clock):
    now, slept, sleep = clock
    primary, backup = Flaky(1, model="a"), Flaky(1, model="b")
    chain = ProviderChain([primary, backup], wait_hours=5, sleep=sleep)
    assert chain.complete("s", "u").text == "ok"
    assert len(slept) == 1 and chain.active is primary


def test_permanent_errors_do_not_wait(clock):
    now, slept, sleep = clock
    with pytest.raises(ProviderUnavailable) as err:
        ProviderChain([Exhausted()], wait_hours=12, sleep=sleep).complete("s", "u")
    assert not isinstance(err.value, ProviderLimitReached) and slept == []


def test_temporary_error_detection():
    assert _TEMPORARY_RE.search("Claude AI usage limit reached|1760000000")
    assert _TEMPORARY_RE.search("You've hit your limit · resets 3pm")
    assert not _TEMPORARY_RE.search("Failed to generate response")
    assert not _TEMPORARY_RE.search("Invalid API key · Please run /login")
    assert _limit_reset_time("Claude AI usage limit reached|1760000000") == 1760000000.0
    assert _limit_reset_time("hit your limit") is None


def test_claude_code_limit_is_temporary(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "limit")
    with pytest.raises(ProviderLimitReached):
        ClaudeCodeProvider(executable=fake_claude).complete("s", "u")


# ---- Codex (codex exec) ---------------------------------------------------

from booktranslator.providers import CodexProvider

FAKE_CODEX = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    if os.environ.get("FAKE_MODE") == "logged_out":
        print("Not logged in"); sys.exit(1)
    print("Logged in using ChatGPT"); sys.exit(0)
prompt = sys.stdin.read()
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "limit":
    print(json.dumps({"type": "error", "message": "You've hit your usage limit. Try again later."}))
    sys.exit(1)
if mode == "login":
    sys.stderr.write("Not logged in. Run codex login")
    sys.exit(1)
schema = None
if "--output-schema" in args:
    schema = json.load(open(args[args.index("--output-schema") + 1], encoding="utf-8"))
out = args[args.index("-o") + 1]
with open(out, "w", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "prompt": prompt, "schema": schema,
                        "has_key": "OPENAI_API_KEY" in os.environ}))
print(json.dumps({"type": "thread.started"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 20, "output_tokens": 9}}))
"""


@pytest.fixture
def fake_codex(tmp_path):
    exe = tmp_path / "codex"
    exe.write_text(FAKE_CODEX)
    exe.chmod(0o755)
    return str(exe)


def test_codex_call(fake_codex, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    p = CodexProvider("gpt-5-codex", executable=fake_codex)
    result = p.complete("QUY TẮC", "Dịch: hello", effort="max", json_schema={"type": "object"})
    data = __import__("json").loads(result.text)
    args = data["args"]
    assert args[0] == "exec" and "--ephemeral" in args and args[args.index("--sandbox") + 1] == "read-only"
    assert args[args.index("-m") + 1] == "gpt-5-codex" and 'model_reasoning_effort="high"' in args
    assert args[-1] == "-" and data["schema"] == {"type": "object"}
    assert "QUY TẮC" in data["prompt"] and "Dịch: hello" in data["prompt"] and "KHÔNG chạy lệnh" in data["prompt"]
    assert data["has_key"] is False  # dùng gói ChatGPT đã đăng nhập, không dùng API key
    assert p.usage.input_tokens == 50 and p.usage.cached_input_tokens == 20 and p.id == "codex:gpt-5-codex"


def test_codex_limit_and_login_errors(fake_codex, monkeypatch):
    p = CodexProvider(executable=fake_codex)
    assert p.id == "codex:mặc định"
    monkeypatch.setenv("FAKE_MODE", "limit")
    with pytest.raises(ProviderLimitReached):
        p.complete("s", "u")
    monkeypatch.setenv("FAKE_MODE", "login")
    with pytest.raises(ProviderUnavailable) as err:
        p.complete("s", "u")
    assert not isinstance(err.value, ProviderLimitReached) and "codex login" in str(err.value)


def test_codex_not_installed(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.delenv("BT_CODEX_PATH", raising=False)
    monkeypatch.setenv("APPDATA", "/khong-co")
    monkeypatch.setattr("os.path.isfile", lambda p: False)
    with pytest.raises(ProviderUnavailable):
        CodexProvider()


def test_codex_not_logged_in_fails_fast(fake_codex, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "logged_out")
    with pytest.raises(ProviderUnavailable, match="codex login"):
        CodexProvider(executable=fake_codex)


def test_find_codex_via_npm_prefix(tmp_path, monkeypatch):
    import booktranslator.providers as prov

    prefix = tmp_path / "custom-npm"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "codex").write_text("")
    npm = tmp_path / "npm"
    npm.write_text(f"#!/bin/sh\necho {prefix}\n")
    npm.chmod(0o755)
    real_which = prov.shutil.which
    monkeypatch.setattr(prov.shutil, "which", lambda name: str(npm) if name == "npm" else None)
    monkeypatch.delenv("BT_CODEX_PATH", raising=False)
    monkeypatch.setattr(prov, "_npm_prefix_cache", [])
    assert prov.find_codex() == str(prefix / "bin" / "codex")
