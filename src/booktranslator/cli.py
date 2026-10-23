"""Giao diện dòng lệnh.

Lệnh (tên tiếng Việt hoặc tiếng Anh đều được):
  the-loai | genres          liệt kê thể loại
  phong-cach | styles        liệt kê phong cách dịch
  hoc-phong-cach | learn-style  học phong cách từ sách tham chiếu
  phan-tich | analyze        lập sổ tay truyện
  dich-thu | preview         dịch thử vài đoạn
  dich | translate           dịch cả cuốn
  hieu-dinh | post-edit      hiệu đính bản dịch đã có (AI chỉ trả về chỗ sửa)
  ap-hieu-dinh | apply-edits xuất lại bản hiệu đính theo các chỗ sửa đã duyệt
  hoc-tu-ban-sua | learn-edits  học từ bản đã sửa: cập nhật sổ tay + hướng dẫn hiệu đính
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from pathlib import Path

import anthropic

from . import __version__
from .bible import StoryBible
from .cache import TranslationCache
from .chunker import DEFAULT_CHUNK_CHARS
from .document import SUPPORTED, default_output_path, load_document
from .genres import BUILTIN as GENRES, load_genre
from .pipeline import analyze_document, preview_document, translate_document
from .learnedits import append_guide, changes_from_docs, changes_from_edits_file, learn_from_changes, summarize, update_bible
from .postedit import (
    CHAPTER_APPROVED,
    CHAPTER_REVIEW,
    LEVELS,
    PostEditor,
    apply_edits_file,
    default_output,
    edits_path,
    existing_edits,
    load_edits,
    mark_chapters,
    parse_sections,
    postedit_document,
    save_edits,
)
from .providers import ProviderUnavailable, build_chain, parse_spec
from .styles import BUILTIN as STYLES, load_style
from .stylelearn import learn_style
from .translator import TranslationError, Translator, TranslatorConfig

_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# Mức chất lượng / chi phí: chọn model Claude cho biên tập, giới hạn effort và bật/tắt nhuận sắc.
PRESETS = {
    "tiet-kiem": {"model": "claude-sonnet-5-5", "polish": False, "max_effort": "medium"},
    "can-bang": {"model": "claude-opus-5-5", "polish": True, "max_effort": None},
    "cao-cap": {"model": "claude-opus-5-5", "polish": True, "min_effort": "high"},
}


def _load_glossary(path: Path | None) -> dict[str, str]:
    if not path:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: bảng thuật ngữ phải là JSON object dạng {{\"nguồn\": \"đích\"}}")
    return {str(k): str(v) for k, v in data.items()}


def _add_api_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--providers", default=DEFAULT_TRANSLATORS,
        help="Nhà cung cấp theo thứ tự ưu tiên, dạng tên[:model], cách nhau dấu phẩy. "
             "Chỉ dùng codex (gói ChatGPT) và deepseek. Mặc định: codex,deepseek; hết quota/lỗi thì tự chuyển sang cái kế tiếp. "
             "Ví dụ: codex,deepseek:deepseek-flash hoặc deepseek,codex.",
    )
    p.add_argument("--model", help="Model cho nhà cung cấp đầu tiên (ghi đè mức --muc)")
    p.add_argument("--effort", choices=_EFFORTS, help="Mức suy nghĩ của mô hình; mặc định theo thể loại và --muc")
    p.add_argument("--no-fallback", action="store_true", help="Tắt fallback phía server của Claude (cần khi dùng qua Bedrock/Vertex)")
    p.add_argument("--cho-han-muc", dest="wait_hours", type=float, default=12, metavar="GIỜ",
                   help="Hết hạn mức (vd. gói Claude Pro) thì tự chờ tối đa bấy nhiêu giờ rồi dịch tiếp (mặc định 12)")
    p.add_argument("--khong-cho", dest="wait_hours", action="store_const", const=0,
                   help="Hết hạn mức thì dừng luôn (tiến độ vẫn lưu, chạy lại sẽ tiếp tục)")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("input", type=Path, help=f"File sách ({', '.join(SUPPORTED)})")
    _add_profile_opts(p)
    p.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS, help="Số ký tự tối đa mỗi đoạn gửi đi")
    _add_api_opts(p)


def _add_profile_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("-g", "--the-loai", "--genre", dest="genre", default="general", choices=list(GENRES),
                   help="Thể loại sách (xem lệnh the-loai)")
    p.add_argument("--the-loai-file", "--genre-file", dest="genre_file", type=Path, help="File JSON thể loại tự định nghĩa")
    p.add_argument("-p", "--phong-cach", "--translator-style", dest="style", default="natural", choices=list(STYLES),
                   help="Phong cách dịch có sẵn (xem lệnh phong-cach)")
    p.add_argument("--phong-cach-file", "--style-file", dest="style_file", type=Path,
                   help="File phong cách dịch (tự học bằng hoc-phong-cach hoặc tự viết) — ghi đè --phong-cach")
    p.add_argument("--muc", choices=list(PRESETS), default="can-bang",
                   help="Mức chất lượng/chi phí: tiet-kiem | can-bang (mặc định) | cao-cap")
    p.add_argument("--to", dest="target", default="Tiếng Việt", help="Ngôn ngữ đích (mặc định: Tiếng Việt)")
    p.add_argument("--from", dest="source", default="tự nhận diện", help="Ngôn ngữ nguồn (mặc định: tự nhận diện)")
    p.add_argument("--huong-dan", "--prompt", dest="brief", type=Path,
                   help="File hướng dẫn dịch riêng cho cuốn/bộ sách (Markdown), đưa vào prompt của mọi bước")
    p.add_argument("--glossary", type=Path, help="File JSON {\"nguồn\": \"đích\"} nạp thêm vào sổ tay")
    p.add_argument("--workers", type=int, default=4, help="Số chương xử lý song song (mặc định: 4)")
    p.add_argument("--cache-dir", type=Path, help="Thư mục lưu tiến độ (mặc định: .bt_cache cạnh file kết quả)")


def _add_translate_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("-o", "--output", type=Path, help="File kết quả (mặc định: <tên>.<mã ngôn ngữ>.<đuôi>)")
    p.add_argument("--lang-code", default="vi", help="Mã ngôn ngữ đích ghi vào EPUB và tên file (mặc định: vi)")
    p.add_argument("--yeu-cau", "--note", dest="note", default="", help="Yêu cầu riêng cho bản dịch, ưu tiên cao nhất")
    p.add_argument("--so-tay", "--bible", dest="bible", type=Path, help="File sổ tay truyện (mặc định: <file kết quả>.bible.json)")
    p.add_argument("--polish", dest="polish", action="store_true", default=None, help="Bật bước biên tập (mặc định theo --muc)")
    p.add_argument("--bien-tap-bang", "--editor-providers", dest="editor_providers",
                   help="AI làm bước biên tập, khác với người dịch nháp (cùng cú pháp --providers). "
                        "Mặc định: claude-code,codex,deepseek (Claude Code biên tập; hết hạn mức thì Codex, "
                        "rồi mới đến DeepSeek). Vd: --providers codex,deepseek --bien-tap-bang claude-code,codex")
    p.add_argument("--no-polish", dest="polish", action="store_false", help="Tắt bước biên tập (rẻ hơn ~một nửa)")
    p.add_argument("--fresh", action="store_true", help="Bỏ qua bản dịch đã lưu, dịch lại từ đầu")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="booktranslator", description="Dịch sách bằng AI: nhất quán, có chiều sâu, đúng phong cách dịch giả.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    ui = sub.add_parser("giao-dien", aliases=["ui"], help="Mở giao diện trên trình duyệt (dễ dùng nhất)")
    ui.set_defaults(cmd="ui")
    ui.add_argument("--thu-muc", "--workspace", dest="workspace", type=Path, default=Path.home() / "BookTranslator",
                    help="Thư mục chứa sách và kết quả (mặc định: ~/BookTranslator)")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true", help="Không tự mở trình duyệt")

    sub.add_parser("the-loai", aliases=["genres"], help="Liệt kê thể loại").set_defaults(cmd="genres")
    sub.add_parser("phong-cach", aliases=["styles"], help="Liệt kê phong cách dịch").set_defaults(cmd="styles")

    ls = sub.add_parser("hoc-phong-cach", aliases=["learn-style"], help="Học phong cách dịch từ một cuốn sách tham chiếu")
    ls.set_defaults(cmd="learn")
    ls.add_argument("ban_dich", type=Path, help="Bản dịch tiếng Việt của sách tham chiếu (EPUB/TXT/MD)")
    ls.add_argument("--ban-goc", type=Path, help="Bản gốc tiếng Anh của cùng cuốn sách (khuyên dùng: học được cách dịch từng câu, từng tên)")
    ls.add_argument("-o", "--output", type=Path, required=True, help="File hồ sơ phong cách JSON sẽ tạo")
    ls.add_argument("--ten", default="", help="Tên phong cách, vd. \"Lý Lan - Harry Potter\"")
    ls.add_argument("--so-trich-doan", type=int, default=4, help="Số trích đoạn đọc trong sách (mặc định 4; nhiều hơn = kỹ hơn, tốn hơn)")
    _add_api_opts(ls)

    a = sub.add_parser("phan-tich", aliases=["analyze"], help="Lập sổ tay truyện (nhân vật, tên riêng, xưng hô, sự kiện) để duyệt trước")
    a.set_defaults(cmd="analyze")
    _add_common(a)
    a.add_argument("-o", "--output", type=Path, help="File sổ tay JSON (mặc định: <tên sách>.bible.json)")

    pv = sub.add_parser("dich-thu", aliases=["preview"], help="Dịch thử vài đoạn: bản gốc / bản nháp / bản biên tập")
    pv.set_defaults(cmd="preview")
    _add_common(pv)
    _add_translate_opts(pv)
    pv.add_argument("--section", type=int, help="Số thứ tự chương để dịch thử (mặc định: chương đầu tiên đủ dài)")
    pv.add_argument("--chunks", type=int, default=2, help="Số đoạn dịch thử (mặc định: 2, mỗi đoạn ~6000 ký tự)")

    t = sub.add_parser("dich", aliases=["translate"], help="Dịch cả cuốn sách")
    t.set_defaults(cmd="translate")
    _add_common(t)
    _add_translate_opts(t)
    t.add_argument("--no-analyze", action="store_true", help="Bỏ bước đọc trước cả sách (rẻ hơn, kém nhất quán hơn)")
    t.add_argument("--no-fix", action="store_true", help="Chỉ báo cáo lỗi nhất quán, không tự dịch lại")

    pe = sub.add_parser("hieu-dinh", aliases=["post-edit"],
                        help="Hiệu đính bản dịch đã có: AI chỉ trả về các chỗ cần sửa (tiết kiệm token), lưu để duyệt")
    pe.set_defaults(cmd="postedit")
    pe.add_argument("ban_dich", type=Path, help="Bản dịch cần hiệu đính (EPUB/TXT/MD)")
    pe.add_argument("--ban-goc", type=Path, help="Bản gốc tiếng Anh (khuyên dùng: AI đối chiếu được nghĩa)")
    pe.add_argument("-o", "--output", type=Path, help="File bản hiệu đính (mặc định: <bản dịch>.hieu-dinh.<đuôi>)")
    pe.add_argument("--huong-dan-hieu-dinh", "--edit-guide", dest="guide", type=Path,
                    help="File hướng dẫn hiệu đính (Markdown): yêu cầu riêng của bạn cho bước sửa")
    pe.add_argument("--muc-hieu-dinh", "--edit-level", dest="level", choices=list(LEVELS), default="vua",
                    help="nhe: chỉ sửa lỗi rõ · vua (mặc định): sửa cả câu thô, nghĩa đen · sau: viết lại mạnh tay")
    pe.add_argument("--chuong", help="Chỉ hiệu đính các chương này, vd. \"1,3-5\" (mặc định: cả cuốn). Chỗ sửa của các chương "
                                     "làm trước được giữ, file kết quả luôn là cả cuốn")
    pe.add_argument("--lam-moi", action="store_true", help="Bỏ hết chỗ sửa và tiến độ duyệt cũ, bắt đầu lại")
    pe.add_argument("--yeu-cau", "--note", dest="note", default="", help="Yêu cầu riêng, ưu tiên cao nhất")
    pe.add_argument("--so-tay", "--bible", dest="bible", type=Path,
                    help="Sổ tay truyện (mặc định: <bản dịch>.bible.json nếu có)")
    _add_profile_opts(pe)
    _add_api_opts(pe)
    pe.add_argument("--bien-tap-bang", "--editor-providers", dest="providers",
                    help="AI hiệu đính (cùng cú pháp --providers). Mặc định: claude-code,codex,deepseek")
    pe.set_defaults(providers=DEFAULT_EDITORS)

    ap = sub.add_parser("ap-hieu-dinh", aliases=["apply-edits"],
                        help="Xuất lại bản hiệu đính từ file .edits.json sau khi duyệt (nhận/bỏ từng chỗ sửa)")
    ap.set_defaults(cmd="apply_edits")
    ap.add_argument("edits", type=Path, help="File <bản hiệu đính>.edits.json")
    ap.add_argument("--bo", default="", help="Số thứ tự các chỗ sửa muốn BỎ, vd. \"3,7-9\"")
    ap.add_argument("--nhan", default="", help="Số thứ tự các chỗ sửa muốn NHẬN lại, vd. \"3\"")
    ap.add_argument("--duyet-chuong", default="", help="Đánh dấu các chương này là đã duyệt, vd. \"9\"")
    ap.add_argument("-o", "--output", type=Path, help="File kết quả (mặc định: như lúc hiệu đính)")

    le = sub.add_parser("hoc-tu-ban-sua", aliases=["learn-edits"],
                        help="Học từ bản đã sửa: tên được thống nhất → sổ tay; bài học câu chữ → hướng dẫn hiệu đính")
    le.set_defaults(cmd="learn_edits")
    le.add_argument("--ban-tho", type=Path, help="Bản dịch trước khi sửa")
    le.add_argument("--ban-sua", type=Path, help="Bản đã sửa (bạn tự sửa, hoặc AI khác hiệu đính)")
    le.add_argument("--edits-json", type=Path, help="Hoặc: file .edits.json đã duyệt (nhận = học theo, bỏ = học tránh)")
    le.add_argument("--ban-goc", type=Path, help="Bản gốc tiếng Anh (tùy chọn, giúp nhận ra tên gốc)")
    le.add_argument("--so-tay", "--bible", dest="bible", type=Path, help="Sổ tay truyện cần cập nhật tên")
    le.add_argument("--huong-dan-hieu-dinh", "--edit-guide", dest="guide", type=Path,
                    help="File hướng dẫn hiệu đính để nối bài học vào (chưa có thì tạo mới)")
    le.add_argument("--chi-xem", "--dry-run", dest="dry_run", action="store_true", help="Chỉ in bài học, không ghi file")
    _add_api_opts(le)
    le.add_argument("--bien-tap-bang", "--editor-providers", dest="providers", help="AI rút bài học (mặc định: claude-code,codex,deepseek)")
    le.set_defaults(providers=DEFAULT_EDITORS)
    return parser


class _ProgressPrinter:
    """In tiến độ ra màn hình; nếu có `track()` thì ghi thêm file <kết quả>.progress.json cho giao diện đọc."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.path: Path | None = None
        self.data: dict = {}
        self._per_section: dict[tuple[str, str], int] = {}

    def track(self, path: Path, book: Path, totals: dict[str, int]) -> None:
        self.path = path
        self.data = {"book": book.name, "step": "", "done": 0, "total": 0, "totals": totals, "finished": False}

    def __call__(self, step: str, name, done: int, total: int | None = None) -> None:
        if total is None:  # (bước, đã xong, tổng)
            name, done, total = "", name, done
        with self._lock:
            label = f"{name}: " if name else ""
            print(f"  [{step}] {label}{done}/{total}", file=sys.stderr, flush=True)
            if self.path and step in self.data["totals"]:
                book_total = self.data["totals"][step]
                if step == "Dịch":
                    # Cả cuốn = tổng số đoạn đã xong của mọi chương (kể cả đoạn lấy từ bản đã lưu).
                    self._per_section[(step, str(name))] = done
                    book_done = sum(v for (st, _), v in self._per_section.items() if st == step)
                else:
                    # Phân tích báo số chương cộng dồn trong số chương CÒN LẠI; cộng thêm chương đã phân tích từ trước.
                    # Tổng 0 = chưa biết trước (vd. số nhóm đoạn hiệu đính) → lấy theo bước đang báo.
                    book_total = book_total or total
                    book_done = book_total - total + done
                self.data |= {"step": step, "done": min(book_done, book_total), "total": book_total}
                self._write()

    def finish(self) -> None:
        if self.path:
            with self._lock:
                self.data |= {"finished": True, "done": self.data.get("total", 0)}
                self._write()

    def _write(self) -> None:
        self.data["updated"] = time.time()
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _print_catalog(items) -> None:
    for item in items:
        print(f"{item.key:<9} {item.name}")
        effort = f" (effort mặc định: {item.effort})" if hasattr(item, "effort") else ""
        print(f"          {item.description}{effort}")


