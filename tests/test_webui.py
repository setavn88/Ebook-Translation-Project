import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from booktranslator.webui import App, _safe_name, make_handler


@pytest.fixture
def server(tmp_path):
    app = App(tmp_path / "ws")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, method="GET", body=None, token=True, raw=False):
        data = body if isinstance(body, bytes) or body is None else (body if isinstance(body, str) else json.dumps(body)).encode()
        req = urllib.request.Request(base + path, data=data, method=method)
        if token:
            req.add_header("X-BT-Token", app.token)
        try:
            with urllib.request.urlopen(req) as res:
                content = res.read()
                return res.status, (content if raw else json.loads(content))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    yield app, call, base
    httpd.shutdown()


def test_index_embeds_token_and_api_requires_it(server):
    app, call, base = server
    html = urllib.request.urlopen(base + "/").read().decode()
    assert app.token in html and "BookTranslator" in html
    assert call("/api/state", token=False)[0] == 403
    status, state = call("/api/state")
    assert status == 200 and {g["key"] for g in state["genres"]} >= {"comic", "fantasy"}
    assert set(state["providers"]) == {"claude-code", "codex", "claude", "deepseek", "openai"}


def test_upload_files_and_text_editing(server):
    app, call, _ = server
    status, r = call("/api/upload?name=../../evil/The%20Colour.epub", "POST", b"epubdata")
    assert status == 200 and r["path"] == "The Colour.epub"  # bỏ thư mục, không thoát ra ngoài
    assert (app.workspace / "The Colour.epub").read_bytes() == b"epubdata"
    assert call("/api/state")[1]["files"]["books"] == ["The Colour.epub"]

    assert call("/api/file?path=notes/huong-dan.md", "PUT", "# Hướng dẫn")[0] == 200
    assert call("/api/file?path=notes/huong-dan.md")[1]["content"] == "# Hướng dẫn"
    assert call("/api/file?path=x.bible.json", "PUT", "{hỏng")[0] == 400  # không lưu JSON hỏng
    assert call("/api/file?path=../../etc/passwd.txt")[0] == 400
    assert call("/api/file?path=script.py", "PUT", "print(1)")[0] == 400

    status, content = call("/api/download?path=The%20Colour.epub", raw=True)
    assert status == 200 and content == b"epubdata"


def test_build_job_argv(server):
    app, _, _ = server
    (app.workspace / "book.epub").write_bytes(b"x")
    (app.workspace / "dw.md").write_text("hd")
    (app.workspace / "lylan-hp.json").write_text("{}")
    action, argv, outputs = app.build_job({
        "action": "preview", "book": "book.epub", "genre": "comic", "style": "file:lylan-hp.json", "brief": "dw.md",
        "bible": "discworld", "muc": "tiet-kiem", "providers": ["claude-code", "deepseek", "rm -rf"], "polish": "on",
        "note": "--fresh không phải tùy chọn", "chunks": "3",
    })
    ws = app.workspace
    assert argv[:3] == ["dich-thu", str(ws / "book.epub"), "--the-loai=comic"]
    assert f"--phong-cach-file={ws / 'lylan-hp.json'}" in argv and f"--huong-dan={ws / 'dw.md'}" in argv
    assert "--providers=claude-code,deepseek" in argv and "--muc=tiet-kiem" in argv and "--polish" in argv
    assert "--yeu-cau=--fresh không phải tùy chọn" in argv and "--fresh" not in argv
    assert f"--so-tay={ws / 'discworld.bible.json'}" in argv and "--chunks=3" in argv
    assert outputs == ["book.vi.epub.preview.md", "discworld.bible.json"]

    assert "--khong-cho" not in argv
    _, argv, _ = app.build_job({"action": "preview", "book": "book.epub", "providers": ["codex", "deepseek"],
                                "editors": ["claude-code", "bad"]})
    assert "--providers=codex,deepseek" in argv and "--bien-tap-bang=claude-code" in argv
    _, argv, _ = app.build_job({"action": "translate", "book": "book.epub", "wait": False})
    assert "--khong-cho" in argv
    _, argv, _ = app.build_job({"action": "translate", "book": "book.epub", "wait": True, "wait_hours": "48"})
    assert "--cho-han-muc=48.0" in argv
    _, argv, outputs = app.build_job({"action": "translate", "book": "book.epub", "genre": "comic", "style": "lylan"})
    assert argv[0] == "dich" and "--phong-cach=lylan" in argv
    assert outputs == ["book.vi.epub", "book.vi.epub.consistency.md", "book.bible.json"]

    with pytest.raises(ValueError):
        app.build_job({"action": "translate", "book": "book.epub", "genre": "bad"})
    with pytest.raises(FileNotFoundError):
        app.build_job({"action": "preview", "book": "missing.epub"})
    with pytest.raises(ValueError):
        app.build_job({"action": "rm"})


