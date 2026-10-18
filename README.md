# Ebook Translation Project

Công cụ dòng lệnh dịch sách tiếng Anh sang tiếng Việt bằng AI để đọc cho mục đích cá nhân. Đích nhắm là **văn dịch hay, lưu loát như dịch giả giỏi**. Dự án lấy bộ **Discworld** (Terry Pratchett) làm mục tiêu đầu tiên.

Bốn mục tiêu và cách công cụ đáp ứng:

| Mục tiêu | Cách làm |
|---|---|
| **1. Nhất quán** thế giới, tên nhân vật, địa danh, sự kiện | **Sổ tay truyện** (file JSON): đọc trước cả sách để lập danh sách tên riêng, cách xưng hô, tóm tắt và sự kiện từng chương. Mỗi tên chỉ có một bản dịch. Có bước kiểm tra sau khi dịch. Dùng chung một sổ tay cho cả bộ truyện. |
| **2. Dịch có chiều sâu**, không word-by-word | Dịch theo **thể loại** (Discworld: `comic`) với danh sách lỗi "văn dịch" cần tránh, cộng **bước biên tập**: một lượt thứ hai đối chiếu bản gốc và nhuận sắc bản nháp. |
| **3. Theo phong cách một dịch giả** (vd. Lý Lan dịch Harry Potter) | **Hồ sơ phong cách dịch**: có sẵn mô tả kiểu Lý Lan. Tốt nhất là **tự học từ sách tham chiếu** bạn đưa vào (`hoc-phong-cach`). |
| **4. Ít token nhất; đổi được API** khi Claude hết quota | Prompt cố định để trúng cache, chỉ gửi phần sổ tay liên quan, lưu lại mọi kết quả, có 3 **mức chi phí**, bước phân tích dùng model rẻ hơn. Hỗ trợ **Claude, DeepSeek, ChatGPT**; lỗi hoặc hết quota thì tự chuyển sang API kế tiếp. |

## Cách dễ nhất: giao diện trên trình duyệt

Sau khi cài đặt (xem bên dưới), **bấm đúp `Mo-giao-dien.bat`** (Windows) hoặc **`mo-giao-dien.command`** (macOS), hoặc gõ:

```bash
booktranslator giao-dien
```

Trình duyệt tự mở trang `http://127.0.0.1:8765`. Giao diện chạy trên máy bạn; sách và kết quả nằm trong thư mục `BookTranslator` ở thư mục người dùng (đổi bằng `--thu-muc`). Các bước:

1. **Sách:** kéo thả file EPUB vào. Bấm "Cài bộ khởi đầu Discworld" để có sẵn file hướng dẫn dịch và bảng tên riêng.
2. **Cài đặt dịch:** chọn thể loại, phong cách, mức chi phí, và AI nào dùng (**Claude Code** để dùng gói Pro/Max của bạn, không tốn tiền API).
3. **Hướng dẫn dịch:** sửa "prompt" của cuốn sách ngay trên trang.
4. **Sổ tay tên riêng:** bảng tên và cách xưng hô, sửa trực tiếp.
5. **Dịch & kết quả:** *Dịch thử*, rồi đọc bản gốc / bản nháp / bản biên tập đặt cạnh nhau. Khi đã ưng thì bấm *Dịch cả cuốn*. Có nút *Dừng*; lần sau chạy tiếp từ chỗ dừng.

Ngoài ra có tab **★ Học phong cách** (học từ sách tham chiếu) và tab **⚙ Kết nối AI** (xem trạng thái, nhập API key tạm thời).

## Dùng gói Claude Pro/Max qua Claude Code (không tốn tiền API)

1. Cài Claude Code: https://claude.com/claude-code
2. Mở Terminal, gõ `claude` và đăng nhập bằng tài khoản Claude của bạn. Chỉ cần làm một lần.
3. Trong giao diện, tick **Claude Code**. Nếu dùng dòng lệnh, thêm `--providers claude-code`.

