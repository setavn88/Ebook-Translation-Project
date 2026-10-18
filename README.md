# Ebook Translation Project

Công cụ dòng lệnh dịch sách bằng AI (Claude của Anthropic), có **chế độ dịch theo thể loại** (Sci-fi, Fantasy, Romance, Tiên hiệp, Trinh thám...) và **sổ tay truyện** để tên nhân vật, tên riêng, xưng hô và diễn biến nhất quán từ trang đầu đến trang cuối. Hỗ trợ **TXT**, **Markdown** và **EPUB**, giữ nguyên định dạng, lưu tiến độ để chạy tiếp khi bị gián đoạn.

## Chế độ dịch theo thể loại

```bash
booktranslator genres
```

| `--genre` | Thể loại | Cách dịch | Theo dõi trong sổ tay | Effort mặc định |
|---|---|---|---|---|
| `general` | Tổng quát | Rõ ràng, chính xác | nhân vật, địa danh, tổ chức, thuật ngữ | medium |
| `scifi` | Khoa học viễn tưởng | Sắc, mạch lạc; thuật ngữ khoa học chuẩn (wormhole → lỗ sâu); thuật ngữ tác giả tự đặt được dịch nhất quán; giữ số liệu, đơn vị | nhân vật, AI, tàu, hành tinh, công nghệ, chủng loài, đơn vị | medium |
| `fantasy` | Giả tưởng / Kỳ ảo | Giàu hình ảnh, hơi cổ điển; xưng hô theo địa vị; danh hiệu dịch nghĩa (Kingslayer → Kẻ Giết Vua); tên bịa giữ nguyên | nhân vật, chủng tộc, sinh vật, vương quốc, phe phái, phép thuật, cổ vật, danh hiệu | medium |
| `romance` | Lãng mạn / Ngôn tình | Mềm mại, giàu cảm xúc; **xưng hô của từng cặp đổi theo giai đoạn quan hệ**; tên gọi yêu thống nhất | nhân vật, biệt danh, quan hệ, xưng hô, các mốc tình cảm | medium |
| `wuxia` | Tiên hiệp / Kiếm hiệp / Huyền huyễn | Cổ phong; **Hán Việt** cho tên người, môn phái, công pháp, cảnh giới; xưng hô cổ trang (tại hạ, sư phụ...) | nhân vật, môn phái, công pháp, cảnh giới (đúng thứ tự), pháp bảo, ngoại hiệu | high |
| `mystery` | Trinh thám / Ly kỳ | Chính xác tuyệt đối chi tiết (manh mối); giữ sự mơ hồ có chủ đích; không tiết lộ trước | nhân vật, bí danh, vật chứng, dòng thời gian, chứng cứ ngoại phạm | high |
| `horror` | Kinh dị | Ưu tiên không khí, nhịp; giữ mức độ ghê rợn | nhân vật, thực thể siêu nhiên, nghi lễ, đồ vật bị nguyền | medium |
| `literary` | Văn học / Kinh điển | Trung thành tối đa văn phong tác giả; tước hiệu quý tộc thống nhất | nhân vật, tước hiệu, quan hệ, motif | high |

Mỗi thể loại quyết định 4 thứ: **văn phong**, **quy tắc đặt/giữ tên riêng**, **những gì cần theo dõi khi đọc trước sách**, và **effort mặc định** (effort cao hơn thì dịch kỹ hơn, tốn token hơn; ghi đè bằng `--effort`).

### Tự định nghĩa thể loại

Tạo file JSON kế thừa một thể loại có sẵn rồi ghi đè các trường cần đổi. Xem ví dụ [`examples/genre-cyberpunk.json`](examples/genre-cyberpunk.json):

```bash
booktranslator translate book.epub --genre-file examples/genre-cyberpunk.json
```

Các trường: `base`, `key`, `name`, `description`, `style`, `naming`, `entity_types`, `analysis_focus`, `effort`, `track_addressing`.

## Cách giữ nhất quán: quy trình 3 bước

```
 ① Phân tích                ② Dịch                          ③ Kiểm tra & sửa
 đọc trước từng chương  →   mỗi đoạn được gửi kèm:      →   đối chiếu từng đoạn với sổ tay;
 lập SỔ TAY TRUYỆN:         • sổ tay (tên + xưng hô)         đoạn nào còn sót tên gốc thì
 • tên riêng → bản dịch     • tóm tắt + sự kiện các          tự dịch lại với lời nhắc;
 • tên khác (alias)           chương trước                   ghi báo cáo .consistency.md
 • xưng hô từng cặp         • tóm tắt chương hiện tại
 • tóm tắt, sự kiện         • tên có trong đoạn này
   then chốt mỗi chương     • đuôi đoạn trước + bản dịch
                            tên mới gặp → bổ sung sổ tay
```

- **Một tên chỉ có một bản dịch.** Khi một tên đã có trong sổ tay, bản dịch của nó không bao giờ bị thay tự động. Thông tin mới chỉ được bổ sung (tên khác, ghi chú). Muốn đổi tên thì sửa file sổ tay.
- **Kết quả không phụ thuộc thứ tự chạy.** Các chương được phân tích song song, nhưng khi gộp thì chương đứng trước trong sách luôn thắng.
- **Diễn biến khớp nhau.** Mô hình biết các sự kiện đã xảy ra (ai đã chết, bí mật nào đã lộ, quan hệ đang ở giai đoạn nào), nên không dịch sai đại từ hay xưng hô.

## Cài đặt

Yêu cầu Python ≥ 3.10.

```bash
git clone https://github.com/setavn88/Ebook-Translation-Project.git
cd Ebook-Translation-Project
pip install -e .
export ANTHROPIC_API_KEY="sk-ant-..."   # lấy tại https://platform.claude.com
```