def test_job_runs_and_streams_output(server):
    app, call, _ = server
    app.build_job = lambda opts: ("preview", ["the-loai"], [])  # lệnh vô hại, không gọi AI
    status, job = call("/api/jobs", "POST", {"action": "preview"})
    assert status == 200
    for _ in range(100):
        status, info = call(f"/api/jobs/{job['id']}")
        if info["status"] != "running":
            break
        time.sleep(0.1)
    assert info["status"] == "done" and any("comic" in line for line in info["lines"])


def test_only_one_job_at_a_time(server):
    app, call, _ = server
    app.build_job = lambda opts: ("preview", ["giao-dien", "--no-browser", "--port", "0", "--thu-muc", str(app.workspace)], [])
    status, job = call("/api/jobs", "POST", {})
    assert status == 200
    assert call("/api/jobs", "POST", {})[0] == 400
    call(f"/api/jobs/{job['id']}/cancel", "POST")
    for _ in range(50):
        if call(f"/api/jobs/{job['id']}")[1]["status"] != "running":
            break
        time.sleep(0.1)
    assert call(f"/api/jobs/{job['id']}")[1]["status"] == "cancelled"


def test_starter_kit(server):
    app, call, _ = server
    status, r = call("/api/starter", "POST")
    assert status == 200 and "discworld/huong-dan-dich.md" in r["files"]
    assert (app.workspace / "discworld" / "ten-rieng.json").exists()


def test_keys_are_memory_only(server):
    app, call, _ = server
    status, r = call("/api/keys", "POST", {"deepseek": "sk-test"})
    assert status == 200 and app.keys["deepseek"] == "sk-test"
    assert not any("sk-test" in p.read_text(errors="ignore") for p in app.workspace.rglob("*") if p.is_file())


def test_safe_name():
    assert _safe_name("../a/b/--evil.epub") == "evil.epub"
    assert _safe_name("C:\\\\Users\\\\x\\\\sách hay.epub") == "sách hay.epub"
    assert _safe_name("...") == "file"