Lưu ý:
- **Hạn mức:** việc dịch tính vào hạn mức của gói (theo khung vài giờ và theo tuần), không phải tiền API. Dịch cả cuốn có biên tập khá tốn hạn mức: gói Pro dễ chạm giới hạn giữa chừng, gói Max thoải mái hơn. Chạm giới hạn thì phần mềm dừng và lưu tiến độ; hoặc thêm DeepSeek làm dự phòng (`--providers claude-code,deepseek`).
- **Sách dài, hết hạn mức giữa chừng:** mỗi đoạn dịch xong được lưu ngay. Mặc định, khi hết hạn mức, phần mềm **tự chờ** đến giờ hạn mức hồi lại (hoặc cứ 15 phút thử lại một lần), rồi dịch tiếp, tối đa 12 giờ. Đổi bằng `--cho-han-muc 48`, tắt bằng `--khong-cho`; trên giao diện là ô "hết hạn mức thì tự chờ". Tắt máy hay bấm Dừng giữa chừng cũng không sao: lần sau chạy lại là tiếp tục đúng đoạn đang dở. Khi để chờ qua đêm, nhớ tắt chế độ ngủ (sleep) của máy.
- **Không bị tính tiền API:** khi gọi Claude Code, phần mềm cố ý **không** truyền `ANTHROPIC_API_KEY`, để Claude Code luôn dùng gói đã đăng nhập.
- **Chỉ dùng cá nhân:** đây là dùng tài khoản của chính bạn để dịch sách tự đọc. Đừng dùng cách này để chạy dịch vụ cho người khác.

## Bắt đầu nhanh bằng dòng lệnh: dịch thử một cuốn Discworld