def _bible_summary(bible: StoryBible) -> str:
    return (
        f"{len(bible.entities)} tên riêng/thuật ngữ, {len(bible.addressing)} cặp xưng hô, "
        f"{len(bible.sections)} chương đã tóm tắt"
    )


def _effort(args, genre_effort: str) -> str:
    if args.effort:
        return args.effort
    preset = PRESETS[getattr(args, "muc", "can-bang")]
    effort = genre_effort
    if preset.get("max_effort") and _EFFORTS.index(effort) > _EFFORTS.index(preset["max_effort"]):
        effort = preset["max_effort"]
    if preset.get("min_effort") and _EFFORTS.index(effort) < _EFFORTS.index(preset["min_effort"]):
        effort = preset["min_effort"]
    return effort


DEFAULT_TRANSLATORS = "codex,deepseek"
DEFAULT_EDITORS = "claude-code,codex,deepseek"  # Claude hết hạn mức → Codex hiệu đính → cuối cùng mới DeepSeek


TRANSLATORS = {"codex", "deepseek"}  # người dịch; Claude chỉ làm biên tập / hiệu đính


def _chains(args, need_editor: bool = False, translators_only: bool = False):
    """(chuỗi chính, chuỗi phân tích, chuỗi biên tập).

    `translators_only`: chuỗi chính là người DỊCH (dich-thu / phan-tich / dich) nên chỉ nhận Codex và DeepSeek.
    Các lệnh hiệu đính / học từ bản sửa dùng chuỗi chính làm người biên tập nên được dùng Claude.
    """
    preset = PRESETS[getattr(args, "muc", "can-bang")]
    spec = parse_spec(args.providers)
    if translators_only and any(name not in TRANSLATORS for name, _ in spec):
        raise ValueError("Chuỗi dịch chỉ hỗ trợ Codex và DeepSeek; hãy đặt Claude trong --bien-tap-bang.")
    if not spec:
        raise ValueError("Cần chọn ít nhất một nhà cung cấp AI (vd. codex,deepseek).")
    translate_spec = list(spec)
    if args.model:
        translate_spec[0] = (spec[0][0], args.model)  # --model chỉ áp cho bước dịch
    wait = getattr(args, "wait_hours", 0)
    llm = build_chain(translate_spec, not args.no_fallback, {"claude": preset["model"], "claude-code": preset["model"]}, wait)
    analysis = build_chain(spec, not args.no_fallback, {}, wait)
    if analysis.id == llm.id:
        analysis = llm
    editor = llm
    if need_editor:  # chỉ dựng chuỗi biên tập khi thật sự có bước biên tập (dich-thu / dich có polish)
        editor_spec = getattr(args, "editor_providers", None) or DEFAULT_EDITORS
        try:
            editor = build_chain(
                editor_spec, not args.no_fallback, {"claude": preset["model"], "claude-code": preset["model"]}, wait
            )
        except ValueError as e:
            # Không AI biên tập nào dùng được (vd. chưa cài Claude Code, chưa có key DeepSeek): vẫn dịch được,
            # để người dịch tự biên tập thay vì dừng hẳn.
            _err(f"Cảnh báo: {e}. Bước biên tập sẽ do người dịch ({llm.active.id}) làm.")
    return llm, analysis, editor