def test_claude_path_and_connection_test(server, tmp_path, monkeypatch):
    from test_providers import FAKE_CLAUDE

    app, call, _ = server
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.delenv("BT_CLAUDE_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert call("/api/claude-path", "POST", {"path": str(tmp_path / "khong-co.exe")})[0] == 400
    exe = tmp_path / "claude"
    exe.write_text(FAKE_CLAUDE)
    exe.chmod(0o755)
    status, r = call("/api/claude-path", "POST", {"path": str(exe)})
    assert status == 200 and r["providers"]["claude-code"] is True
    status, r = call("/api/test-claude", "POST")
    assert status == 200 and r["ok"] is True and r["path"] == str(exe)


# ---- tắt phần mềm mở lại: không mất gì ------------------------------------


def _wait_done(call, job_id):
    for _ in range(100):
        status, info = call(f"/api/jobs/{job_id}")
        if info["status"] != "running":
            return info
        time.sleep(0.1)
    raise AssertionError("tác vụ không kết thúc")


def test_jobs_settings_and_progress_survive_restart(server):
    app, call, _ = server
    # cài đặt lưu trên ổ đĩa
    assert call("/api/settings", "PUT", {"genre": "comic", "providers": ["claude-code"]})[0] == 200
    # một tác vụ chạy xong
    app.build_job = lambda opts: ("translate", ["the-loai"], ["book.vi.epub"])
    status, job = call("/api/jobs", "POST", {"action": "translate", "book": "book.epub"})
    _wait_done(call, job["id"])
    # tiến trình dịch ghi file tiến độ
    (app.workspace / "book.vi.epub.progress.json").write_text(json.dumps({"step": "Dịch", "done": 37, "total": 120}))
    # một tác vụ đang chạy dở thì phần mềm bị tắt
    (app.jobs_dir / "dead.json").write_text(json.dumps({
        "id": "dead", "action": "translate", "argv": [], "outputs": ["book.vi.epub"], "opts": {"book": "book.epub"},
        "status": "running", "returncode": None, "started": time.time() + 10}))

    app2 = App(app.workspace)  # mở lại phần mềm
    state = app2.state()
    assert state["settings"] == {"genre": "comic", "providers": ["claude-code"]}
    last = state["jobs"][0]
    assert last["id"] == "dead" and last["status"] == "interrupted"
    assert last["opts"] == {"book": "book.epub"} and last["progress"]["done"] == 37
    done = app2.jobs[job["id"]]
    assert done.status == "done" and any("comic" in line for line in done.lines)  # nhật ký còn nguyên
    # file nội bộ không lẫn vào danh sách file của người dùng
    files = state["files"]
    assert not any(".bt_ui" in f or f.endswith(".progress.json") for group in files.values() for f in group)


def test_old_jobs_are_pruned(tmp_path):
    from booktranslator.webui import MAX_JOBS_KEPT

    app = App(tmp_path)
    for i in range(MAX_JOBS_KEPT + 5):
        (app.jobs_dir / f"j{i}.json").write_text(json.dumps({
            "id": f"j{i}", "action": "preview", "argv": [], "outputs": [], "status": "done", "started": i}))
    assert len(App(tmp_path).jobs) == MAX_JOBS_KEPT


def test_codex_path_setting(server, tmp_path, monkeypatch):
    app, call, _ = server
    import booktranslator.providers as prov

    monkeypatch.setattr(prov.shutil, "which", lambda name: None)
    monkeypatch.setattr(prov, "_npm_prefix_cache", [None])
    monkeypatch.setenv("BT_CODEX_PATH", "")
    assert call("/api/codex-path", "POST", {"path": str(tmp_path / "khong-co.cmd")})[0] == 400
    exe = tmp_path / "codex.cmd"
    exe.write_text("")
    status, r = call("/api/codex-path", "POST", {"path": f'"{exe}"'})
    assert status == 200 and r["providers"]["codex"] is True and r["path"] == str(exe)


def test_provider_spec_with_models():
    from booktranslator.webui import provider_spec

    assert provider_spec(["codex", "deepseek"], {"deepseek": "deepseek-reasoner"}) == "codex,deepseek:deepseek-reasoner"
    assert provider_spec(["deepseek", "bad"], {"deepseek": "x; rm -rf /"}) == "deepseek"  # model lạ bị bỏ qua


def test_postedit_jobs_review_and_apply(server):
    app, call, _ = server
    ws = app.workspace
    (ws / "book.epub").write_bytes(b"x")
    (ws / "book.vi.md").write_text("Câu thô một.\n\nCâu hay.", encoding="utf-8")
    (ws / "so.bible.json").write_text('{"entities": []}', encoding="utf-8")
    files = call("/api/state")[1]["files"]
    assert files["translations"] == ["book.vi.md"] and files["books"] == ["book.epub"]

    status, r = call("/api/postedit/guide", "POST")
    assert status == 200 and (ws / r["path"]).read_text(encoding="utf-8").startswith("# Hướng dẫn hiệu đính")

    opts = {"action": "postedit", "pe_translated": "book.vi.md", "pe_source": "book.epub", "pe_guide": r["path"],
            "pe_level": "sau", "pe_chapters": "1,3-5", "bible": "so.bible.json", "genre": "comic",
            "providers": ["codex"], "editors": ["claude-code", "codex"]}
    action, argv, outputs = app.build_job(opts)
    assert argv[:2] == ["hieu-dinh", str(ws / "book.vi.md")]
    assert "--providers=claude-code,codex" in argv and f"--ban-goc={ws / 'book.epub'}" in argv
    assert "--muc-hieu-dinh=sau" in argv and "--chuong=1,3-5" in argv and f"--so-tay={ws / 'so.bible.json'}" in argv
    assert outputs == ["book.vi.hieu-dinh.md", "book.vi.hieu-dinh.md.edits.json"]
    with pytest.raises(ValueError):
        app.build_job(opts | {"pe_chapters": "1; rm -rf"})
    with pytest.raises(ValueError):
        app.build_job(opts | {"pe_level": "x"})

    action, argv, outputs = app.build_job({"action": "learn_edits", "le_mode": "docs", "le_tho": "book.vi.md",
                                           "le_sua": "book.vi.md", "bible": "so.bible.json", "pe_guide": r["path"]})
    assert argv[0] == "hoc-tu-ban-sua" and f"--huong-dan-hieu-dinh={ws / r['path']}" in argv
    assert outputs == [r["path"], "so.bible.json"]
    with pytest.raises(ValueError):
        app.build_job({"action": "learn_edits"})

    # Duyệt: bỏ chỗ sửa số 1 rồi xuất lại.
    from booktranslator.postedit import Edit, save_edits

    out, log = ws / "book.vi.hieu-dinh.md", ws / "book.vi.hieu-dinh.md.edits.json"
    edits = [Edit(1, "sec-0001", "", 0, "thô", "mượt"), Edit(2, "sec-0001", "", 1, "hay", "rất hay")]
    save_edits(log, ws / "book.vi.md", None, out, "vua", edits)
    assert call("/api/state")[1]["files"]["edits"] == ["book.vi.hieu-dinh.md.edits.json"]
    assert "book.vi.hieu-dinh.md.edits.json" not in call("/api/state")[1]["files"]["jsons"]
    status, r = call("/api/postedit/apply", "POST", {"path": log.name, "decisions": {"1": False, "2": True}})
    assert status == 200 and r == {"applied": 1, "failed": 0, "output": "book.vi.hieu-dinh.md"}
    assert out.read_text(encoding="utf-8") == "Câu thô một.\n\nCâu rất hay.\n"
    assert json.loads(log.read_text(encoding="utf-8"))["edits"][0]["accepted"] is False

    data = json.loads(log.read_text(encoding="utf-8"))
    data["output"] = "/tmp/evil.md"
    log.write_text(json.dumps(data), encoding="utf-8")
    assert call("/api/postedit/apply", "POST", {"path": log.name})[0] == 400