**1. Tải về và cài đặt** (cần [Python ≥ 3.10](https://www.python.org/downloads/) và [Git](https://git-scm.com/)):

```bash
git clone -b claude/ai-book-translator https://github.com/setavn88/Ebook-Translation-Project.git
cd Ebook-Translation-Project
pip install -e '.[openai]'
```

**2. Đặt API key** (lấy tại https://platform.claude.com):

```bash
# macOS / Linux
export ANTHROPIC_API_KEY="sk-ant-..."
# Windows PowerShell
$env:ANTHROPIC_API_KEY="sk-ant-..."
```

**3. Dịch thử 2 đoạn** (vài nghìn chữ) để xem giọng văn và chi phí:

```bash
booktranslator dich-thu "The Colour of Magic.epub" -g comic -p lylan \
    --huong-dan examples/discworld/huong-dan-dich.md \
    --glossary examples/discworld/ten-rieng.json \
    --so-tay discworld.bible.json
```

Mở `The Colour of Magic.vi.epub.preview.md`. File này có:
- danh sách tên riêng đã chọn,
- từng đoạn **bản gốc / bản nháp / bản biên tập** đặt cạnh nhau,
- số token đã dùng, in ở cuối màn hình.

Chưa ưng chỗ nào thì sửa:

| Muốn sửa | Sửa ở đâu |
|---|---|
| Giọng văn, giọng nhân vật, cách xử lý câu đùa | `examples/discworld/huong-dan-dich.md`, mục "Ghi chú của tôi" hoặc bất kỳ đâu |
| Tên riêng | `discworld.bible.json` (hoặc `ten-rieng.json` trước lần chạy đầu) |

Rồi chạy lại lệnh trên. Đổi file hướng dẫn thì công cụ tự dịch lại phần thử.

**4. Dịch cả cuốn** (cùng tùy chọn; các đoạn đã dịch thử không tốn tiền lại):

```bash
booktranslator dich "The Colour of Magic.epub" -g comic -p lylan \
    --huong-dan examples/discworld/huong-dan-dich.md \
    --glossary examples/discworld/ten-rieng.json \
    --so-tay discworld.bible.json
```

Các cuốn Discworld sau dùng lại đúng `discworld.bible.json` và file hướng dẫn, để tên và giọng nhân vật thống nhất cả bộ. Khi đã học được phong cách từ sách Lý Lan (`hoc-phong-cach`, xem mục 3), thay `-p lylan` bằng `--phong-cach-file lylan-hp.json`.

Muốn rẻ hơn khi thử: thêm `--muc tiet-kiem` (Sonnet, không biên tập). Muốn bản tốt nhất: `--muc cao-cap` hoặc `--model claude-fable-5-1`.

## Cài đặt

Yêu cầu Python ≥ 3.10.

```bash
git clone https://github.com/setavn88/Ebook-Translation-Project.git
cd Ebook-Translation-Project
pip install -e '.[openai]'     # bỏ [openai] nếu chỉ dùng Claude

export ANTHROPIC_API_KEY="sk-ant-..."   # Claude: https://platform.claude.com
export DEEPSEEK_API_KEY="sk-..."        # tùy chọn: https://platform.deepseek.com
export OPENAI_API_KEY="sk-..."          # tùy chọn: https://platform.openai.com
```

## Quy trình cho Discworld

```bash
# 0. (Một lần) Học phong cách Lý Lan từ sách tham chiếu bạn có
booktranslator hoc-phong-cach "HP1 - Lý Lan.epub" --ban-goc "HP1 - English.epub" \
    --ten "Lý Lan - Harry Potter" -o lylan-hp.json

# 1. Dịch thử 2 đoạn, xem giọng văn và các tên được Việt hóa
booktranslator dich-thu "Guards! Guards!.epub" -g comic --phong-cach-file lylan-hp.json

# 2. Lập sổ tay cho cả cuốn, mở file JSON sửa tên cho ưng ý
booktranslator phan-tich "Guards! Guards!.epub" -g comic --phong-cach-file lylan-hp.json -o discworld.bible.json

# 3. Dịch cả cuốn. Các cuốn Discworld sau dùng lại discworld.bible.json để tên thống nhất cả bộ
booktranslator dich "Guards! Guards!.epub" -g comic --phong-cach-file lylan-hp.json --so-tay discworld.bible.json
```

Kết quả gồm:
- `Guards! Guards!.vi.epub`: sách đã dịch.
- `discworld.bible.json`: sổ tay, được bổ sung dần.
- `….consistency.md`: báo cáo nhất quán.

Chạy lại cùng lệnh sau khi bị ngắt thì công cụ tiếp tục từ chỗ dừng, không trả tiền lại cho phần đã dịch.

Các lệnh có tên tiếng Anh tương đương: `learn-style`, `preview`, `analyze`, `translate`, `genres`, `styles`.

## 1. Nhất quán: sổ tay truyện

```
 ① phan-tich                 ② dich                            ③ kiểm tra & sửa
 đọc trước từng chương  →    mỗi đoạn gửi kèm phần sổ tay  →   đối chiếu từng đoạn với sổ tay;
 lập SỔ TAY:                 LIÊN QUAN tới đoạn đó:             đoạn còn sót tên gốc thì tự dịch
 • tên riêng → bản dịch      • tên có mặt + xưng hô của họ      lại với lời nhắc; ghi báo cáo
 • tên khác (alias)          • tóm tắt + sự kiện chương trước
 • xưng hô từng cặp          • đuôi đoạn trước + bản dịch
 • tóm tắt, sự kiện          tên mới gặp → bổ sung sổ tay
```

- **Một tên chỉ có một bản dịch.** Khi một tên đã có trong sổ tay, bản dịch không bao giờ bị thay tự động. Muốn đổi thì sửa file JSON rồi chạy lại với `--fresh`.
- **Kết quả không phụ thuộc thứ tự chạy.** Các chương được phân tích song song, nhưng khi gộp thì chương đứng trước trong sách luôn thắng.
- **Diễn biến khớp nhau.** Mô hình biết ai đã chết, bí mật nào đã lộ, quan hệ đang ở giai đoạn nào.
- **Cả series dùng chung một sổ tay** (`--so-tay`). Tên như Ankh-Morpork, Đại học Vô Hình, Cái Rương sẽ giống nhau ở mọi cuốn.

Ví dụ sổ tay:

```json
{
  "entities": [
    {"source": "Unseen University", "target": "Đại học Vô Hình", "type": "organization", "aliases": ["UU"], "gender": "", "note": "trường phù thủy"},
    {"source": "the Luggage", "target": "Cái Rương", "type": "artifact", "aliases": ["Luggage"], "gender": "", "note": "rương gỗ biết đi"}
  ],
  "addressing": [
    {"speaker": "Rincewind", "listener": "Twoflower", "self_term": "tôi", "other_term": "ông", "context": "mới quen, khó chịu"}
  ],
  "sections": {"OEBPS/ch01.xhtml": {"title": "Chương 1", "summary": "...", "events": ["..."]}}
}
```

## 2. Dịch có chiều sâu: thể loại + biên tập

`booktranslator the-loai` liệt kê các thể loại:

| `-g` | Thể loại | Effort mặc định |
|---|---|---|
| `comic` | **Giả tưởng hài / châm biếm (Discworld, Hitchhiker's Guide)**: chơi chữ được sáng tác lại bằng tiếng Việt; giọng tỉnh bơ; giữ cú chốt cuối câu; dịch đủ footnote của tác giả; lời Thần Chết giữ IN HOA; tên có nghĩa Việt hóa dí dỏm | high |
| `fantasy` | Giả tưởng / kỳ ảo | medium |
| `scifi` | Khoa học viễn tưởng | medium |
| `romance` | Lãng mạn / ngôn tình (ưu tiên xưng hô theo giai đoạn quan hệ) | medium |
| `wuxia` | Tiên hiệp / kiếm hiệp (Hán Việt) | high |
| `mystery` | Trinh thám (giữ chính xác manh mối) | high |
| `horror`, `literary`, `general` | Kinh dị, văn chương kinh điển, tổng quát | |

Thể loại tự định nghĩa: xem [`examples/genre-cyberpunk.json`](examples/genre-cyberpunk.json), dùng với `--the-loai-file`.

Khi đích là tiếng Việt, mọi bước đều theo một **danh sách lỗi "văn dịch"** cần tránh: dịch chữ theo trật tự tiếng Anh, "một cách + tính từ", lạm dụng "được/bị", "của anh ấy" thừa, lặp "anh ấy/cô ấy", khung "Đó là… / Có một…", lời thoại cứng, "said" lúc nào cũng thành "nói"…

**Bước biên tập** (bật ở mức `can-bang` và `cao-cap`): lượt thứ hai đọc *bản gốc + bản nháp*, sửa chỗ hiểu sai, rồi nhuận sắc cho câu đùa buồn cười hơn, giọng nhân vật rõ hơn. Tên riêng giữ đúng sổ tay.

## 3. Phong cách dịch giả

`booktranslator phong-cach` liệt kê phong cách có sẵn: `natural` (mặc định, trung tính) và `lylan` (mô tả sẵn đặc điểm lối dịch Harry Potter của Lý Lan: Việt hóa tên có nghĩa kiểu Hẻm Xéo, Giám ngục; khẩu ngữ sinh động; bạn bè xưng "tớ – bồ"; thầy cô gọi "trò"). Phong cách và thể loại kết hợp tự do.

### Học phong cách từ sách tham chiếu (khuyên dùng)

```bash
booktranslator hoc-phong-cach "HP1 - Lý Lan.epub" --ban-goc "HP1 - English.epub" --ten "Lý Lan - Harry Potter" -o lylan-hp.json
```

- Công cụ đọc khoảng 4 trích đoạn rải đều trong sách (`--so-trich-doan`), không đọc cả cuốn, nên chỉ tốn vài lần gọi API.
- **Có cả bản gốc tiếng Anh**: tìm các cặp câu gốc ↔ dịch tiêu biểu (lời thoại, câu đùa, thành ngữ được Việt hóa) và cách Việt hóa tên (Diagon Alley → Hẻm Xéo…).
- **Chỉ có bản tiếng Việt**: vẫn học được nhịp văn, cách xưng hô, giọng kể. Đoạn mẫu khi đó chỉ có bản tiếng Việt.
- Kết quả là file JSON gồm mô tả phong cách, quy luật đặt tên, quy luật xưng hô và các đoạn mẫu. **Bạn đọc lại và sửa được.** Dùng bằng `--phong-cach-file lylan-hp.json`.
- File này chứa trích đoạn sách của bạn, **chỉ dùng cá nhân**, đừng đưa lên repo công khai.

Tự viết hồ sơ phong cách cũng được: xem [`examples/phong-cach-mau.json`](examples/phong-cach-mau.json). File kế thừa `base`, thêm `examples`, có thể ghi đè `principles`/`naming`/`addressing`.

## 4. Tiết kiệm token và đổi API

### Mức chất lượng / chi phí (`--muc`)

| `--muc` | Model dịch (Claude) | Model phân tích | Biên tập | Effort |
|---|---|---|---|---|
| `tiet-kiem` | `claude-sonnet-5-5` | `claude-haiku-5-5` | không | tối đa `medium` |
| `can-bang` *(mặc định)* | `claude-opus-5-5` | `claude-sonnet-5-5` | có | theo thể loại |
| `cao-cap` | `claude-opus-5-5` | `claude-opus-5-5` | có | tối thiểu `high` |

- `--model` ghi đè model dịch, ví dụ `--model claude-fable-5-1`: model mạnh nhất, giá khoảng 2,5 lần Opus. Nên so hai bản bằng `dich-thu` trước.
- `--effort`, `--polish`, `--no-polish` ghi đè từng phần của mức đã chọn.

### Những cách tiết kiệm có sẵn

- **Prompt cache.** Prompt hệ thống (quy tắc thể loại, phong cách, đoạn mẫu) giữ nguyên cho cả cuốn, nên các request sau chỉ tính khoảng 10% giá cho phần đó.
- **Chỉ gửi phần sổ tay liên quan** tới từng đoạn, thay vì cả sổ tay (vốn lên tới hàng trăm mục với cả bộ Discworld).
- **Phân tích** dùng model rẻ hơn với effort thấp, và không gửi kèm đoạn mẫu.
- **Lưu mọi kết quả.** Chạy lại, `dich-thu` rồi `dich`, hay bị ngắt giữa chừng đều không tốn tiền lại cho phần đã làm.
- **`dich-thu` trước.** Lệnh này in số token đã dùng cho vài đoạn; nhân lên theo số đoạn của sách để ước lượng chi phí cả cuốn trước khi chạy.

### Nhiều nhà cung cấp API (`--providers`)

```bash
# Claude trước; Claude hết quota, rate limit hay lỗi thì tự chuyển sang DeepSeek, rồi ChatGPT
booktranslator dich book.epub -g comic --providers claude,deepseek,openai

# Chỉ dùng DeepSeek (rẻ nhất), hoặc chỉ định model
booktranslator dich book.epub -g comic --providers deepseek
booktranslator dich book.epub -g comic --providers deepseek:deepseek-reasoner,openai:gpt-5
```

| Tên | Biến môi trường | Model mặc định |
|---|---|---|
| `claude-code` | không cần (đăng nhập Claude Code) | theo `--muc` |
| `claude` | `ANTHROPIC_API_KEY` | theo `--muc` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| `openai` (hay `chatgpt`) | `OPENAI_API_KEY` | `gpt-5` |

- Nhà cung cấp chưa có key thì bị bỏ qua.
- **Hết quota / rate limit / mất mạng**: chuyển hẳn sang nhà cung cấp kế tiếp.
- **Một đoạn bị từ chối**: chỉ đoạn đó được thử ở nhà cung cấp kế tiếp.
- Phần đã dịch vẫn giữ nguyên, sổ tay vẫn giữ tên nhất quán dù đổi API giữa chừng. Giọng văn giữa hai model có thể hơi khác, nên với một cuốn sách nên ưu tiên dịch trọn bằng một model.
- Tên model của DeepSeek và OpenAI thay đổi theo thời gian; kiểm tra trang của họ và chỉ định bằng `tên:model` nếu cần.

## Tất cả tùy chọn

| Tùy chọn | Mặc định | Ý nghĩa |
|---|---|---|
| `-g, --the-loai` / `--the-loai-file` | `general` | Thể loại |
| `-p, --phong-cach` / `--phong-cach-file` | `natural` | Phong cách dịch |
| `--muc` | `can-bang` | Mức chất lượng/chi phí |
| `--providers` | `claude` | Nhà cung cấp API theo thứ tự ưu tiên |
| `--model`, `--effort` | theo `--muc` | Ghi đè model dịch / mức suy nghĩ |
| `--polish` / `--no-polish` | theo `--muc` | Bật/tắt bước biên tập |
| `--so-tay FILE` | `<kết quả>.bible.json` | File sổ tay truyện |
| `--glossary FILE` | | Nạp thêm tên `{"nguồn": "đích"}` vào sổ tay |
| `--yeu-cau "..."` | | Yêu cầu riêng, ưu tiên cao nhất |
| `--no-analyze` | | Bỏ bước đọc trước (rẻ hơn, kém nhất quán; nên kèm `--workers 1`) |
| `--no-fix` | | Chỉ báo cáo lỗi nhất quán, không tự sửa |
| `--fresh` | | Bỏ qua kết quả đã lưu |
| `--cho-han-muc GIỜ` / `--khong-cho` | 12 giờ | Hết hạn mức thì tự chờ rồi dịch tiếp / dừng luôn |
| `--to` / `--from` / `--lang-code` | Tiếng Việt / tự nhận / `vi` | Ngôn ngữ |
| `--chunk-chars` | `6000` | Kích thước mỗi đoạn |
| `--workers` | `4` | Số chương xử lý song song |
| `dich-thu --section N --chunks K` | chương đủ dài đầu tiên, 2 đoạn | Chọn đoạn dịch thử |

## Docker

```bash
docker build -t booktranslator .
docker run --rm -e ANTHROPIC_API_KEY -e DEEPSEEK_API_KEY -v "$PWD:/books" booktranslator dich /books/book.epub -g comic
```

## Phát triển

```bash
pip install -e '.[dev,openai]'
pytest          # test dùng API giả lập, không cần key
```

```
src/booktranslator/
  providers.py    # Claude / DeepSeek / ChatGPT + chuỗi dự phòng
  genres.py       # thể loại (văn phong, quy tắc tên, thứ cần theo dõi)
  styles.py       # phong cách dịch giả + nạp hồ sơ phong cách
  stylelearn.py   # học phong cách từ sách tham chiếu
  bible.py        # sổ tay truyện: tên, alias, xưng hô, tóm tắt/sự kiện
  translator.py   # prompt, phân tích chương, dịch nháp, biên tập
  consistency.py  # kiểm tra tên gốc còn sót
  pipeline.py     # phân tích → dịch → kiểm tra & sửa; dịch thử
  document.py     # đọc/ghi TXT, Markdown, EPUB theo chương
  webui.py, webui.html  # giao diện trên trình duyệt (booktranslator giao-dien)
  chunker.py, cache.py, cli.py
```

## Giới hạn hiện tại

- **Kiểm tra nhất quán** là so khớp chữ. Nó bắt chắc lỗi *sót tên gốc*. Còn chỗ "không thấy tên đã dịch" thường là do câu dùng đại từ, nên chỉ được ghi vào báo cáo. Xưng hô không kiểm tra tự động được, mà dựa vào sổ tay gửi kèm từng đoạn.
- **Phong cách `lylan` có sẵn** chỉ là mô tả đặc điểm kèm vài câu mẫu tự soạn, nên chỉ *gần giống*. Muốn sát giọng thì học từ sách tham chiếu.
- **Định dạng:** chưa hỗ trợ PDF/DOCX; có thể chuyển sang EPUB bằng [Calibre](https://calibre-ebook.com/) (`ebook-convert book.pdf book.epub`). Tiêu đề `<head>` và mục lục `toc.ncx`/`nav` chưa được dịch.
- **Chưa dùng Batch API**, vốn rẻ hơn 50% nhưng phải chờ kết quả. Đây là hướng tiết kiệm tiếp theo.