def _paths(args) -> tuple[Path, Path, Path]:
    """(file kết quả, file sổ tay, file cache) của lệnh hiện tại."""
    if args.cmd == "analyze":
        output = args.output or args.input.with_name(f"{args.input.stem}.bible.json")
        bible_path = output
    else:
        output = args.output or default_output_path(args.input, args.lang_code)
        bible_path = args.bible or output.with_name(f"{output.name}.bible.json")
    cache_path = (args.cache_dir or output.parent / ".bt_cache") / f"{output.name}.jsonl"
    return output, bible_path, cache_path


def _learn(args, progress) -> int:
    if not args.ban_dich.exists() or (args.ban_goc and not args.ban_goc.exists()):
        _err("Không tìm thấy file sách tham chiếu")
        return 2
    llm, _, _ = _chains(args)
    translated = load_document(args.ban_dich)
    original = load_document(args.ban_goc) if args.ban_goc else None
    name = args.ten or f"Phong cách học từ {args.ban_dich.stem}"
    profile = learn_style(
        llm, translated, original, name=name, key=args.output.stem, samples=args.so_trich_doan,
        effort=args.effort or "high", progress=progress,
    )
    profile.save(args.output)
    _err(f"Đã tạo hồ sơ phong cách: {args.output} ({len(profile.examples)} đoạn mẫu)")
    _err("Mở file để xem/sửa, rồi dùng: --phong-cach-file " + str(args.output))
    _err("Lưu ý: file chứa trích đoạn sách của bạn — chỉ dùng cá nhân.")
    _err(llm.usage_report())
    return 0


