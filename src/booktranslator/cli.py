"""Giao diện dòng lệnh: `booktranslator genres | analyze | translate`."""

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
from .genres import BUILTIN, load_genre
from .pipeline import analyze_document, translate_document
from .translator import DEFAULT_MODEL, TranslationError, Translator, TranslatorConfig


def _load_glossary(path: Path | None) -> dict[str, str]:
    if not path:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: bảng thuật ngữ phải là JSON object dạng {{\"nguồn\": \"đích\"}}")
    return {str(k): str(v) for k, v in data.items()}


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("input", type=Path, help=f"File sách ({', '.join(SUPPORTED)})")
    p.add_argument(
        "-g", "--genre", default="general", choices=list(BUILTIN),
        help="Chế độ dịch theo thể loại (xem `booktranslator genres`)",
    )
    p.add_argument("--genre-file", type=Path, help="File JSON định nghĩa thể loại riêng (ghi đè --genre)")
    p.add_argument("--to", dest="target", default="Tiếng Việt", help="Ngôn ngữ đích (mặc định: Tiếng Việt)")
    p.add_argument("--from", dest="source", default="tự nhận diện", help="Ngôn ngữ nguồn (mặc định: tự nhận diện)")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Model Claude (mặc định: {DEFAULT_MODEL})")
    p.add_argument(
        "--effort", choices=["low", "medium", "high", "xhigh", "max"],
        help="Mức suy nghĩ của mô hình; mặc định theo thể loại",
    )
    p.add_argument("--glossary", type=Path, help="File JSON {\"nguồn\": \"đích\"} nạp thêm vào sổ tay")
    p.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS, help="Số ký tự tối đa mỗi đoạn gửi đi")
    p.add_argument("--workers", type=int, default=4, help="Số chương xử lý song song (mặc định: 4)")
    p.add_argument("--cache-dir", type=Path, help="Thư mục lưu tiến độ (mặc định: .bt_cache cạnh file kết quả)")
    p.add_argument(
        "--no-fallback", action="store_true",
        help="Tắt fallback phía server khi mô hình từ chối (cần khi dùng Bedrock/Vertex/Foundry)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="booktranslator", description="Dịch sách bằng Claude theo thể loại.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("genres", help="Liệt kê các chế độ dịch theo thể loại")

    a = sub.add_parser("analyze", help="Đọc sách, lập sổ tay truyện (nhân vật, tên riêng, xưng hô, sự kiện) để duyệt trước")
    _add_common(a)
    a.add_argument("-o", "--output", type=Path, help="File sổ tay JSON (mặc định: <tên sách>.bible.json)")

    t = sub.add_parser("translate", help="Dịch một cuốn sách")
    _add_common(t)
    t.add_argument("-o", "--output", type=Path, help="File kết quả (mặc định: <tên>.<mã ngôn ngữ>.<đuôi>)")
    t.add_argument("--lang-code", default="vi", help="Mã ngôn ngữ đích ghi vào EPUB và tên file (mặc định: vi)")
    t.add_argument("--style", default="", help="Yêu cầu văn phong riêng, ưu tiên hơn quy tắc thể loại")
    t.add_argument("--bible", type=Path, help="File sổ tay truyện (mặc định: <file kết quả>.bible.json)")
    t.add_argument("--no-analyze", action="store_true", help="Bỏ bước đọc trước cả sách (rẻ hơn, kém nhất quán hơn)")
    t.add_argument("--no-fix", action="store_true", help="Chỉ báo cáo lỗi nhất quán, không tự dịch lại")
    t.add_argument("--fresh", action="store_true", help="Bỏ qua bản dịch đã lưu, dịch lại từ đầu")
    return parser


class _ProgressPrinter:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __call__(self, step: str, name: str, done: int, total: int) -> None:
        with self._lock:
            print(f"  [{step}] {name}: {done}/{total}", file=sys.stderr, flush=True)


def _print_usage(translator: Translator) -> None:
    u = translator.usage
    print(
        f"API: {u.requests} request, input {u.input_tokens:,} token "
        f"(cache đọc {u.cache_read_input_tokens:,}, ghi {u.cache_creation_input_tokens:,}), "
        f"output {u.output_tokens:,} token",
        file=sys.stderr,
    )


def _print_genres() -> None:
    for g in BUILTIN.values():
        print(f"{g.key:<9} {g.name}")
        print(f"          {g.description} (effort mặc định: {g.effort})")


def _bible_summary(bible: StoryBible) -> str:
    return (
        f"{len(bible.entities)} tên riêng/thuật ngữ, {len(bible.addressing)} cặp xưng hô, "
        f"{len(bible.sections)} chương đã tóm tắt"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    if args.command == "genres":
        _print_genres()
        return 0
    if not args.input.exists():
        print(f"Không tìm thấy file: {args.input}", file=sys.stderr)
        return 2

    genre = load_genre(args.genre, args.genre_file)
    config = TranslatorConfig(
        target_lang=args.target,
        source_lang=args.source,
        model=args.model,
        effort=args.effort or genre.effort,
        use_fallback=not args.no_fallback,
    )
    progress = _ProgressPrinter()

    if args.command == "analyze":
        output = args.output or args.input.with_name(f"{args.input.stem}.bible.json")
        cache_path = (args.cache_dir or output.parent / ".bt_cache") / f"{output.name}.jsonl"
    else:
        output = args.output or default_output_path(args.input, args.lang_code)
        cache_path = (args.cache_dir or output.parent / ".bt_cache") / f"{output.name}.jsonl"
        config.style = args.style

    try:
        doc = load_document(args.input, args.chunk_chars)
        bible_path = output if args.command == "analyze" else (args.bible or output.with_name(f"{output.name}.bible.json"))
        bible = StoryBible.load(bible_path)
        bible.import_glossary(_load_glossary(args.glossary))
        cache = TranslationCache(cache_path, load=not getattr(args, "fresh", False))
        translator = Translator(config, genre, bible, cache=cache)
        print(f"Thể loại: {genre.name} · effort: {config.effort} · {len(doc.sections)} chương", file=sys.stderr)

        if args.command == "analyze" or not args.no_analyze:
            analyze_document(translator, doc, args.workers, progress)
            bible.save()
            print(f"Sổ tay truyện: {bible_path} ({_bible_summary(bible)})", file=sys.stderr)
        if args.command == "analyze":
            print("Hãy xem và sửa sổ tay nếu cần, rồi chạy `translate --bible` với file này.", file=sys.stderr)
            _print_usage(translator)
            return 0

        if args.no_analyze and args.workers > 1:
            print("Lưu ý: không phân tích trước mà dịch song song thì tên mới có thể lệch giữa các chương; "
                  "nên dùng --workers 1.", file=sys.stderr)
        result = translate_document(
            translator, doc, output, workers=args.workers, lang_code=args.lang_code, fix=not args.no_fix, progress=progress
        )
        bible.save()
        report_path = output.with_name(f"{output.name}.consistency.md")
        report_path.write_text(result.report, encoding="utf-8")
        untranslated = sum(1 for i in result.issues if i.problem == "untranslated")
        print(f"Xong: {output}", file=sys.stderr)
        print(f"Sổ tay: {bible_path} ({_bible_summary(bible)})", file=sys.stderr)
        print(
            f"Nhất quán: đã sửa {result.fixed_chunks} đoạn, còn {untranslated} lỗi sót tên, "
            f"{len(result.issues) - untranslated} chỗ cần xem lại → {report_path}",
            file=sys.stderr,
        )
        _print_usage(translator)
        return 0
    except (KeyError, ValueError) as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 2
    except TranslationError as e:
        print(f"Lỗi dịch: {e}", file=sys.stderr)
        print("Tiến độ đã được lưu; chạy lại cùng lệnh để tiếp tục.", file=sys.stderr)
        return 1
    except anthropic.AuthenticationError:
        print("Sai hoặc thiếu API key. Đặt biến môi trường ANTHROPIC_API_KEY.", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as e:
        print(f"Lỗi API ({e.status_code}): {e.message}", file=sys.stderr)
        print("Tiến độ đã được lưu; chạy lại cùng lệnh để tiếp tục.", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError:
        print("Lỗi kết nối mạng tới API. Tiến độ đã được lưu; chạy lại để tiếp tục.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
