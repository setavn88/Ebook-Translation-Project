"""Giao diện web chạy trên máy: `booktranslator giao-dien` rồi mở trình duyệt.

Máy chủ chỉ nghe trên 127.0.0.1. Mọi API đòi header `X-BT-Token` (mã ngẫu nhiên
nhúng trong trang) để trang web lạ không gọi được vào máy chủ này. Mỗi tác vụ
(dịch thử, lập sổ tay, dịch, học phong cách) chạy `python -m booktranslator ...`
trong tiến trình con, nên dừng được bất cứ lúc nào và tiến độ vẫn được lưu.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .cli import PRESETS
from .document import SUPPORTED, default_output_path, load_document
from .genres import BUILTIN as GENRES
from .postedit import LEVELS, apply_edits_file, chapter_overview, default_output, edits_path, existing_edits
from .providers import API_KEY_ENV, ProviderUnavailable, CLAUDE_PATH_ENV, CODEX_PATH_ENV, check_claude_code, check_codex, find_claude, find_codex, list_models
from .styles import BUILTIN as STYLES

HTML_FILE = Path(__file__).with_name("webui.html")
EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
TEXT_SUFFIXES = (".md", ".json", ".txt")
MAX_TEXT_BYTES = 5 * 1024 * 1024
VALID_PROVIDERS = ("claude-code", "codex", "claude", "deepseek", "openai")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,80}$")


def provider_spec(names: list, models: dict | None) -> str:
    """["codex", "deepseek"] + {"deepseek": "deepseek-flash"} → "codex,deepseek:deepseek-flash"."""
    out = []
    for name in names:
        if name not in VALID_PROVIDERS:
            continue
        model = str((models or {}).get(name) or "").strip()
        out.append(f"{name}:{model}" if model and _MODEL_RE.match(model) else name)
    return ",".join(out)


PROJECT_FILE = "project.json"
PROJECT_BIBLE = "so-tay.bible.json"


def _slug(name: str) -> str:
    """"Discworld Series" → "discworld-series" (tên thư mục dự án)."""
    text = unicodedata.normalize("NFD", name.replace("đ", "d").replace("Đ", "D"))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:60] or "du-an"


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

    def load_settings(self, project: str = "") -> dict:
        if project:
            return dict(self._project_info(project).get("settings") or {})
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_settings(self, data: dict, project: str = "") -> None:
        if not isinstance(data, dict):
            raise ValueError("Cài đặt không hợp lệ")
        if project:
            info = self._project_info(project)
            info["settings"] = data
            self._write_project(project, info)
            return
        self.settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- dự án: mỗi bộ sách / series một thư mục riêng ---------------------
    # <thư mục làm việc>/<dự án>/project.json + sách, sổ tay, hướng dẫn, kết quả của dự án đó.
    # Dự án "" = thư mục chung (cách dùng cũ, trước khi có dự án).

    def project_dir(self, project: str = "") -> Path:
        if not project:
            return self.workspace
        path = self.resolve(project)
        if path == self.workspace or not (path / PROJECT_FILE).is_file():
            raise FileNotFoundError(f"Không có dự án: {project}")
        return path

    def _project_info(self, project: str) -> dict:
        try:
            data = json.loads((self.project_dir(project) / PROJECT_FILE).read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError(f"{project}/{PROJECT_FILE} bị hỏng") from e
        return data if isinstance(data, dict) else {}

    def _write_project(self, project: str, info: dict) -> None:
        path = self.project_dir(project) / PROJECT_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _project_dirs(self) -> list[Path]:
        return sorted(p.parent for p in self.workspace.glob(f"*/{PROJECT_FILE}") if not p.parent.name.startswith("."))

    def projects(self) -> list[dict]:
        out = []
        for d in self._project_dirs():
            rel = self.rel(d)
            try:
                info = self._project_info(rel)
            except ValueError:
                info = {}
            out.append({"id": rel, "name": info.get("name") or d.name, "description": info.get("description", ""),
                        "books": len(self.list_files(rel)["books"])})
        return out

    def create_project(self, name: str, description: str = "", genre: str = "", starter: bool = False,
                       move_common: bool = False) -> dict:
        name = " ".join(str(name).split())
        if not name:
            raise ValueError("Chưa đặt tên dự án")
        slug = _slug(name)
        folder = self.workspace / slug
        n = 2
        while folder.exists():
            folder, n = self.workspace / f"{slug}-{n}", n + 1
        folder.mkdir()
        settings = {"bible": PROJECT_BIBLE}
        if genre in GENRES:
            settings["genre"] = genre
        info = {"name": name, "description": str(description), "created": time.strftime("%Y-%m-%d %H:%M"), "settings": settings}
        (folder / PROJECT_FILE).write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        pid = self.rel(folder)
        moved = self.move_common_into(pid) if move_common else []
        if starter:
            self.install_starter(pid)
        return {"id": pid, "moved": moved}

    def move_common_into(self, project: str) -> list[str]:
        """Chuyển sách/sổ tay/kết quả đang ở thư mục chung vào dự án (kèm tiến độ đã lưu), giữ nguyên tên file.
        Cài đặt chung được chép sang dự án, đường dẫn trỏ vào vị trí mới."""
        dest = self.project_dir(project)
        if dest == self.workspace:
            raise ValueError("Chọn một dự án để chuyển file vào")
        project_dirs = set(self._project_dirs())
        moves: dict[str, str] = {}

        def move(src: Path, target: Path) -> None:
            if target.exists():
                return  # không ghi đè file đã có trong dự án
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
            moves[self.rel_any(src)] = self.rel(target)

        for item in sorted(self.workspace.iterdir()):
            if item.name.startswith(".") or item in project_dirs or item == dest:
                if item.name == ".bt_cache":  # bản dịch đã lưu (tính theo tên file kết quả) đi theo sách
                    for f in item.iterdir():
                        move(f, dest / ".bt_cache" / f.name)
                continue
            if item.is_file():
                move(item, dest / item.name)
            elif item.name == "discworld" and not (item / PROJECT_FILE).exists():
                for f in item.iterdir():  # bộ khởi đầu cũ: đưa thẳng vào dự án
                    if f.is_file():
                        move(f, dest / f.name)
                if not any(item.iterdir()):
                    item.rmdir()
        # Chép cài đặt chung sang dự án, sửa đường dẫn theo chỗ mới.
        common = self.load_settings()
        if common:
            fixed = {}
            for key, value in common.items():
                if isinstance(value, str):
                    prefix = "file:" if value.startswith("file:") else ""
                    value = prefix + moves.get(value[len(prefix):], value[len(prefix):])
                fixed[key] = value
            # Sổ tay đang dùng (nếu đã chuyển vào) thành sổ tay của dự án.
            bible = Path(str(fixed.get("bible") or "")).name
            fixed["bible"] = bible if bible and (dest / bible).exists() else PROJECT_BIBLE
            info = self._project_info(project)
            info["settings"] = {**(info.get("settings") or {}), **fixed}
            self._write_project(project, info)
        return sorted(moves.values())

    def _count_common(self) -> int:
        """Số file sách/sổ tay/hướng dẫn còn nằm ở thư mục chung (để gợi ý chuyển vào dự án)."""
        files = self.list_files("")
        return len(set(files["books"] + files["translations"] + files["briefs"] + files["bibles"] + files["jsons"]))

    def rel_any(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    def progress(self, job: Job) -> dict | None:
        """Tiến độ cả cuốn (file <kết quả>.progress.json do tiến trình dịch ghi ra)."""
        if job.action not in ("translate", "analyze", "postedit") or not job.outputs:
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

    def list_files(self, project: str = "") -> dict:
        base = self.project_dir(project)
        others = [d for d in self._project_dirs() if d != base]
        files = sorted(
            p for p in base.rglob("*")
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(base).parts)
            and not p.name.endswith(".progress.json") and p.name != PROJECT_FILE
            and not any(p.is_relative_to(d) for d in others)  # thư mục chung: không lẫn file của các dự án
        )
        rels = [self.rel(p) for p in files]
        reports = (".preview.md", ".consistency.md")
        is_result = lambda r: ".vi." in r or ".hieu-dinh." in r or r.endswith(reports)
        return {
            # File hướng dẫn (huong-dan*.md) không phải sách.
            "books": [r for r in rels if r.lower().endswith(SUPPORTED) and not is_result(r)
                      and not (r.endswith(".md") and Path(r).name.startswith("huong-dan"))],
            "translations": [r for r in rels if r.lower().endswith(SUPPORTED) and is_result(r) and not r.endswith(reports)],
            "briefs": [r for r in rels if r.endswith(".md") and not r.endswith(reports)],
            "bibles": [r for r in rels if r.endswith(".bible.json")],
            "jsons": [r for r in rels if r.endswith(".json") and not r.endswith((".bible.json", ".edits.json"))],
            "edits": [r for r in rels if r.endswith(".edits.json")],
            "results": [r for r in rels if is_result(r)],
        }

    def install_starter(self, project: str = "") -> list[str]:
        """Chép bộ khởi đầu Discworld (hướng dẫn dịch + tên riêng) vào dự án (hoặc thư mục chung/discworld)."""
        src = EXAMPLES_DIR / "discworld"
        if not src.is_dir():
            raise FileNotFoundError("Không tìm thấy thư mục examples/discworld (hãy cài từ mã nguồn git clone)")
        dest = self.project_dir(project) if project else self.workspace / "discworld"
        dest.mkdir(exist_ok=True)
        copied = []
        for f in src.iterdir():
            target = dest / f.name
            if not target.exists():
                shutil.copy2(f, target)
            copied.append(self.rel(target))
        return copied

    def install_edit_guide(self, project: str = "") -> str:
        """Tạo file hướng dẫn hiệu đính mặc định trong dự án (không ghi đè file đã có)."""
        target = self.project_dir(project) / "huong-dan-hieu-dinh.md"
        if not target.exists():
            src = EXAMPLES_DIR / "huong-dan-hieu-dinh.md"
            if src.is_file():
                shutil.copy2(src, target)
            else:
                target.write_text("# Hướng dẫn hiệu đính\n\n## Ghi chú của tôi\n- \n", encoding="utf-8")
        return self.rel(target)

    def postedit_chapters(self, book_rel: str) -> dict:
        """Các chương của bản dịch + trạng thái hiệu đính/duyệt từng chương (để làm lần lượt từng chương)."""
        book = Path(self.existing(book_rel))
        output = default_output(book)
        log = edits_path(output)
        edits, chapters = existing_edits(log, book)
        return {
            "chapters": chapter_overview(load_document(book), chapters, edits),
            "edits_path": self.rel(log) if log.exists() and (edits or chapters) else "",
            "output": self.rel(output),
        }

    def apply_edits(self, rel: str, decisions: dict, chapters: dict | None = None) -> dict:
        """Xuất lại bản hiệu đính theo các chỗ sửa người dùng đã nhận/bỏ trên giao diện."""
        path = self.resolve(rel)
        if not path.name.endswith(".edits.json") or not path.is_file():
            raise FileNotFoundError(f"Không tìm thấy file chỗ sửa: {rel}")
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("translated", "output"):  # file sửa tay không được trỏ ra ngoài thư mục làm việc
            target = Path(data.get(key) or "")
            target = target if target.is_absolute() else path.parent / target
            if not target.resolve().is_relative_to(self.workspace):
                raise PermissionError("File chỗ sửa trỏ ra ngoài thư mục làm việc")
        status = {str(k): str(v) for k, v in (chapters or {}).items()}
        applied, failed = apply_edits_file(path, {int(k): bool(v) for k, v in (decisions or {}).items()}, None, status)
        out = Path(data["output"])
        out = out if out.is_absolute() else path.parent / out
        return {"applied": applied, "failed": failed, "output": self.rel(out)}

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

    def test_deepseek(self) -> dict:
        try:
            models = list_models("deepseek", self.keys.get("deepseek"))
        except ProviderUnavailable as e:
            return {"ok": False, "message": str(e), "models": []}
        return {"ok": True, "message": f"Kết nối được. Model dùng được: {', '.join(models)}", "models": models}

    def state(self, project: str = "") -> dict:
        if project:
            self.project_dir(project)  # báo lỗi nếu dự án không còn
        return {
            "workspace": str(self.workspace),
            "project": project,
            "projects": self.projects(),
            "common_files": self._count_common(),
            "files": self.list_files(project),
            "genres": [{"key": g.key, "name": g.name, "description": g.description, "effort": g.effort} for g in GENRES.values()],
            "styles": [{"key": s.key, "name": s.name, "description": s.description} for s in STYLES.values()],
            "presets": list(PRESETS),
            "levels": [{"key": k, "name": name, "description": how} for k, (name, how) in LEVELS.items()],
            "providers": self.providers_status(),
            "jobs": [j.summary() | {"progress": self.progress(j)}
                     for j in sorted(self.jobs.values(), key=lambda j: j.started, reverse=True)],
            "settings": self.load_settings(project),
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

        base = self.project_dir(str(opts.get("project") or ""))  # file mới (sổ tay, hướng dẫn...) tạo trong dự án

        if action == "learn":
            ban_dich = file_opt("ban_dich")
            if not ban_dich:
                raise ValueError("Chưa chọn bản dịch tham chiếu")
            out = base / _safe_name(opts.get("style_output") or "phong-cach.json")
            if out.suffix != ".json":
                out = out.with_suffix(".json")
            argv = ["hoc-phong-cach", ban_dich, f"--output={out}", f"--ten={opts.get('style_name') or out.stem}"]
            if ban_goc := file_opt("ban_goc"):
                argv.append(f"--ban-goc={ban_goc}")
            argv += self._api_args(opts)
            return action, argv, [self.rel(out)]

        def bible_path(default_stem: str) -> Path:
            rel = _safe_name(opts.get("bible") or f"{default_stem}.bible.json")
            if not rel.endswith(".bible.json"):
                rel = rel.removesuffix(".json") + ".bible.json"
            return base / rel

        def guide_path() -> Path:
            rel = (opts.get("pe_guide") or "").strip()
            path = self.resolve(rel) if rel else base / "huong-dan-hieu-dinh.md"
            if path.suffix != ".md":
                raise ValueError("Hướng dẫn hiệu đính phải là file .md")
            return path

        # AI hiệu đính / rút bài học = các AI đã chọn ở ô "Người biên tập".
        editor_opts = opts | {"providers": opts.get("editors") or []}

        if action == "learn_edits":
            argv = ["hoc-tu-ban-sua"]
            if opts.get("le_mode") == "docs":
                tho, sua = file_opt("le_tho"), file_opt("le_sua")
                if not (tho and sua):
                    raise ValueError("Chọn cả bản trước khi sửa và bản đã sửa")
                argv += [f"--ban-tho={tho}", f"--ban-sua={sua}"]
                if goc := file_opt("le_goc"):
                    argv.append(f"--ban-goc={goc}")
            else:
                edits = file_opt("edits_json")
                if not edits:
                    raise ValueError("Chưa chọn file chỗ sửa (.edits.json)")
                argv.append(f"--edits-json={edits}")
            bible, guide = bible_path("so-tay"), guide_path()
            argv += [f"--so-tay={bible}", f"--huong-dan-hieu-dinh={guide}", *self._api_args(editor_opts)]
            return action, argv, [self.rel(guide), self.rel(bible)]

        commands = {"preview": "dich-thu", "analyze": "phan-tich", "translate": "dich", "postedit": "hieu-dinh"}
        if action not in commands:
            raise ValueError(f"Tác vụ không hợp lệ: {action}")
        book = file_opt("pe_translated" if action == "postedit" else "book")
        if not book:
            raise ValueError("Chưa chọn bản dịch cần hiệu đính" if action == "postedit" else "Chưa chọn sách")
        genre = opts.get("genre") or "general"
        if genre not in GENRES:
            raise ValueError(f"Thể loại không hợp lệ: {genre}")
        bible = bible_path(Path(book).stem)

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

        if action == "postedit":
            argv += self._api_args(editor_opts)
            if source := file_opt("pe_source"):
                argv.append(f"--ban-goc={source}")
            if bible.exists():
                argv.append(f"--so-tay={bible}")
            guide = guide_path()
            if guide.exists():
                argv.append(f"--huong-dan-hieu-dinh={guide}")
            if (level := opts.get("pe_level") or "vua") not in LEVELS:
                raise ValueError(f"Mức hiệu đính không hợp lệ: {level}")
            argv.append(f"--muc-hieu-dinh={level}")
            if chapters := (opts.get("pe_chapters") or "").strip():
                if not re.fullmatch(r"[\d\s,\-]+", chapters):
                    raise ValueError("Chương cần hiệu đính ghi dạng 1,3-5")
                argv.append(f"--chuong={chapters}")
            if note := (opts.get("pe_note") or "").strip():
                argv.append(f"--yeu-cau={note}")
            out = default_output(Path(book))
            return action, argv, [self.rel(out), self.rel(edits_path(out))]

        argv += self._api_args(opts)

        outputs: list[str] = []
        if action == "analyze":
            argv.append(f"--output={bible}")
            outputs.append(self.rel(bible))
        else:
            argv.append(f"--so-tay={bible}")
            if editors := provider_spec(opts.get("editors") or [], opts.get("models")):
                argv.append(f"--bien-tap-bang={editors}")
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
        if providers := provider_spec(opts.get("providers") or [], opts.get("models")):
            args.append(f"--providers={providers}")
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
        env = dict(os.environ, PYTHONIOENCODING="utf-8:backslashreplace", PYTHONUTF8="1")
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
                return self._json(app.state(q("project")))
            if method == "POST" and path == "/api/projects":
                data = json.loads(self._body() or b"{}")
                return self._json(app.create_project(data.get("name", ""), data.get("description", ""), data.get("genre", ""),
                                                     bool(data.get("starter")), bool(data.get("move_common"))))
            if method == "POST" and path == "/api/projects/move-common":
                return self._json({"moved": app.move_common_into(q("project"))})
            if method == "POST" and path == "/api/keys":
                data = json.loads(self._body() or b"{}")
                for name in API_KEY_ENV:
                    if name in data:
                        app.keys[name] = str(data[name]).strip()
                return self._json({"providers": app.providers_status()})
            if path == "/api/settings":
                if method == "PUT":
                    app.save_settings(json.loads(self._body() or b"{}"), q("project"))
                    return self._json({"saved": True})
                return self._json(app.load_settings(q("project")))
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
            if method == "POST" and path == "/api/test-deepseek":
                return self._json(app.test_deepseek())
            if method == "POST" and path == "/api/test-codex":
                return self._json(app.test_codex())
            if method == "POST" and path == "/api/postedit/guide":
                return self._json({"path": app.install_edit_guide(q("project"))})
            if method == "POST" and path == "/api/postedit/apply":
                data = json.loads(self._body() or b"{}")
                return self._json(app.apply_edits(str(data.get("path", "")), data.get("decisions") or {}, data.get("chapters")))
            if method == "GET" and path == "/api/postedit/chapters":
                return self._json(app.postedit_chapters(q("book")))
            if method == "POST" and path == "/api/starter":
                return self._json({"files": app.install_starter(q("project"))})
            if method == "POST" and path == "/api/upload":
                target = app.project_dir(q("project")) / _safe_name(q("name"))
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
