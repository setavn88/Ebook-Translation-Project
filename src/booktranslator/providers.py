"""Nhà cung cấp mô hình (Claude, DeepSeek, ChatGPT) và chuỗi dự phòng giữa chúng.

Mọi lời gọi mô hình trong dự án đi qua `Provider.complete(system, user, ...)`.
`ProviderChain` thử lần lượt: khi một nhà cung cấp hết quota / bị giới hạn /
mất kết nối thì chuyển hẳn sang nhà cung cấp kế tiếp; khi một nhà cung cấp từ
chối một đoạn thì thử đoạn đó ở nhà cung cấp kế tiếp.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)
_warned: set[str] = set()

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Model mặc định của từng nhà cung cấp; đổi bằng "tên:model" trong --providers.
DEFAULT_MODELS = {
    "claude": "claude-opus-5-5",
    "claude-code": "claude-opus-5-5",
    "deepseek": "deepseek-chat",
    "openai": "gpt-5",
}
API_KEY_ENV = {"claude": "ANTHROPIC_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY"}
_BASE_URLS = {"deepseek": "https://api.deepseek.com", "openai": None}
# Giới hạn output của từng nhà cung cấp (token). DeepSeek chat tối đa 8K.
_MAX_OUTPUT = {"claude": 64000, "deepseek": 8192, "openai": 32000}


class ProviderUnavailable(RuntimeError):
    """Nhà cung cấp tạm thời không dùng được (hết quota, rate limit, quá tải, sai key, mất mạng)."""


@dataclass
class Completion:
    text: str
    stop_reason: str  # "end" | "max_tokens" | "refusal"
    refusal_category: str | None = None
    provider: str = ""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    requests: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, input_tokens: int, output_tokens: int, cached: int = 0) -> None:
        with self._lock:
            self.requests += 1
            self.input_tokens += input_tokens or 0
            self.output_tokens += output_tokens or 0
            self.cached_input_tokens += cached or 0


class Provider:
    name: str = ""

    def __init__(self, model: str):
        self.model = model
        self.usage = Usage()

    @property
    def id(self) -> str:
        return f"{self.name}:{self.model}"

    def complete(
        self, system: str, user: str, *, effort: str = "medium", json_schema: dict | None = None, max_tokens: int | None = None
    ) -> Completion:
        raise NotImplementedError


# ---- Claude ----------------------------------------------------------


class ClaudeProvider(Provider):
    name = "claude"

    def __init__(self, model: str = DEFAULT_MODELS["claude"], client: Any = None, use_fallback: bool = True):
        super().__init__(model)
        if client is None:
            import anthropic

            try:
                client = anthropic.Anthropic(max_retries=5)
            except anthropic.AnthropicError as e:  # chưa có API key / chưa đăng nhập
                raise ProviderUnavailable(f"Claude: {e}") from e
        self.client = client
        self.use_fallback = use_fallback

    def complete(self, system, user, *, effort="medium", json_schema=None, max_tokens=None):
        import anthropic

        output_config: dict[str, Any] = {"effort": effort}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": min(max_tokens or _MAX_OUTPUT["claude"], _MAX_OUTPUT["claude"]),
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
            # Prompt hệ thống cố định suốt cuốn sách → được cache, các lần sau chỉ tính ~10% giá input.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
        }
        if self.use_fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            with self.client.beta.messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
        except (anthropic.RateLimitError, anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.InternalServerError, anthropic.APIConnectionError) as e:
            raise ProviderUnavailable(f"Claude: {type(e).__name__}: {e}") from e
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e).lower():
                raise ProviderUnavailable(f"Claude: hết credit ({e})") from e
            raise
        except TypeError as e:  # SDK chỉ phát hiện thiếu API key khi gửi request
            if "authentication" in str(e):
                raise ProviderUnavailable("Claude: chưa có ANTHROPIC_API_KEY") from e
            raise
        u = message.usage
        cached = (getattr(u, "cache_read_input_tokens", 0) or 0)
        self.usage.add((u.input_tokens or 0) + cached + (getattr(u, "cache_creation_input_tokens", 0) or 0), u.output_tokens, cached)
        text = "".join(b.text for b in message.content if b.type == "text")
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            return Completion(text, "refusal", getattr(details, "category", None) if details else None, self.id)
        return Completion(text, "max_tokens" if message.stop_reason == "max_tokens" else "end", provider=self.id)


# ---- Claude Code (dùng gói Claude Pro/Max đang đăng nhập) ------------

CLAUDE_PATH_ENV = "BT_CLAUDE_PATH"


def find_claude() -> str | None:
    """Tìm lệnh `claude`: biến BT_CLAUDE_PATH, PATH, rồi các chỗ cài đặt quen thuộc.

    Trên Windows, trình cài đặt hay đặt claude.exe ở %USERPROFILE%\\.local\\bin — thư mục này
    nhiều khi chưa có trong PATH của cửa sổ đang chạy, nên phải tự dò.
    """
    custom = os.environ.get(CLAUDE_PATH_ENV, "").strip().strip('"')
    if custom and os.path.isfile(custom):
        return custom
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "bin", "claude.exe"),
        os.path.join(home, ".local", "bin", "claude"),
        os.path.join(home, ".claude", "local", "claude.exe"),
        os.path.join(home, ".claude", "local", "claude"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def check_claude_code(timeout: int = 90) -> tuple[bool, str]:
    """Gọi thử Claude Code một câu rất ngắn để biết đã cài và đã đăng nhập chưa."""
    exe = find_claude()
    if not exe:
        return False, "Không tìm thấy Claude Code trên máy (lệnh `claude`)."
    try:
        provider = ClaudeCodeProvider("claude-haiku-5-5", executable=exe, timeout=timeout)
        result = provider.complete("Trả lời đúng một chữ: OK", "Kiểm tra kết nối", effort="low")
    except ProviderUnavailable as e:
        return False, f"Tìm thấy {exe} nhưng gọi thử thất bại: {e}"
    return True, f"Kết nối được ({exe}). Trả lời thử: {result.text.strip()[:40]}"


class ClaudeCodeProvider(Provider):
    """Gọi `claude -p` (Claude Code chế độ không tương tác) thay cho API.

    Dùng hạn mức của gói Claude đang đăng nhập trong Claude Code, không tính tiền API.
    Biến ANTHROPIC_API_KEY bị xóa khỏi môi trường của tiến trình con — nếu không,
    Claude Code sẽ dùng API key đó và tính tiền API.
    """

    name = "claude-code"

    def __init__(self, model: str = DEFAULT_MODELS["claude-code"], executable: str | None = None, timeout: int = 900):
        super().__init__(model)
        self.exe = executable or find_claude()
        if not self.exe:
            raise ProviderUnavailable("Chưa cài Claude Code (lệnh `claude`). Cài tại https://claude.com/claude-code rồi đăng nhập.")
        self.timeout = timeout
        self._dir = tempfile.mkdtemp(prefix="booktranslator-")
        atexit.register(shutil.rmtree, self._dir, True)

    def _system_file(self, system: str) -> str:
        # Prompt hệ thống quá dài để truyền qua dòng lệnh (giới hạn của Windows) → ghi ra file, dùng lại theo nội dung.
        path = os.path.join(self._dir, hashlib.sha256(system.encode("utf-8")).hexdigest()[:16] + ".txt")
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(system)
        return path

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(key, None)
        return env

    def complete(self, system, user, *, effort="medium", json_schema=None, max_tokens=None):
        cmd = [
            self.exe, "-p", "--output-format", "json",
            "--tools", "",  # chỉ dịch, không cho dùng công cụ nào
            "--system-prompt-file", self._system_file(system),
            "--model", self.model, "--effort", effort,
            "--no-session-persistence", "--strict-mcp-config",
        ]
        if json_schema is not None:
            cmd += ["--json-schema", json.dumps(json_schema, ensure_ascii=False)]  # Claude Code tự kiểm tra định dạng
        try:
            proc = subprocess.run(cmd, input=user.encode("utf-8"), capture_output=True, timeout=self.timeout, env=self._env())
        except subprocess.TimeoutExpired as e:
            raise ProviderUnavailable(f"Claude Code không trả lời sau {self.timeout} giây") from e
        except OSError as e:
            raise ProviderUnavailable(f"Không chạy được Claude Code: {e}") from e
        out = proc.stdout.decode("utf-8", errors="replace")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            err = (proc.stderr.decode("utf-8", errors="replace") or out).strip()[-500:]
            raise ProviderUnavailable(f"Claude Code lỗi (mã {proc.returncode}): {err}") from None
        if data.get("is_error"):
            # Hết hạn mức gói, chưa đăng nhập, lỗi mạng... → để chuỗi dự phòng chuyển sang nhà cung cấp khác.
            raise ProviderUnavailable(f"Claude Code: {str(data.get('result', ''))[:300]}")
        u = data.get("usage") or {}
        cached = u.get("cache_read_input_tokens") or 0
        self.usage.add(
            (u.get("input_tokens") or 0) + cached + (u.get("cache_creation_input_tokens") or 0), u.get("output_tokens") or 0, cached
        )
        text = data.get("result") or ""
        if json_schema is not None and isinstance(data.get("structured_output"), dict):
            text = json.dumps(data["structured_output"], ensure_ascii=False)
        stop = data.get("stop_reason")
        if stop == "refusal":
            return Completion(text, "refusal", None, self.id)
        return Completion(text, "max_tokens" if stop == "max_tokens" else "end", provider=self.id)


# ---- OpenAI-compatible (ChatGPT, DeepSeek) ----------------------------


class OpenAICompatProvider(Provider):
    """ChatGPT và DeepSeek dùng chung giao thức Chat Completions của OpenAI."""

    def __init__(self, name: str, model: str | None = None, client: Any = None):
        super().__init__(model or DEFAULT_MODELS[name])
        self.name = name
        if client is None:
            try:
                import openai
            except ImportError as e:
                raise ProviderUnavailable("Cần cài thêm: pip install openai") from e
            key = os.environ.get(API_KEY_ENV[name])
            if not key:
                raise ProviderUnavailable(f"Thiếu biến môi trường {API_KEY_ENV[name]}")
            client = openai.OpenAI(api_key=key, base_url=_BASE_URLS[name], max_retries=5)
        self.client = client

    def complete(self, system, user, *, effort="medium", json_schema=None, max_tokens=None):
        import openai

        limit = min(max_tokens or _MAX_OUTPUT[self.name], _MAX_OUTPUT[self.name])
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.name == "openai":
            kwargs["max_completion_tokens"] = limit
            kwargs["reasoning_effort"] = {"xhigh": "high", "max": "high"}.get(effort, effort)
        else:
            kwargs["max_tokens"] = limit
        if json_schema is not None:
            # json_object được cả hai hỗ trợ; schema được mô tả trong prompt.
            kwargs["response_format"] = {"type": "json_object"}
            kwargs["messages"][0]["content"] += (
                "\n\nTrả lời bằng một JSON object hợp lệ đúng schema sau:\n" + json.dumps(json_schema, ensure_ascii=False)
            )
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except (openai.RateLimitError, openai.AuthenticationError, openai.PermissionDeniedError,
                openai.InternalServerError, openai.APIConnectionError) as e:
            raise ProviderUnavailable(f"{self.name}: {type(e).__name__}: {e}") from e
        except openai.APIStatusError as e:
            if e.status_code == 402:  # DeepSeek: hết số dư
                raise ProviderUnavailable(f"{self.name}: hết số dư tài khoản") from e
            raise
        u = resp.usage
        if u is not None:
            details = getattr(u, "prompt_tokens_details", None)
            cached = getattr(u, "prompt_cache_hit_tokens", None) or (getattr(details, "cached_tokens", 0) if details else 0)
            self.usage.add(u.prompt_tokens, u.completion_tokens, cached or 0)
        choice = resp.choices[0]
        text = choice.message.content or ""
        if choice.finish_reason == "content_filter":
            return Completion(text, "refusal", "content_filter", self.id)
        return Completion(text, "max_tokens" if choice.finish_reason == "length" else "end", provider=self.id)


# ---- chuỗi dự phòng --------------------------------------------------


class ProviderChain:
    def __init__(self, providers: list[Provider]):
        if not providers:
            raise ValueError("Cần ít nhất một nhà cung cấp mô hình")
        self.providers = providers
        self._active = 0
        self._lock = threading.Lock()

    @property
    def id(self) -> str:
        """Định danh ổn định (nhà cung cấp chính) — dùng cho khóa cache, không đổi khi đã chuyển dự phòng."""
        return self.providers[0].id

    @property
    def active(self) -> Provider:
        return self.providers[self._active]

    def complete(self, system: str, user: str, **kw) -> Completion:
        with self._lock:
            start = self._active
        last: Completion | None = None
        for idx in range(start, len(self.providers)):
            provider = self.providers[idx]
            try:
                result = provider.complete(system, user, **kw)
            except ProviderUnavailable as e:
                with self._lock:
                    if self._active <= idx < len(self.providers) - 1:
                        self._active = idx + 1
                        log.warning("%s — chuyển sang %s", e, self.providers[idx + 1].id)
                if idx == len(self.providers) - 1:
                    raise
                continue
            if result.stop_reason != "refusal":
                return result
            last = result  # bị từ chối: thử đoạn này ở nhà cung cấp kế tiếp, không chuyển hẳn
            log.warning("%s từ chối đoạn này (%s), thử nhà cung cấp kế tiếp", provider.id, result.refusal_category)
        assert last is not None
        return last

    def usage_report(self) -> str:
        lines = []
        for p in self.providers:
            u = p.usage
            if u.requests:
                lines.append(
                    f"{p.id}: {u.requests} request, input {u.input_tokens:,} token "
                    f"(cache {u.cached_input_tokens:,}), output {u.output_tokens:,} token"
                )
        return "\n".join(lines) or "Chưa gọi API lần nào (mọi thứ lấy từ cache)."


def parse_spec(spec: str) -> list[tuple[str, str | None]]:
    """"claude:claude-opus-5-5,deepseek" → [("claude", "claude-opus-5-5"), ("deepseek", None)]."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, model = part.partition(":")
        name = name.strip().lower()
        name = {"chatgpt": "openai", "gpt": "openai", "claudecode": "claude-code", "cc": "claude-code"}.get(name, name)
        if name not in DEFAULT_MODELS:
            raise ValueError(f"Nhà cung cấp '{name}' không hỗ trợ (có: claude-code, claude, deepseek, openai)")
        out.append((name, model.strip() or None))
    return out


def build_chain(
    spec: str | list[tuple[str, str | None]], use_fallback: bool = True, model_override: dict[str, str] | None = None
) -> ProviderChain:
    """Tạo chuỗi từ cấu hình; nhà cung cấp thiếu key/thư viện thì bỏ qua (trừ khi không còn cái nào).

    `model_override` là model mặc định theo nhà cung cấp, chỉ dùng khi spec không ghi model.
    """
    providers: list[Provider] = []
    errors = []
    for name, model in parse_spec(spec) if isinstance(spec, str) else spec:
        model = model or (model_override or {}).get(name)
        try:
            if name == "claude":
                providers.append(ClaudeProvider(model or DEFAULT_MODELS["claude"], use_fallback=use_fallback))
            elif name == "claude-code":
                providers.append(ClaudeCodeProvider(model or DEFAULT_MODELS["claude-code"]))
            else:
                providers.append(OpenAICompatProvider(name, model))
        except ProviderUnavailable as e:
            errors.append(str(e))
            if str(e) not in _warned:
                _warned.add(str(e))
                log.warning("Bỏ qua %s: %s", name, e)
    if not providers:
        raise ValueError("Không dùng được nhà cung cấp nào: " + "; ".join(errors))
    return ProviderChain(providers)