def _numbers(spec: str) -> set[int]:
    out: set[int] = set()
    for part in filter(None, (x.strip() for x in spec.split(","))):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    return out


def _apply_edits(args) -> int:
    decisions = {n: False for n in _numbers(args.bo)} | {n: True for n in _numbers(args.nhan)}
    status = {}
    if args.duyet_chuong:
        data, _ = load_edits(args.edits)
        translated = Path(data["translated"])
        doc = load_document(translated if translated.is_absolute() else args.edits.parent / translated)
        status = {sid: CHAPTER_APPROVED for sid in parse_sections(args.duyet_chuong, doc)}
    applied, failed = apply_edits_file(args.edits, decisions, args.output, status)
    _err(f"Đã xuất lại bản hiệu đính: {applied} chỗ sửa được áp" + (f", {failed} chỗ không áp được" if failed else ""))
    return 0


def _postedit(args, progress) -> int:
    for path in (args.ban_dich, args.ban_goc):
        if path and not path.exists():
            _err(f"Không tìm thấy file: {path}")
            return 2
    genre = load_genre(args.genre, args.genre_file)
    style = load_style(args.style, args.style_file)
    config = TranslatorConfig(
        target_lang=args.target,
        source_lang=args.source,
        effort=_effort(args, genre.effort),
        note=args.note,
        brief=args.brief.read_text(encoding="utf-8") if args.brief else "",
    )
    llm, _, _ = _chains(args)
    translated = load_document(args.ban_dich)
    source = load_document(args.ban_goc) if args.ban_goc else None
    output = args.output or default_output(args.ban_dich)
    if output.resolve() == args.ban_dich.resolve():
        _err("File kết quả phải khác bản dịch (bản dịch được giữ nguyên để duyệt lại)")
        return 2
    bible_path = args.bible or args.ban_dich.with_name(f"{args.ban_dich.name}.bible.json")
    bible = StoryBible.load(bible_path) if bible_path.exists() else StoryBible()
    bible.import_glossary(_load_glossary(args.glossary))
    cache = TranslationCache((args.cache_dir or output.parent / ".bt_cache") / f"{output.name}.jsonl")
    guide = args.guide.read_text(encoding="utf-8") if args.guide else ""
    editor = PostEditor(config, llm, genre, style, bible, cache, args.level, guide)
    sections = parse_sections(args.chuong, translated)
    _err(
        f"Hiệu đính: {args.ban_dich.name} · mức {LEVELS[args.level][0]} · AI: {llm.active.id} (effort {config.effort})"
        f" · {'có' if source else 'không có'} bản gốc · {len(sections) if sections else len(translated.sections)} chương"
    )
    if isinstance(progress, _ProgressPrinter):
        progress.track(output.with_name(f"{output.name}.progress.json"), args.ban_dich, {"Hiệu đính": 0})
    log_path = edits_path(output)
    previous, chapters = ([], {}) if args.lam_moi else existing_edits(log_path, args.ban_dich)
    if previous:
        _err(f"Giữ {len(previous)} chỗ sửa của các lần hiệu đính trước (thêm --lam-moi để bỏ)")
    result = postedit_document(editor, translated, source, output, sections, args.workers, progress, previous)
    done = sections if sections is not None else {s.id for s in translated.sections}
    mark_chapters(chapters, sorted(done), CHAPTER_REVIEW, {s.id: s.title for s in translated.sections})
    save_edits(log_path, args.ban_dich, args.ban_goc, output, args.level, result.edits, chapters)
    progress.finish()
    failed = sum(1 for e in result.edits if e.accepted and e.status == "failed")
    _err(f"Xong: {output} (cả cuốn)")
    _err(f"Lần này tìm được {result.new} chỗ sửa; cả cuốn đang áp {result.applied} chỗ"
         + (f", {failed} chỗ không áp được (câu cũ không khớp)" if failed else "") + f" → duyệt trong {log_path}")
    _err(llm.usage_report())
    return 0


