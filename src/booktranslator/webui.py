"""Giao diện web chạy trên máy: `booktranslator giao-dien` rồi mở trình duyệt.

Máy chủ chỉ nghe trên 127.0.0.1. Mọi API đòi header `X-BT-Token` (mã ngẫu nhiên
nhúng trong trang) để trang web lạ không gọi được vào máy chủ này. Mỗi tác vụ
(dịch thử, lập sổ tay, dịch, học phong cách) chạy `python -m booktranslator ...`
trong tiến trình con, nên dừng được bất cứ lúc nào và tiến độ vẫn được lưu.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .cli import PRESETS
from .document import SUPPORTED, default_output_path
from .genres import BUILTIN as GENRES
from .providers import API_KEY_ENV, CLAUDE_PATH_ENV, CODEX_PATH_ENV, check_claude_code, check_codex, find_claude, find_codex
from .styles import BUILTIN as STYLES

HTML_FILE = Path(__file__).with_name("webui.html")
EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
TEXT_SUFFIXES = (".md", ".json", ".txt")
MAX_TEXT_BYTES = 5 * 1024 * 1024
VALID_PROVIDERS = ("claude-code", "codex", "claude", "deepseek", "openai")


def _safe_name(name: str) -> str:
    """Tên file an toàn: bỏ thư mục, ký tự lạ, không bắt đầu bằng '-' hay '.'."""
    name = Path(name.replace("\\", "/")).name
    name = "".join(c for c in name if c not in '<>:"|?*\x00').strip().lstrip("-.")
    return name or "file"


MAX_JOBS_KEPT = 30


class Job:
    """Một lần chạy. Bản ghi (.json) và nhật ký (.log) được lưu trong <thư mục làm việc>/.bt_ui/jobs
    để tắt phần mềm mở lại vẫn thấy lịch sử và bấm "Dịch tiếp"."""

    def __init__(self, job_id: str, action: str, argv: list[str], outputs: list[str], opts: dict, folder: Path):
        self.id = job_id
        self.action = action
        self.argv = argv
        self.outputs = outputs
        self.opts = opts
        self.folder = folder
        self.lines: list[str] = []
        self.status = "running"
        self.returncode: int | None = None
        self.started = time.time()
        self.proc: subprocess.Popen | None = None

    @property
    def record_path(self) -> Path:
        return self.folder / f"{self.id}.json"

    @property
    def log_path(self) -> Path:
        return self.folder / f"{self.id}.log"

    def save(self) -> None:
        data = {k: getattr(self, k) for k in ("id", "action", "argv", "outputs", "opts", "status", "returncode", "started")}
        try:
            self.record_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    @classmethod
    def load(cls, path: Path) -> "Job":
        data = json.loads(path.read_text(encoding="utf-8"))
        job = cls(data["id"], data["action"], data["argv"], data["outputs"], data.get("opts", {}), path.parent)
        job.status, job.returncode, job.started = data["status"], data.get("returncode"), data["started"]
        if job.log_path.exists():
            job.lines = job.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-1000:]
        if job.status == "running":  # phần mềm bị tắt khi tác vụ đang chạy
            job.status = "interrupted"
            job.save()
        return job

    def summary(self) -> dict:
        return {"id": self.id, "action": self.action, "status": self.status, "returncode": self.returncode,
                "started": self.started, "outputs": self.outputs, "opts": self.opts}


class App:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(24)
        self.keys: dict[str, str] = {}  # API key nhập trên giao diện: chỉ giữ trong bộ nhớ
        self.claude_path = ""  # đường dẫn claude do người dùng chỉ định trên giao diện
        self.ui_dir = self.workspace / ".bt_ui"
        self.jobs_dir = self.ui_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.ui_dir / "settings.json"
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._load_jobs()

    def _load_jobs(self) -> None:
        records = sorted(self.jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in records[:-MAX_JOBS_KEPT]:  # dọn bớt lịch sử cũ
            for f in (old, old.with_suffix(".log")):
                f.unlink(missing_ok=True)
        for path in records[-MAX_JOBS_KEPT:]:
            try:
                job = Job.load(path)
            except (OSError, ValueError, KeyError):
                continue
            self.jobs[job.id] = job

    # ---- cài đặt giao diện (lưu trên ổ đĩa, không phụ thuộc trình duyệt) ------

    def load_settings(self) -> dict:
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_settings(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise ValueError("Cài đặt không hợp lệ")
        self.settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def progress(self, job: Job) -> dict | None:
        """Tiến độ cả cuốn (file <kết quả>.progress.json do tiến trình dịch ghi ra)."""
        if job.action not in ("translate", "analyze") or not job.outputs:
            return None
        try:
            path = self.resolve(job.outputs[0])
            return json.loads(path.with_name(path.name + ".progress.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, PermissionError):
            return None

    # ---- file trong thư mục làm việc -------------------------------------

    def resolve(self, rel: str) -> Path:
        path = (self.workspace / rel).resolve()
        if not path.is_relative_to(self.workspace):
            raise PermissionError("Đường dẫn nằm ngoài thư mục làm việc")
        return path

    def existing(self, rel: str) -> str:
        path = self.resolve(rel)
        if not path.exists():
            raise FileNotFoundError(f"Không tìm thấy file: {rel}")
        return str(path)

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.workspace).as_posix()

    def list_files(self) -> dict:
        files = sorted(
            p for p in self.workspace.rglob("*")
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(self.workspace).parts)
            and not p.name.endswith(".progress.json")
        )
        rels = [self.rel(p) for p in files]
        return {
            "books": [r for r in rels if r.lower().endswith(SUPPORTED) and ".vi." not in r and not r.endswith(".preview.md")],
            "briefs": [r for r in rels if r.endswith(".md") and not r.endswith((".preview.md", ".consistency.md"))],
            "bibles": [r for r in rels if r.endswith(".bible.json")],
            "jsons": [r for r in rels if r.endswith(".json") and not r.endswith(".bible.json")],
            "results": [r for r in rels if ".vi." in r or r.endswith((".preview.md", ".consistency.md"))],
        }

    def install_starter(self) -> list[str]:
        """Chép bộ khởi đầu Discworld (hướng dẫn dịch + tên riêng) vào thư mục làm việc."""
        src = EXAMPLES_DIR / "discworld"
        if not src.is_dir():
            raise FileNotFoundError("Không tìm thấy thư mục examples/discworld (hãy cài từ mã nguồn git clone)")
        dest = self.workspace / "discworld"
        dest.mkdir(exist_ok=True)
        copied = []
        for f in src.iterdir():
            target = dest / f.name
            if not target.exists():
                shutil.copy2(f, target)
            copied.append(self.rel(target))
        return copied

    # ---- trạng thái -------------------------------------------------------

    def providers_status(self) -> dict:
        def has_key(name: str) -> bool:
            return bool(self.keys.get(name) or os.environ.get(API_KEY_ENV[name]))

        try:
            import openai  # noqa: F401

            has_openai_lib = True
        except ImportError:
            has_openai_lib = False
        return {
            "claude-code": bool(self._claude_exe()),
            "codex": bool(find_codex()),
            "claude": has_key("claude"),
            "deepseek": has_key("deepseek") and has_openai_lib,
            "openai": has_key("openai") and has_openai_lib,
        }

    def _claude_exe(self) -> str | None:
        if self.claude_path and os.path.isfile(self.claude_path):
            return self.claude_path
        return find_claude()

    def test_claude(self) -> dict:
        if self.claude_path:
            os.environ[CLAUDE_PATH_ENV] = self.claude_path
        ok, message = check_claude_code()
        return {"ok": ok, "message": message, "path": self._claude_exe() or ""}

    def test_codex(self) -> dict:
        ok, message = check_codex()
        return {"ok": ok, "message": message, "path": find_codex() or ""}

    def state(self) -> dict:
        return {
            "workspace": str(self.workspace),
            "files": self.list_files(),
            "genres": [{"key": g.key, "name": g.name, "description": g.description, "effort": g.effort} for g in GENRES.values()],
            "styles": [{"key": s.key, "name": s.name, "description": s.description} for s in STYLES.values()],
            "presets": list(PRESETS),
            "providers": self.providers_status(),
            "jobs": [j.summary() | {"progress": self.progress(j)}
                     for j in sorted(self.jobs.values(), key=lambda j: j.started, reverse=True)],
            "settings": self.load_settings(),
            "starter_available": (EXAMPLES_DIR / "discworld").is_dir(),
            "claude_path": self._claude_exe() or "",
            "codex_path": find_codex() or "",
        }

    # ---- tác vụ -----------------------------------------------------------

    def build_job(self, opts: dict) -> tuple[str, list[str], list[str]]:
        """Dựng dòng lệnh từ lựa chọn trên giao diện. Trả về (action, argv, các file kết quả dự kiến)."""
        action = opts.get("action")

        def file_opt(key: str) -> str | None:
            value = (opts.get(key) or "").strip()
            return self.existing(value) if value else None

        if action == "learn":
            ban_dich = file_opt("ban_dich")
            if not ban_dich:
                raise ValueError("Chưa chọn bản dịch tham chiếu")
            out = self.resolve(_safe_name(opts.get("style_output") or "phong-cach.json"))
            if out.suffix != ".json":
                out = out.with_suffix(".json")
            argv = ["hoc-phong-cach", ban_dich, f"--output={out}", f"--ten={opts.get('style_name') or out.stem}"]
            if ban_goc := file_opt("ban_goc"):
                argv.append(f"--ban-goc={ban_goc}")
            argv += self._api_args(opts)
            return action, argv, [self.rel(out)]

        commands = {"preview": "dich-thu", "analyze": "phan-tich", "translate": "dich"}
        if action not in commands:
            raise ValueError(f"Tác vụ không hợp lệ: {action}")
        book = file_opt("book")
        if not book:
            raise ValueError("Chưa chọn sách")
        genre = opts.get("genre") or "general"
        if genre not in GENRES:
            raise ValueError(f"Thể loại không hợp lệ: {genre}")
        bible_rel = _safe_name(opts.get("bible") or f"{Path(book).stem}.bible.json")
        if not bible_rel.endswith(".bible.json"):
            bible_rel = bible_rel.removesuffix(".json") + ".bible.json"
        bible = self.resolve(bible_rel)

        argv = [commands[action], book, f"--the-loai={genre}"]
        style = opts.get("style") or "natural"
        if style.startswith("file:"):
            argv.append(f"--phong-cach-file={self.existing(style[5:])}")
        elif style in STYLES:
            argv.append(f"--phong-cach={style}")
        else:
            raise ValueError(f"Phong cách không hợp lệ: {style}")
        if brief := file_opt("brief"):
            argv.append(f"--huong-dan={brief}")
        if glossary := file_opt("glossary"):
            argv.append(f"--glossary={glossary}")
        argv += self._api_args(opts)

        outputs: list[str] = []
        if action == "analyze":
            argv.append(f"--output={bible}")
            outputs.append(self.rel(bible))
        else:
            argv.append(f"--so-tay={bible}")
            editors = [p for p in opts.get("editors") or [] if p in VALID_PROVIDERS]
            if editors:
                argv.append(f"--bien-tap-bang={','.join(editors)}")
            polish = opts.get("polish") or "auto"
            if polish == "on":
                argv.append("--polish")
            elif polish == "off":
                argv.append("--no-polish")
            if note := (opts.get("note") or "").strip():
                argv.append(f"--yeu-cau={note}")
            if opts.get("fresh"):
                argv.append("--fresh")
            translated = default_output_path(Path(book), "vi")
            if action == "preview":
                argv.append(f"--chunks={int(opts.get('chunks') or 2)}")
                if opts.get("section"):
                    argv.append(f"--section={int(opts['section'])}")
                outputs.append(self.rel(translated.with_name(translated.name + ".preview.md")))
            else:
                outputs += [self.rel(translated), self.rel(translated.with_name(translated.name + ".consistency.md"))]
            outputs.append(self.rel(bible))
        return action, argv, outputs

    def _api_args(self, opts: dict) -> list[str]:
        args = []
        if (preset := opts.get("muc")) and preset in PRESETS:
            args.append(f"--muc={preset}")
        providers = [p for p in opts.get("providers") or [] if p in VALID_PROVIDERS]
        if providers:
            args.append(f"--providers={','.join(providers)}")
        if model := (opts.get("model") or "").strip():
            args.append(f"--model={model}")
        if (effort := opts.get("effort")) in ("low", "medium", "high", "xhigh", "max"):
            args.append(f"--effort={effort}")
        if opts.get("wait") is False:
            args.append("--khong-cho")
        elif opts.get("wait_hours"):
            args.append(f"--cho-han-muc={max(0.0, min(float(opts['wait_hours']), 168.0))}")
        return args

    def start_job(self, opts: dict) -> Job:
        with self._lock:
            if any(j.status == "running" for j in self.jobs.values()):
                raise RuntimeError("Đang có một tác vụ chạy. Hãy chờ xong hoặc bấm Dừng.")
            action, argv, outputs = self.build_job(opts)
            job = Job(secrets.token_hex(4), action, argv, outputs, opts, self.jobs_dir)
            self.jobs[job.id] = job
            job.save()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        # Tiến trình con phải import được booktranslator kể cả khi chưa "pip install" (chạy từ mã nguồn).
        package_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(p for p in (package_root, env.get("PYTHONPATH", "")) if p)
        for name, value in self.keys.items():
            if value:
                env[API_KEY_ENV[name]] = value
        if exe := self._claude_exe():
            env[CLAUDE_PATH_ENV] = exe
        if exe := find_codex():
            env[CODEX_PATH_ENV] = exe
        job.proc = subprocess.Popen(
            [sys.executable, "-m", "booktranslator", *argv],
            cwd=self.workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return job

    def _pump(self, job: Job) -> None:
        assert job.proc and job.proc.stdout
        with job.log_path.open("a", encoding="utf-8") as log:
            for raw in job.proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                job.lines.append(line)
                log.write(line + "\n")
                log.flush()
        job.returncode = job.proc.wait()
        if job.status == "running":
            job.status = "done" if job.returncode == 0 else "error"
        job.save()

    def cancel(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job and job.proc and job.status == "running":
            job.status = "cancelled"
            job.save()
            job.proc.terminate()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BookTranslator"

        def log_message(self, *args):  # im lặng
            pass

        # ---- tiện ích ---------------------------------------------------
        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, msg: str, status: int = 400) -> None:
            self._json({"error": msg}, status)

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _authorized(self, query: dict) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                return False  # chặn DNS rebinding
            token = self.headers.get("X-BT-Token") or (query.get("t") or [""])[0]
            return secrets.compare_digest(token, app.token)

        def _route(self, method: str) -> None:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if method == "GET" and url.path == "/":
                html = HTML_FILE.read_text(encoding="utf-8").replace("__BT_TOKEN__", app.token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if not url.path.startswith("/api/"):
                return self._error("Không tìm thấy", 404)
            if not self._authorized(query):
                return self._error("Không được phép", 403)
            try:
                return self._api(method, url.path, query)
            except (PermissionError, FileNotFoundError, ValueError, RuntimeError, json.JSONDecodeError) as e:
                return self._error(str(e))

        def _api(self, method: str, path: str, query: dict) -> None:
            q = lambda k: (query.get(k) or [""])[0]
            if method == "GET" and path == "/api/state":
                return self._json(app.state())
            if method == "POST" and path == "/api/keys":
                data = json.loads(self._body() or b"{}")
                for name in API_KEY_ENV:
                    if name in data:
                        app.keys[name] = str(data[name]).strip()
                return self._json({"providers": app.providers_status()})
            if path == "/api/settings":
                if method == "PUT":
                    app.save_settings(json.loads(self._body() or b"{}"))
                    return self._json({"saved": True})
                return self._json(app.load_settings())
            if method == "POST" and path == "/api/codex-path":
                value = str(json.loads(self._body() or b"{}").get("path", "")).strip().strip('"')
                if value and not os.path.isfile(value):
                    raise FileNotFoundError(f"Không có file: {value}")
                os.environ[CODEX_PATH_ENV] = value  # find_codex() và các tác vụ dịch đều đọc biến này
                return self._json({"providers": app.providers_status(), "path": find_codex() or ""})
            if method == "POST" and path == "/api/claude-path":
                value = str(json.loads(self._body() or b"{}").get("path", "")).strip().strip('"')
                if value and not os.path.isfile(value):
                    raise FileNotFoundError(f"Không có file: {value}")
                app.claude_path = value
                return self._json({"providers": app.providers_status(), "path": app._claude_exe() or ""})
            if method == "POST" and path == "/api/test-claude":
                return self._json(app.test_claude())
            if method == "POST" and path == "/api/test-codex":
                return self._json(app.test_codex())
            if method == "POST" and path == "/api/starter":
                return self._json({"files": app.install_starter()})
            if method == "POST" and path == "/api/upload":
                name = _safe_name(q("name"))
                target = app.resolve(name)
                target.write_bytes(self._body())
                return self._json({"path": app.rel(target)})
            if path == "/api/file":
                target = app.resolve(q("path"))
                if target.suffix not in TEXT_SUFFIXES:
                    raise PermissionError("Chỉ đọc/ghi được file .md, .json, .txt")
                if method == "GET":
                    if not target.exists():
                        return self._json({"path": q("path"), "content": "", "exists": False})
                    if target.stat().st_size > MAX_TEXT_BYTES:
                        raise ValueError("File quá lớn để mở trên giao diện")
                    return self._json({"path": q("path"), "content": target.read_text(encoding="utf-8"), "exists": True})
                if method == "PUT":
                    text = self._body().decode("utf-8")
                    if target.suffix == ".json":
                        json.loads(text)  # không cho lưu JSON hỏng
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding="utf-8")
                    return self._json({"saved": True})
            if method == "GET" and path == "/api/download":
                target = app.resolve(q("path"))
                if not target.is_file():
                    raise FileNotFoundError("Không tìm thấy file")
                return self._send(200, target.read_bytes(), "application/octet-stream",
                                  {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(target.name)}"})
            if method == "POST" and path == "/api/jobs":
                job = app.start_job(json.loads(self._body() or b"{}"))
                return self._json(job.summary())
            if path.startswith("/api/jobs/"):
                parts = path.split("/")
                job = app.jobs.get(parts[3]) if len(parts) > 3 else None
                if not job:
                    return self._error("Không có tác vụ này", 404)
                if method == "POST" and len(parts) > 4 and parts[4] == "cancel":
                    app.cancel(job.id)
                    return self._json(job.summary())
                if method == "GET":
                    since = int(q("since") or 0)
                    return self._json(job.summary() | {"lines": job.lines[since:], "next": len(job.lines),
                                                       "progress": app.progress(job)})
            return self._error("Không tìm thấy", 404)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

    return Handler


def serve(workspace: Path, port: int = 8765, open_browser: bool = True) -> None:
    app = App(workspace)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Giao diện BookTranslator: {url}", file=sys.stderr)
    print(f"Thư mục làm việc: {app.workspace}", file=sys.stderr)
    print("Nhấn Ctrl+C để tắt.", file=sys.stderr)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for job in app.jobs.values():
            if job.proc and job.status == "running":
                job.proc.terminate()
        server.server_close()
