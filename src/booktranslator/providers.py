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
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)
_warned: set[str] = set()

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Model mặc định của từng nhà cung cấp; đổi bằng "tên:model" trong --providers.
DEFAULT_MODELS = {
    "claude": "claude-opus-5-5",
    "claude-code": "claude-opus-5-5",
    "codex": "",  # để trống = model mặc định trong cấu hình Codex của người dùng
    "deepseek": "deepseek-chat",
    "openai": "gpt-5",
}
API_KEY_ENV = {"claude": "ANTHROPIC_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY"}
_BASE_URLS = {"deepseek": "https://api.deepseek.com", "openai": None}
# Giới hạn output của từng nhà cung cấp (token). DeepSeek chat tối đa 8K.
_MAX_OUTPUT = {"claude": 64000, "deepseek": 8192, "openai": 32000}


class ProviderUnavailable(RuntimeError):
    """Nhà cung cấp không dùng được (hết quota, rate limit, quá tải, sai key, mất mạng)."""


class ProviderLimitReached(ProviderUnavailable):
    """Không dùng được TẠM THỜI — hết hạn mức gói, rate limit, quá tải, mất mạng. Chờ rồi thử lại được.

    `retry_at` (epoch giây) là lúc dự kiến dùng lại được, nếu nhà cung cấp cho biết.
    """

    def __init__(self, message: str, retry_at: float | None = None):
        super().__init__(message)
        self.retry_at = retry_at


def _retry_after(error) -> float | None:
    """Đọc header retry-after (giây) của lỗi HTTP 429, nếu có."""
    try:
        value = error.response.headers.get("retry-after")
        return time.time() + float(value) if value else None
    except (AttributeError, TypeError, ValueError):
        return None


_TEMPORARY_RE = re.compile(
    r"\b(usage limit|limit reached|hit your limit|rate.?limit|overloaded|quá tải|connection|network|timed? ?out)\b",
    re.IGNORECASE,
)


def _limit_reset_time(message: str) -> float | None:
    """Claude Code có thể kèm thời điểm reset hạn mức dạng epoch: "...usage limit reached|1760000000"."""
    m = re.search(r"\|(\d{10})\b", message)
    return float(m.group(1)) if m else None


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
        return f"{self.name}:{self.model or 'mặc định'}"

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
        except anthropic.RateLimitError as e:
            raise ProviderLimitReached(f"Claude: hết hạn mức tạm thời ({e})", _retry_after(e)) from e
        except (anthropic.InternalServerError, anthropic.APIConnectionError) as e:
            raise ProviderLimitReached(f"Claude: quá tải hoặc mất mạng ({type(e).__name__})") from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
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
            raise ProviderLimitReached(f"Claude Code không trả lời sau {self.timeout} giây") from e
        except OSError as e:
            raise ProviderUnavailable(f"Không chạy được Claude Code: {e}") from e
        out = proc.stdout.decode("utf-8", errors="replace")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            err = (proc.stderr.decode("utf-8", errors="replace") or out).strip()[-500:]
            raise ProviderUnavailable(f"Claude Code lỗi (mã {proc.returncode}): {err}") from None
        if data.get("is_error"):
            message = str(data.get("result", ""))
            if _TEMPORARY_RE.search(message):
                # Hết hạn mức gói (theo khung giờ / theo tuần), quá tải, mất mạng → chờ được.
                raise ProviderLimitReached(f"Claude Code: {message[:300]}", _limit_reset_time(message))
            raise ProviderUnavailable(f"Claude Code: {message[:300]}")  # vd. chưa đăng nhập
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


# ---- Codex (dùng gói ChatGPT Plus/Pro đang đăng nhập) -----------------

CODEX_PATH_ENV = "BT_CODEX_PATH"


_npm_prefix_cache: list[str | None] = []


def _npm_global_dir() -> str | None:
    """Thư mục npm cài lệnh toàn cục (`npm install -g`) — khác nhau tùy máy (nvm, đổi prefix...)."""
    if not _npm_prefix_cache:
        prefix = None
        npm = shutil.which("npm")
        if npm:
            try:
                out = subprocess.run([npm, "config", "get", "prefix"], capture_output=True, timeout=20)
                prefix = out.stdout.decode("utf-8", errors="replace").strip() or None
            except (OSError, subprocess.TimeoutExpired):
                prefix = None
        _npm_prefix_cache.append(prefix)
    return _npm_prefix_cache[0]


