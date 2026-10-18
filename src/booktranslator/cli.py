"""Giao diện dòng lệnh: `booktranslator translate ...` và `booktranslator glossary ...`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from pathlib import Path

import anthropic

from . import __version__
from .cache import TranslationCache
from .chunker import DEFAULT_CHUNK_CHARS
from .formats import SUPPORTED, default_output_path, read_sample, translate_file
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
    p.add_argument("--to", dest="target", default="Tiếng Việt", help="Ngôn ngữ đích (mặc định: Tiếng Việt)")
    p.add_argument("--from", dest="source", default="tự nhận diện", help="Ngôn ngữ nguồn (mặc định: tự nhận diện)")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Model Claude (mặc định: {DEFAULT_MODEL})")
    p.add_argument(
        "--effort",
        default="medium",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="Mức suy nghĩ của mô hình; cao hơn thì dịch kỹ hơn nhưng tốn hơn (mặc định: medium)",
    )
    p.add_argument(
        "--no-fallback",
        action="store_true",
        help="Tắt fallback phía server khi mô hình từ chối (cần khi dùng Bedrock/Vertex/Foundry)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="booktranslator", description="Dịch sách bằng Claude.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    t = sub.add_parser("translate", help="Dịch một cuốn sách")
    _add_common(t)
    t.add_argument("-o", "--output", type=Path, help="File kết quả (mặc định: <tên>.<mã ngôn ngữ>.<đuôi>)")
    t.add_argument("--lang-code", default="vi", help="Mã ngôn ngữ đích ghi vào EPUB và tên file (mặc định: vi)")
    t.add_argument("--style", default="", help="Yêu cầu văn phong, ví dụ: 'xưng hô ta - ngươi, giọng cổ trang'")
    t.add_argument("--glossary", type=Path, help="File JSON bảng thuật ngữ {\"nguồn\": \"đích\"}")
    t.add_argument("--auto-glossary", action="store_true", help="Tự tạo bảng thuật ngữ trước khi dịch")
    t.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS, help="Số ký tự tối đa mỗi đoạn gửi đi")
    t.add_argument("--workers", type=int, default=4, help="Số chương EPUB dịch song song (mặc định: 4)")
    t.add_argument("--cache-dir", type=Path, help="Thư mục lưu tiến độ (mặc định: .bt_cache cạnh file kết quả)")

    g = sub.add_parser("glossary", help="Tạo bảng thuật ngữ từ sách để chỉnh tay trước khi dịch")
    _add_common(g)
    g.add_argument("-o", "--output", type=Path, required=True, help="File JSON kết quả")
    g.add_argument("--max-terms", type=int, default=80)
    return parser


class _ProgressPrinter:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __call__(self, name: str, done: int, total: int) -> None:
        with self._lock:
            print(f"  [{name}] {done}/{total} đoạn", file=sys.stderr, flush=True)


def _print_usage(translator: Translator) -> None:
    u = translator.usage
    print(
        f"API: {u.requests} request, input {u.input_tokens:,} token "
        f"(cache đọc {u.cache_read_input_tokens:,}, ghi {u.cache_creation_input_tokens:,}), "
        f"output {u.output_tokens:,} token",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    if not args.input.exists():
        print(f"Không tìm thấy file: {args.input}", file=sys.stderr)
        return 2

    config = TranslatorConfig(
        target_lang=args.target,
        source_lang=args.source,
        model=args.model,
        effort=args.effort,
        use_fallback=not args.no_fallback,
    )

    try:
        if args.command == "glossary":
            translator = Translator(config)
            glossary = translator.build_glossary(read_sample(args.input), args.max_terms)
            args.output.write_text(json.dumps(glossary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Đã ghi {len(glossary)} thuật ngữ vào {args.output}", file=sys.stderr)
            _print_usage(translator)
            return 0

        output = args.output or default_output_path(args.input, args.lang_code)
        cache_dir = args.cache_dir or output.parent / ".bt_cache"
        cache = TranslationCache(cache_dir / f"{output.name}.jsonl")
        config.style = args.style
        config.glossary = _load_glossary(args.glossary)
        translator = Translator(config, cache=cache)

        if args.auto_glossary:
            print("Đang tạo bảng thuật ngữ...", file=sys.stderr)
            auto = translator.build_glossary(read_sample(args.input))
            config.glossary = {**auto, **config.glossary}  # thuật ngữ người dùng ưu tiên hơn
            glossary_path = output.with_name(output.stem + ".glossary.json")
            glossary_path.write_text(json.dumps(config.glossary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Bảng thuật ngữ ({len(config.glossary)} mục): {glossary_path}", file=sys.stderr)

        if len(cache):
            print(f"Tiếp tục từ {len(cache)} đoạn đã dịch trước đó", file=sys.stderr)
        translate_file(
            args.input,
            output,
            translator,
            chunk_chars=args.chunk_chars,
            workers=args.workers,
            lang_code=args.lang_code,
            progress=_ProgressPrinter(),
        )
        print(f"Xong: {output}", file=sys.stderr)
        _print_usage(translator)
        return 0
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