## Sử dụng

**Cách nhanh**: dịch luôn, sổ tay được lập tự động:

```bash
booktranslator translate book.epub --genre fantasy
# → book.vi.epub, book.vi.epub.bible.json (sổ tay), book.vi.epub.consistency.md (báo cáo)
```

**Cách kỹ (khuyên dùng cho sách dài)**: lập sổ tay → duyệt/sửa tên → dịch:

```bash
booktranslator analyze book.epub --genre wuxia -o book.bible.json
#   mở book.bible.json, sửa tên/xưng hô cho đúng ý
booktranslator translate book.epub --genre wuxia --bible book.bible.json
```

Ví dụ khác:

```bash
booktranslator translate novel.md -g romance --style "Nữ chính 17 tuổi, xưng tớ - cậu với bạn bè"
booktranslator translate sach.txt -g scifi --to English --lang-code en
booktranslator translate book.epub -g mystery --glossary names.json   # nạp thêm tên có sẵn {"nguồn": "đích"}
```

### Sổ tay truyện (`*.bible.json`)

```json
{
  "entities": [
    {"source": "Aragorn", "target": "Aragorn", "type": "character", "aliases": ["Strider"], "gender": "nam", "note": "người thừa kế ngai vàng"},
    {"source": "Strider", "target": "Sải Chân Dài", "type": "title", "aliases": [], "gender": "", "note": ""}
  ],
  "addressing": [
    {"speaker": "Frodo", "listener": "Gandalf", "self_term": "cháu", "other_term": "ông", "context": ""}
  ],
  "sections": {
    "OEBPS/ch01.xhtml": {"title": "Chương 1", "summary": "...", "events": ["Bilbo biến mất trong bữa tiệc"]}
  }
}
```

Có thể sửa trực tiếp các mục: `target` (bản dịch), `aliases` (tên gọi khác trong bản gốc), `addressing` (xưng hô), `summary`/`events`. Sau khi sửa sổ tay, chạy lại với `--fresh` để dịch lại các đoạn đã dịch theo tên mới.

### Tùy chọn

| Tùy chọn | Mặc định | Ý nghĩa |
|---|---|---|
| `-g, --genre` / `--genre-file` | `general` | Chế độ dịch theo thể loại |
| `--to` / `--from` | `Tiếng Việt` / tự nhận diện | Ngôn ngữ đích / nguồn |
| `--style` | | Yêu cầu riêng, ưu tiên hơn quy tắc thể loại |
| `--bible FILE` | `<kết quả>.bible.json` | File sổ tay truyện |
| `--glossary FILE` | | Nạp thêm tên `{"nguồn": "đích"}` vào sổ tay |
| `--no-analyze` | | Bỏ bước đọc trước (rẻ hơn ~ chi phí input 1 lần đọc sách, nhưng kém nhất quán; nên kèm `--workers 1`) |
| `--no-fix` | | Chỉ báo cáo lỗi nhất quán, không tự dịch lại |
| `--fresh` | | Bỏ qua bản dịch đã lưu |
| `--effort` | theo thể loại | `low` → `max` |
| `--model` | `claude-opus-5-5` | Model Claude |
| `--lang-code` | `vi` | Mã ngôn ngữ ghi vào EPUB và tên file |
| `--chunk-chars` | `6000` | Kích thước mỗi đoạn gửi đi |
| `--workers` | `4` | Số chương xử lý song song |
| `--cache-dir` | `.bt_cache` cạnh file kết quả | Nơi lưu tiến độ |
| `--no-fallback` | | Tắt fallback phía server (cần khi dùng Bedrock/Vertex/Foundry) |

Chạy xong, công cụ in số request và token đã dùng. Nên dịch thử một chương trước để ước lượng chi phí cả cuốn. Bước phân tích tốn thêm khoảng một lần đọc toàn bộ sách (chủ yếu là token input).

## Chạy bằng Docker

```bash
docker build -t booktranslator .
docker run --rm -e ANTHROPIC_API_KEY -v "$PWD:/books" booktranslator translate /books/book.epub -g fantasy
```

## Phát triển

```bash
pip install -e '.[dev]'
pytest          # test dùng client giả lập, không cần API key
```

```
src/booktranslator/
  genres.py       # 8 chế độ dịch theo thể loại + nạp thể loại tự định nghĩa
  bible.py        # sổ tay truyện: tên riêng, alias, xưng hô, tóm tắt/sự kiện từng chương
  document.py     # đọc TXT/Markdown/EPUB thành chương → đoạn; ghi lại đúng định dạng
  translator.py   # prompt theo thể loại, phân tích chương (structured output), dịch đoạn
  consistency.py  # kiểm tra tên gốc còn sót / bản dịch tên bị thiếu
  pipeline.py     # điều phối: phân tích → dịch → kiểm tra & sửa
  chunker.py      # chia văn bản/HTML theo ranh giới đoạn
  cache.py        # lưu tiến độ dạng JSON lines
  cli.py          # lệnh genres / analyze / translate
```

## Giới hạn hiện tại

- Kiểm tra nhất quán là so khớp chữ: nó bắt chắc chắn lỗi **sót tên gốc**, còn mục "cần xem lại" (không thấy tên đã dịch) thường là do câu dùng đại từ, nên chỉ được báo cáo, không tự sửa. Kiểm tra xưng hô không tự động được, mà dựa vào sổ tay được đưa vào mỗi đoạn.
- Chưa hỗ trợ PDF/DOCX. Có thể chuyển sang EPUB bằng [Calibre](https://calibre-ebook.com/) (`ebook-convert book.pdf book.epub`) rồi dịch.
- Tiêu đề trong `<head>` và mục lục `toc.ncx`/`nav` của EPUB chưa được dịch.