def _learn_edits(args) -> int:
    if args.edits_json:
        changes = changes_from_edits_file(args.edits_json)
        origin = args.edits_json.name
    elif args.ban_tho and args.ban_sua:
        source = load_document(args.ban_goc) if args.ban_goc else None
        changes = changes_from_docs(load_document(args.ban_tho), load_document(args.ban_sua), source)
        origin = args.ban_sua.name
    else:
        _err("Cần --edits-json, hoặc cả --ban-tho và --ban-sua")
        return 2
    if not args.bible and not args.guide and not args.dry_run:
        _err("Cần --so-tay và/hoặc --huong-dan-hieu-dinh để ghi bài học (hoặc --chi-xem)")
        return 2
    if not changes:
        _err("Không thấy chỗ nào được sửa — không có gì để học.")
        return 0
    _err(f"Đang rút bài học từ {len(changes)} chỗ sửa…")
    llm, _, _ = _chains(args)
    bible = StoryBible.load(args.bible) if args.bible and args.bible.exists() else StoryBible(args.bible)
    lessons = learn_from_changes(llm, changes, bible, args.effort or "medium")
    report = update_bible(bible, lessons.names)
    _err(summarize(lessons, report))
    if args.dry_run:
        return 0
    if args.bible:
        bible.save(args.bible)
        _err(f"Đã cập nhật sổ tay: {args.bible}")
    if args.guide:
        added = append_guide(args.guide, lessons, origin)
        _err(f"Đã thêm {added} dòng vào {args.guide}" if added else "Hướng dẫn hiệu đính đã có đủ các bài học này.")
    _err(llm.usage_report())
    return 0