def find_codex() -> str | None:
    """Tìm lệnh `codex`: biến BT_CODEX_PATH, PATH, thư mục npm toàn cục, rồi các chỗ cài quen thuộc.

    Cửa sổ đã mở từ trước khi cài Codex không thấy PATH mới, nên phải tự dò thư mục npm.
    """
    custom = os.environ.get(CODEX_PATH_ENV, "").strip().strip('"')
    if custom and os.path.isfile(custom):
        return custom
    found = shutil.which("codex")
    if found:
        return found
    candidates = []
    if prefix := _npm_global_dir():
        candidates += [os.path.join(prefix, "codex.cmd"), os.path.join(prefix, "bin", "codex")]
    candidates += [
        os.path.join(os.environ.get("APPDATA", ""), "npm", "codex.cmd"),
        os.path.join(os.path.expanduser("~"), ".local", "bin", "codex"),
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
    ]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


class CodexProvider(Provider):
    """Gọi `codex exec` (Codex CLI chế độ không tương tác) — dùng hạn mức gói ChatGPT, không tính tiền API.

    Codex là một agent biết chạy lệnh; ở đây nó chạy trong thư mục tạm trống với sandbox chỉ-đọc
    và chỉ được yêu cầu trả lời bằng văn bản. OPENAI_API_KEY / CODEX_API_KEY bị xóa khỏi môi trường
    để Codex dùng tài khoản ChatGPT đã đăng nhập thay vì API key (tính tiền).
    """

    name = "codex"

    def __init__(self, model: str = "", executable: str | None = None, timeout: int = 900):
        super().__init__(model or "")
        self.exe = executable or find_codex()
        if not self.exe:
            raise ProviderUnavailable("Chưa cài Codex (lệnh `codex`). Cài: npm install -g @openai/codex, rồi `codex login`.")
        self.timeout = timeout
        self._check_login()
        self._dir = tempfile.mkdtemp(prefix="booktranslator-codex-")
        atexit.register(shutil.rmtree, self._dir, True)

    def _check_login(self) -> None:
        """Chưa đăng nhập thì `codex exec` treo tới hết thời gian chờ — hỏi `codex login status` trước cho nhanh."""
        try:
            proc = subprocess.run([self.exe, "login", "status"], capture_output=True, timeout=30, env=self._env())
        except (subprocess.TimeoutExpired, OSError):
            return  # không xác định được thì cứ thử gọi bình thường
        text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
        if proc.returncode != 0 or "not logged in" in text.lower():
            raise ProviderUnavailable("Codex chưa đăng nhập. Mở PowerShell/Terminal, chạy `codex login` rồi đăng nhập tài khoản ChatGPT.")

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            env.pop(key, None)
        return env

    def complete(self, system, user, *, effort="medium", json_schema=None, max_tokens=None):
        out_file = os.path.join(self._dir, f"out-{threading.get_ident()}-{time.monotonic_ns()}.txt")
        cmd = [
            self.exe, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "--json",
            "--color", "never", "-C", self._dir, "-o", out_file,
            "-c", f'model_reasoning_effort="{ {"xhigh": "high", "max": "high"}.get(effort, effort) }"',
        ]
        if self.model:
            cmd += ["-m", self.model]
        if json_schema is not None:
            schema_file = os.path.join(self._dir, hashlib.sha256(json.dumps(json_schema).encode()).hexdigest()[:16] + ".json")
            if not os.path.exists(schema_file):
                with open(schema_file, "w", encoding="utf-8") as f:
                    json.dump(json_schema, f, ensure_ascii=False)
            cmd += ["--output-schema", schema_file]
        cmd.append("-")  # đọc yêu cầu từ stdin
        prompt = (
            "Đây là việc dịch thuật thuần văn bản: KHÔNG chạy lệnh, KHÔNG đọc hay sửa file nào. "
            "Chỉ trả lời đúng theo hướng dẫn.\n\n"
            f"<instructions>\n{system}\n</instructions>\n\n{user}"
        )
        try:
            proc = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=self.timeout, env=self._env())
        except subprocess.TimeoutExpired as e:
            raise ProviderLimitReached(f"Codex không trả lời sau {self.timeout} giây") from e
        except OSError as e:
            raise ProviderUnavailable(f"Không chạy được Codex: {e}") from e
        events = []
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        errors = [str(e.get("message") or e.get("error") or "") for e in events if "error" in str(e.get("type", ""))]
        text = ""
        if os.path.exists(out_file):
            with open(out_file, encoding="utf-8", errors="replace") as f:
                text = f.read()
            os.remove(out_file)
        if proc.returncode != 0 or (errors and not text.strip()):
            message = " ".join(errors) or proc.stderr.decode("utf-8", errors="replace").strip()[-500:]
            if _TEMPORARY_RE.search(message):
                raise ProviderLimitReached(f"Codex: {message[:300]}", _limit_reset_time(message))
            raise ProviderUnavailable(f"Codex lỗi (mã {proc.returncode}): {message[:300]}")
        for event in events:
            usage = event.get("usage") if isinstance(event, dict) else None
            if isinstance(usage, dict):
                cached = usage.get("cached_input_tokens") or 0
                self.usage.add(usage.get("input_tokens") or 0, usage.get("output_tokens") or 0, cached)
        return Completion(text.strip(), "end", provider=self.id)


