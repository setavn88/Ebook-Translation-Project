"""Giao diện dòng lệnh.

Lệnh (tên tiếng Việt hoặc tiếng Anh đều được):
  the-loai | genres          liệt kê thể loại
  phong-cach | styles        liệt kê phong cách dịch
  hoc-phong-cach | learn-style  học phong cách từ sách tham chiếu
  phan-tich | analyze        lập sổ tay truyện
  dich-thu | preview         dịch thử vài đoạn
  dich | translate           dịch cả cuốn
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from pathlib import Path

import anthropic

from . import __version__
from .bible import StoryBible
from .cache import TranslationCache
from .chunker import DEFAULT_CHUNK_CHARS
from .document import SUPPORTED, default_output_path, load_document
from .genres import BUILTIN as GENRES, load_genre
from .pipeline import analyze_document, preview_document, translate_document
from .providers import ProviderUnavailable, build_chain, parse_spec
from .styles import BUILTIN as STYLES, load_style
from .stylelearn import learn_style
from .translator import TranslationError, Translator, TranslatorConfig

_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# Mức chất lượng / chi phí. Chỉ áp dụng cho Claude khi người dùng không tự chọn model.
PRESETS = {
    "tiet-kiem": {"model": "claude-sonnet-5-5", "analysis_model": "claude-haiku-5-5", "polish": False, "max_effort": "medium"},
    "can-bang": {"model": "claude-opus-5-5", "analysis_model": "claude-sonnet-5-5", "polish": True, "max_effort": None},
    "cao-cap": {"model": "claude-opus-5-5", "analysis_model": "claude-opus-5-5", "polish": True, "min_effort": "high"},
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
        "--providers", default="claude",
        help="Nhà cung cấp API theo thứ tự ưu tiên, dạng tên[:model], cách nhau dấu phẩy. "
             "Vd: claude-code (dùng gói Claude Pro/Max, không tốn tiền API), claude,deepseek, "
             "deepseek:deepseek-chat,openai:gpt-5. Hết quota/lỗi thì tự chuyển sang cái kế tiếp.",
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
    p.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS, help="Số ký tự tối đa mỗi đoạn gửi đi")
    p.add_argument("--workers", type=int, default=4, help="Số chương xử lý song song (mặc định: 4)")
    p.add_argument("--cache-dir", type=Path, help="Thư mục lưu tiến độ (mặc định: .bt_cache cạnh file kết quả)")
    _add_api_opts(p)


def _add_translate_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("-o", "--output", type=Path, help="File kết quả (mặc định: <tên>.<mã ngôn ngữ>.<đuôi>)")
    p.add_argument("--lang-code", default="vi", help="Mã ngôn ngữ đích ghi vào EPUB và tên file (mặc định: vi)")
    p.add_argument("--yeu-cau", "--note", dest="note", default="", help="Yêu cầu riêng cho bản dịch, ưu tiên cao nhất")
    p.add_argument("--so-tay", "--bible", dest="bible", type=Path, help="File sổ tay truyện (mặc định: <file kết quả>.bible.json)")
    p.add_argument("--polish", dest="polish", action="store_true", default=None, help="Bật bước biên tập (mặc định theo --muc)")
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
    return parser


class _ProgressPrinter:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __call__(self, step: str, name, done: int, total: int | None = None) -> None:
        if total is None:  # (bước, đã xong, tổng)
            name, done, total = "", name, done
        with self._lock:
            label = f"{name}: " if name else ""
            print(f"  [{step}] {label}{done}/{total}", file=sys.stderr, flush=True)


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


def _chains(args):
    """(chuỗi dịch, chuỗi phân tích). Mức --muc chỉ chọn model Claude khi người dùng không tự chọn."""
    preset = PRESETS[getattr(args, "muc", "can-bang")]
    spec = parse_spec(args.providers)
    translate_spec = list(spec)
    if args.model:
        translate_spec[0] = (spec[0][0], args.model)  # --model chỉ áp cho bước dịch
    wait = getattr(args, "wait_hours", 0)
    llm = build_chain(translate_spec, not args.no_fallback, {"claude": preset["model"], "claude-code": preset["model"]}, wait)
    analysis = build_chain(
        spec, not args.no_fallback, {"claude": preset["analysis_model"], "claude-code": preset["analysis_model"]}, wait
    )
    if analysis.id == llm.id:
        analysis = llm
    return llm, analysis


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
    llm, _ = _chains(args)
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


def _run(args, progress) -> int:
    if args.cmd == "learn":
        return _learn(args, progress)
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
    llm, analysis_llm = _chains(args)
    output, bible_path, cache_path = _paths(args)
    doc = load_document(args.input, args.chunk_chars)
    bible = StoryBible.load(bible_path)
    bible.import_glossary(_load_glossary(args.glossary))
    cache = TranslationCache(cache_path, load=not getattr(args, "fresh", False))
    translator = Translator(config, llm, genre, style, bible, cache, analysis_llm)
    _err(
        f"Thể loại: {genre.name} · Phong cách: {style.name}\n"
        f"Dịch: {llm.active.id} (effort {config.effort}, biên tập: {'có' if config.polish else 'không'}) · "
        f"Phân tích: {analysis_llm.active.id} · {len(doc.sections)} chương"
    )

    if args.cmd == "preview":
        preview_path = output.with_name(f"{output.name}.preview.md")
        preview_path.write_text(preview_document(translator, doc, args.section, args.chunks), encoding="utf-8")
        bible.save()
        _err(f"Bản dịch thử: {preview_path}")
        _err(f"Sổ tay: {bible_path} — các đoạn đã dịch thử được dùng lại khi chạy `dich` cùng tùy chọn.")
        _err(translator.usage_report())
        return 0

    if args.cmd == "analyze" or not args.no_analyze:
        analyze_document(translator, doc, args.workers, progress)
        bible.save()
        _err(f"Sổ tay truyện: {bible_path} ({_bible_summary(bible)})")
    if args.cmd == "analyze":
        _err("Hãy xem và sửa sổ tay nếu cần, rồi chạy `dich --so-tay` với file này.")
        _err(translator.usage_report())
        return 0

    if args.no_analyze and args.workers > 1:
        _err("Lưu ý: không phân tích trước mà dịch song song thì tên mới có thể lệch giữa các chương; nên dùng --workers 1.")
    result = translate_document(
        translator, doc, output, workers=args.workers, lang_code=args.lang_code, fix=not args.no_fix, progress=progress
    )
    bible.save()
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
        _err("Tiến độ đã được lưu. Nạp thêm credit, đặt API key khác, hoặc thêm --providers claude,deepseek,openai rồi chạy lại.")
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