def _run(args, progress) -> int:
    if args.cmd == "learn_edits":
        return _learn_edits(args)
    if args.cmd == "learn":
        return _learn(args, progress)
    if args.cmd == "postedit":
        return _postedit(args, progress)
    if args.cmd == "apply_edits":
        return _apply_edits(args)
    if not args.input.exists():
        _err(f"Không tìm thấy file: {args.input}")
        return 2

    genre = load_genre(args.genre, args.genre_file)
    style = load_style(args.style, args.style_file)
    preset = PRESETS[args.muc]
    polish = getattr(args, "polish", None)
    config = TranslatorConfig(
        target_lang=args.target,
        source_lang=args.source,
        effort=_effort(args, genre.effort),
        note=getattr(args, "note", ""),
        brief=args.brief.read_text(encoding="utf-8") if args.brief else "",
        polish=preset["polish"] if polish is None else polish,
    )
    llm, analysis_llm, editor_llm = _chains(
        args, need_editor=args.cmd in ("preview", "translate") and config.polish, translators_only=True
    )
    output, bible_path, cache_path = _paths(args)
    doc = load_document(args.input, args.chunk_chars)
    bible = StoryBible.load(bible_path)
    bible.import_glossary(_load_glossary(args.glossary))
    cache = TranslationCache(cache_path, load=not getattr(args, "fresh", False))
    translator = Translator(config, llm, genre, style, bible, cache, analysis_llm, editor_llm)
    _err(
        f"Thể loại: {genre.name} · Phong cách: {style.name}\n"
        f"Dịch: {' -> '.join(p.id for p in llm.providers)} (effort {config.effort}) · "
        f"Biên tập: {' -> '.join(p.id for p in editor_llm.providers) if config.polish else 'không'} · "
        f"Phân tích: {' -> '.join(p.id for p in analysis_llm.providers)} · {len(doc.sections)} chương"
    )

    if args.cmd == "preview":
        preview_path = output.with_name(f"{output.name}.preview.md")
        preview_path.write_text(preview_document(translator, doc, args.section, args.chunks), encoding="utf-8")
        bible.save()
        _err(f"Bản dịch thử: {preview_path}")
        _err(f"Sổ tay: {bible_path} — các đoạn đã dịch thử được dùng lại khi chạy `dich` cùng tùy chọn.")
        _err(translator.usage_report())
        return 0

    if isinstance(progress, _ProgressPrinter):
        totals = {"Phân tích": len(doc.sections)}
        if args.cmd == "translate":
            totals["Dịch"] = sum(len(s.chunks) for s in doc.sections)
        progress.track(output.with_name(f"{output.name}.progress.json"), args.input, totals)

    if args.cmd == "analyze" or not args.no_analyze:
        analyze_document(translator, doc, args.workers, progress)
        bible.save()
        _err(f"Sổ tay truyện: {bible_path} ({_bible_summary(bible)})")
    if args.cmd == "analyze":
        progress.finish()
        _err("Hãy xem và sửa sổ tay nếu cần, rồi chạy `dich --so-tay` với file này.")
        _err(translator.usage_report())
        return 0

    if args.no_analyze and args.workers > 1:
        _err("Lưu ý: không phân tích trước mà dịch song song thì tên mới có thể lệch giữa các chương; nên dùng --workers 1.")
    result = translate_document(
        translator, doc, output, workers=args.workers, lang_code=args.lang_code, fix=not args.no_fix, progress=progress
    )
    bible.save()
    progress.finish()
    report_path = output.with_name(f"{output.name}.consistency.md")
    report_path.write_text(result.report, encoding="utf-8")
    untranslated = sum(1 for i in result.issues if i.problem == "untranslated")
    _err(f"Xong: {output}")
    _err(f"Sổ tay: {bible_path} ({_bible_summary(bible)})")
    _err(
        f"Nhất quán: đã sửa {result.fixed_chunks} đoạn, còn {untranslated} lỗi sót tên, "
        f"{len(result.issues) - untranslated} chỗ cần xem lại → {report_path}"
    )
    _err(translator.usage_report())
    return 0