def check_codex(timeout: int = 120) -> tuple[bool, str]:
    exe = find_codex()
    if not exe:
        return False, "Không tìm thấy Codex trên máy (lệnh `codex`)."
    try:
        result = CodexProvider(executable=exe, timeout=timeout).complete("Trả lời đúng một chữ: OK", "Kiểm tra kết nối", effort="low")
    except ProviderUnavailable as e:
        return False, f"Tìm thấy {exe} nhưng gọi thử thất bại: {e}"
    return True, f"Kết nối được ({exe}). Trả lời thử: {result.text.strip()[:40]}"


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
        except openai.RateLimitError as e:
            if "insufficient_quota" in str(e):  # hết tiền nạp: chờ không giải quyết được
                raise ProviderUnavailable(f"{self.name}: hết số dư tài khoản") from e
            raise ProviderLimitReached(f"{self.name}: hết hạn mức tạm thời", _retry_after(e)) from e
        except (openai.InternalServerError, openai.APIConnectionError) as e:
            raise ProviderLimitReached(f"{self.name}: quá tải hoặc mất mạng ({type(e).__name__})") from e
        except (openai.AuthenticationError, openai.PermissionDeniedError) as e:
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


PROBE_SECONDS = 15 * 60  # không biết giờ reset thì cứ 15 phút thử lại một lần


class ProviderChain:
    """Thử lần lượt các nhà cung cấp; tất cả đều tạm hết hạn mức thì (nếu bật) chờ rồi dịch tiếp.

    `wait_hours`: tổng thời gian tối đa chờ hạn mức hồi lại cho MỘT lần gọi; 0 = không chờ, dừng luôn
    (tiến độ vẫn được lưu, chạy lại sẽ tiếp tục).
    """

    def __init__(self, providers: list[Provider], wait_hours: float = 0, sleep=time.sleep):
        if not providers:
            raise ValueError("Cần ít nhất một nhà cung cấp mô hình")
        self.providers = providers
        self.wait_hours = wait_hours
        self._sleep = sleep
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
        deadline: float | None = None
        while True:
            try:
                return self._complete_once(system, user, **kw)
            except ProviderLimitReached as e:
                now = time.time()
                deadline = deadline or now + self.wait_hours * 3600
                if now >= deadline:
                    raise
                resume = e.retry_at + 60 if e.retry_at and e.retry_at > now else now + PROBE_SECONDS
                resume = min(resume, deadline)
                log.warning(
                    "Tất cả nhà cung cấp đang tạm hết hạn mức (%s). Tiến độ đã lưu — tự chờ đến %s rồi dịch tiếp. "
                    "Muốn dừng hẳn thì bấm Dừng / Ctrl+C.", e, time.strftime("%H:%M %d/%m", time.localtime(resume)),
                )
                self._sleep(max(0.0, resume - now))
                with self._lock:
                    self._active = 0  # hạn mức đã hồi: quay lại nhà cung cấp ưu tiên nhất

    def _complete_once(self, system: str, user: str, **kw) -> Completion:
        with self._lock:
            start = self._active
        last: Completion | None = None
        limited: list[ProviderLimitReached] = []
        for idx in range(start, len(self.providers)):
            provider = self.providers[idx]
            try:
                result = provider.complete(system, user, **kw)
            except ProviderUnavailable as e:
                if isinstance(e, ProviderLimitReached):
                    limited.append(e)
                with self._lock:
                    if self._active <= idx < len(self.providers) - 1:
                        self._active = idx + 1
                        log.warning("%s — chuyển sang %s", e, self.providers[idx + 1].id)
                if idx == len(self.providers) - 1:
                    if limited and self.wait_hours > 0:
                        # Ít nhất một nhà cung cấp chỉ tạm hết hạn mức → chờ được; lấy giờ reset sớm nhất.
                        times = [x.retry_at for x in limited if x.retry_at]
                        raise ProviderLimitReached("; ".join(map(str, limited)), min(times) if times else None) from e
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
            raise ValueError(f"Nhà cung cấp '{name}' không hỗ trợ (có: claude-code, codex, claude, deepseek, openai)")
        out.append((name, model.strip() or None))
    return out


def build_chain(
    spec: str | list[tuple[str, str | None]],
    use_fallback: bool = True,
    model_override: dict[str, str] | None = None,
    wait_hours: float = 0,
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
            elif name == "codex":
                providers.append(CodexProvider(model or ""))
            else:
                providers.append(OpenAICompatProvider(name, model))
        except ProviderUnavailable as e:
            errors.append(str(e))
            if str(e) not in _warned:
                _warned.add(str(e))
                log.warning("Bỏ qua %s: %s", name, e)
    if not providers:
        raise ValueError("Không dùng được nhà cung cấp nào: " + "; ".join(errors))
    return ProviderChain(providers, wait_hours=wait_hours)
