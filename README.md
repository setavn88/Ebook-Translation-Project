# Ebook Translation Project

Công cụ dòng lệnh dịch sách bằng AI (Claude của Anthropic). Hỗ trợ **TXT**, **Markdown** và **EPUB**, giữ nguyên định dạng, thống nhất tên riêng/thuật ngữ trong suốt cuốn sách, và lưu tiến độ để dừng giữa chừng rồi chạy tiếp mà không mất tiền dịch lại.

## Tính năng

- **Giữ định dạng**: EPUB được dịch trong từng file XHTML, giữ nguyên thẻ, liên kết, ảnh, CSS, mục lục; cập nhật `dc:language` và `lang`. Markdown giữ nguyên cú pháp.
- **Mạch văn liền lạc**: sách được chia thành các đoạn (mặc định ~6000 ký tự, ngắt ở ranh giới đoạn văn), mỗi đoạn gửi kèm phần cuối của đoạn trước và bản dịch của nó để giữ xưng hô và giọng văn.
- **Bảng thuật ngữ**: tự trích tên nhân vật, địa danh, thuật ngữ (`--auto-glossary` hoặc lệnh `glossary`), có thể sửa tay trong file JSON rồi dùng lại.
- **Chạy tiếp khi bị gián đoạn**: mỗi đoạn dịch xong được ghi vào `.bt_cache/`. Chạy lại cùng lệnh thì các đoạn đã dịch được bỏ qua.
- **Tiết kiệm chi phí**: prompt hệ thống (hướng dẫn + bảng thuật ngữ) được prompt-cache giữa các đoạn; các chương EPUB dịch song song.
- **Xử lý lỗi**: bản dịch bị cắt (`max_tokens`) thì tự chia nhỏ đoạn rồi dịch lại; SDK tự retry khi gặp lỗi 429/5xx; khi mô hình từ chối, fallback phía server (`fallbacks: "default"`) tự chuyển sang mô hình dự phòng.

## Cài đặt

Yêu cầu Python ≥ 3.10.

```bash
git clone https://github.com/setavn88/Ebook-Translation-Project.git
cd Ebook-Translation-Project
pip install -e .
export ANTHROPIC_API_KEY="sk-ant-..."   # lấy tại https://platform.claude.com
```

## Sử dụng

```bash
# Dịch EPUB sang tiếng Việt, tự tạo bảng thuật ngữ trước → book.vi.epub
booktranslator translate book.epub --auto-glossary

# Chỉ định ngôn ngữ, văn phong và file kết quả
booktranslator translate novel.md --from "Tiếng Trung" --to "Tiếng Việt" \
    --style "Truyện kiếm hiệp, xưng hô ta - ngươi, giữ từ Hán Việt" -o novel.vi.md

# Quy trình kỹ: tạo bảng thuật ngữ → sửa tay → dịch với bảng đó
booktranslator glossary book.epub -o glossary.json
booktranslator translate book.epub --glossary glossary.json

# Dịch sang tiếng Anh
booktranslator translate sach.txt --to English --lang-code en
```

Bảng thuật ngữ là JSON đơn giản:

```json
{ "Harry Potter": "Harry Potter", "Hogwarts": "Trường Hogwarts", "Muggle": "Muggle" }
```

### Tùy chọn chính

| Tùy chọn | Mặc định | Ý nghĩa |
|---|---|---|
| `--to` / `--from` | `Tiếng Việt` / tự nhận diện | Ngôn ngữ đích / nguồn |
| `--lang-code` | `vi` | Mã ngôn ngữ ghi vào EPUB và tên file kết quả |
| `--style` | | Yêu cầu văn phong, xưng hô |
| `--glossary FILE` / `--auto-glossary` | | Dùng bảng thuật ngữ có sẵn / tự tạo (thuật ngữ do bạn viết được ưu tiên) |
| `--model` | `claude-opus-5-5` | Model Claude |
| `--effort` | `medium` | `low` → `max`: cao hơn thì dịch kỹ hơn nhưng tốn token hơn |
| `--chunk-chars` | `6000` | Kích thước mỗi đoạn gửi đi |
| `--workers` | `4` | Số chương EPUB dịch song song |
| `--cache-dir` | `.bt_cache` cạnh file kết quả | Nơi lưu tiến độ |
| `--no-fallback` | | Tắt fallback phía server (cần khi dùng qua Bedrock/Vertex/Foundry) |

Khi chạy xong, công cụ in số request và số token đã dùng. Nên dịch thử một chương trước để ước lượng chi phí cả cuốn.

## Chạy bằng Docker

```bash
docker build -t booktranslator .
docker run --rm -e ANTHROPIC_API_KEY -v "$PWD:/books" booktranslator translate /books/book.epub --auto-glossary
```

## Phát triển

```bash
pip install -e '.[dev]'
pytest          # test dùng client giả lập, không cần API key
```

Cấu trúc mã nguồn:

```
src/booktranslator/
  cli.py         # lệnh translate / glossary
  formats.py     # đọc/ghi TXT, Markdown, EPUB
  chunker.py     # chia văn bản/HTML theo ranh giới đoạn
  translator.py  # prompt, gọi Claude API, xử lý max_tokens/refusal, bảng thuật ngữ
  cache.py       # lưu tiến độ dạng JSON lines
```

## Giới hạn hiện tại

- Chưa hỗ trợ PDF/DOCX. Có thể chuyển sang EPUB bằng [Calibre](https://calibre-ebook.com/) (`ebook-convert book.pdf book.epub`) rồi dịch.
- Tiêu đề trong `<head>` và mục lục `toc.ncx`/`nav` hiện chưa được dịch.