def main(argv: list[str] | None = None) -> int:
    # A Windows console or embedded Python may expose ASCII-only streams. Keep
    # provider fallback warnings from aborting a translation on Unicode text.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if args.cmd == "ui":
        from .webui import serve

        serve(args.workspace, args.port, not args.no_browser)
        return 0
    if args.cmd == "genres":
        _print_catalog(GENRES.values())
        return 0
    if args.cmd == "styles":
        _print_catalog(STYLES.values())
        return 0
    try:
        return _run(args, _ProgressPrinter())
    except (KeyError, ValueError, OSError) as e:
        _err(f"Lỗi: {e}")
        return 2
    except ProviderUnavailable as e:
        _err(f"Không còn nhà cung cấp API nào dùng được: {e}")
        _err("Tiến độ đã được lưu. Kiểm tra hạn mức Codex hoặc API key DeepSeek rồi chạy lại với --providers codex,deepseek.")
        return 1
    except TranslationError as e:
        _err(f"Lỗi dịch: {e}")
        _err("Tiến độ đã được lưu; chạy lại cùng lệnh để tiếp tục.")
        return 1
    except anthropic.APIStatusError as e:
        _err(f"Lỗi API ({e.status_code}): {e.message}")
        _err("Tiến độ đã được lưu; chạy lại cùng lệnh để tiếp tục.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
