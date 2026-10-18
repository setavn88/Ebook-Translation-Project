"""Chế độ dịch theo thể loại sách.

Mỗi thể loại quyết định: văn phong, cách xử lý tên riêng, những loại thực thể
cần theo dõi trong sổ tay truyện, điều cần chú ý khi phân tích diễn biến, và
mức effort mặc định.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class GenreProfile:
    key: str
    name: str
    description: str
    style: str
    naming: str
    entity_types: tuple[str, ...]
    analysis_focus: str
    effort: str = "medium"
    track_addressing: bool = True

    def prompt_block(self) -> str:
        return "\n".join(
            [
                f"THỂ LOẠI: {self.name}",
                "",
                "Văn phong và cách dịch:",
                self.style.strip(),
                "",
                "Tên riêng và thuật ngữ:",
                self.naming.strip(),
            ]
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entity_types"] = list(self.entity_types)
        return d


_COMMON_TYPES = ("character", "place", "organization", "term")

BUILTIN: dict[str, GenreProfile] = {}


def _register(profile: GenreProfile) -> None:
    BUILTIN[profile.key] = profile


_register(
    GenreProfile(
        key="general",
        name="Tổng quát",
        description="Sách phổ thông, phi hư cấu, hoặc khi không rõ thể loại.",
        style="""
- Dịch rõ ràng, chính xác, tự nhiên; giữ giọng văn gốc (trang trọng hay thân mật).
- Thành ngữ dịch bằng thành ngữ tương đương trong ngôn ngữ đích nếu có, không dịch word-by-word.
""",
        naming="""
- Tên người nước ngoài giữ nguyên chính tả gốc (chữ Latin). Tên từ chữ Hán dùng âm Hán Việt.
- Tên tổ chức, địa danh đã có tên tiếng Việt quen thuộc thì dùng tên đó (London → Luân Đôn chỉ khi văn cảnh cổ; mặc định giữ London).
""",
        entity_types=_COMMON_TYPES,
        analysis_focus="nhân vật, địa danh, tổ chức, thuật ngữ chuyên môn lặp lại, các sự kiện chính.",
    )
)

_register(
    GenreProfile(
        key="scifi",
        name="Khoa học viễn tưởng (Sci-fi)",
        description="Không gian, công nghệ tương lai, AI, du hành thời gian, cyberpunk.",
        style="""
- Giọng văn sắc, mạch lạc; mô tả kỹ thuật phải chính xác và dễ hiểu, không làm mềm hay văn hoa hóa.
- Thuật ngữ khoa học có thật (vật lý, thiên văn, sinh học, tin học) dùng thuật ngữ chuẩn tiếng Việt
  (wormhole → lỗ sâu, event horizon → chân trời sự kiện, warp drive → động cơ bẻ cong không gian).
- Thuật ngữ do tác giả sáng tạo: chọn một cách dịch ngắn gọn, giàu hình ảnh và dùng y hệt suốt cuốn sách.
- Giữ nguyên số liệu, đơn vị đo, mã hiệu, ký hiệu kỹ thuật, tên lệnh máy tính.
- AI/robot: xưng hô theo cách tác giả nhân hóa (nếu nhân vật đối xử như người thì không gọi là "nó").
""",
        naming="""
- Tên người giữ nguyên chính tả gốc. Mật danh/biệt hiệu có nghĩa thì dịch nghĩa nếu tác giả dùng như một từ có nghĩa.
- Tên tàu vũ trụ, trạm, AI, hành tinh: giữ nguyên tên gốc; chữ viết tắt (UN, AI, FTL) giữ nguyên.
- Tên tổ chức có nghĩa rõ ràng thì dịch nghĩa (the Federation → Liên bang, Earth Defense Force → Lực lượng Phòng vệ Trái Đất).
""",
        entity_types=("character", "ai", "ship", "planet", "place", "organization", "technology", "species", "unit", "term"),
        analysis_focus=(
            "nhân vật (kể cả AI), tàu và phương tiện, hành tinh/trạm, tổ chức/phe phái, công nghệ và thuật ngữ "
            "do tác giả sáng tạo, mốc thời gian và thay đổi lớn của thế giới (chiến tranh, phát minh, thảm họa)."
        ),
    )
)

_register(
    GenreProfile(
        key="fantasy",
        name="Giả tưởng / Kỳ ảo (Fantasy)",
        description="Thế giới phép thuật, sử thi, chủng tộc huyền bí, vương quốc.",
        style="""
- Giọng văn giàu hình ảnh, hơi cổ điển và trang trọng ở phần tự sự và lời của vua chúa/pháp sư; đối thoại đời thường vẫn tự nhiên.
- Xưng hô phản ánh địa vị và quan hệ: thần dân - bệ hạ, ta - ngươi (kẻ thù, bề trên kiêu ngạo),
  thầy - trò, các ngài/tiểu thư... giữ ổn định cho từng cặp nhân vật.
- Thơ, lời tiên tri, bài hát: dịch có vần điệu nếu có thể nhưng giữ đủ ý (lời tiên tri thường là manh mối).
- Thuật ngữ phép thuật, hệ thống ma thuật: dịch nhất quán, không đổi từ đồng nghĩa giữa các chương.
""",
        naming="""
- Tên người, địa danh bịa ra giữ nguyên chính tả gốc (Frodo, Mordor).
- Biệt danh, danh hiệu, tên có nghĩa thì dịch nghĩa: the Dark Lord → Chúa tể Hắc ám, Kingslayer → Kẻ Giết Vua,
  the Shire → giữ nguyên hoặc dịch nghĩa nhưng phải thống nhất.
- Chủng tộc dùng tên quen thuộc: elf → tiên/người Elf, dwarf → người lùn, orc → Orc, dragon → rồng (chọn một và giữ nguyên).
- Tên phép thuật, vũ khí, cổ vật có nghĩa thì dịch nghĩa, viết hoa như tên riêng.
""",
        entity_types=("character", "race", "creature", "place", "kingdom", "faction", "magic", "artifact", "title", "term"),
        analysis_focus=(
            "nhân vật và danh hiệu của họ, chủng tộc, sinh vật, vương quốc/địa danh, phe phái, hệ thống phép thuật, "
            "cổ vật, lời tiên tri; các sự kiện then chốt (cái chết, phản bội, lên ngôi, tiết lộ thân thế)."
        ),
    )
)

_register(
    GenreProfile(
        key="romance",
        name="Lãng mạn / Ngôn tình (Romance)",
        description="Tình cảm, lãng mạn đương đại hoặc cổ trang, chick-lit.",
        style="""
- Ưu tiên cảm xúc và nội tâm: câu văn mềm mại, giàu cảm xúc, nhịp nhàng; đối thoại tự nhiên như người thật nói.
- XƯNG HÔ LÀ ƯU TIÊN SỐ MỘT: chọn đại từ phù hợp tuổi tác, vai vế, mức độ thân mật (tôi - anh, em - anh, tớ - cậu,
  mình - cậu...). Xưng hô của một cặp chỉ thay đổi khi quan hệ thay đổi trong truyện (quen → yêu, cãi nhau, chia tay)
  và phải khớp với giai đoạn ghi trong sổ tay.
- Giữ nguyên sắc thái tán tỉnh, châm chọc, ngập ngừng; không làm câu chữ sến hơn hay thô hơn bản gốc.
- Cảnh nhạy cảm dịch tinh tế, đúng mức độ của bản gốc.
""",
        naming="""
- Tên nhân vật giữ nguyên chính tả gốc (tên tiếng Trung dùng Hán Việt).
- Biệt danh, tên gọi yêu (pet names: babe, sweetheart, honey) dịch tự nhiên và thống nhất cho từng nhân vật
  (một nhân vật luôn gọi người yêu bằng cùng một cách).
- Tên thương hiệu, quán xá, thành phố giữ nguyên.
""",
        entity_types=("character", "nickname", "place", "organization", "term"),
        analysis_focus=(
            "nhân vật và quan hệ giữa họ, CÁCH XƯNG HÔ của từng cặp và lúc nó thay đổi, biệt danh/tên gọi yêu, "
            "các mốc của mối quan hệ (gặp gỡ, hiểu lầm, tỏ tình, chia tay, tái hợp), bí mật giữa các nhân vật."
        ),
    )
)

_register(
    GenreProfile(
        key="wuxia",
        name="Tiên hiệp / Kiếm hiệp / Huyền huyễn",
        description="Truyện võ hiệp, tu tiên, huyền huyễn (thường dịch từ tiếng Trung).",
        effort="high",
        style="""
- Giọng cổ phong, súc tích; dùng từ Hán Việt hợp lý cho chiêu thức, cảnh giới, danh hiệu, nhưng câu văn vẫn dễ đọc.
- Xưng hô kiểu cổ trang và nhất quán: ta - ngươi, tại hạ - các hạ, sư phụ - đồ nhi, sư huynh - sư muội,
  bổn tọa, lão phu, tiểu nữ... chọn theo thân phận và quan hệ; không trộn xưng hô hiện đại.
- Thơ và câu đối dịch giữ vần, đối, hoặc để Hán Việt kèm ý nghĩa trong câu văn (không thêm chú thích riêng).
- Thứ bậc cảnh giới tu luyện phải đúng thứ tự và cùng một tên gọi suốt truyện.
""",
        naming="""
- Tên người, môn phái, địa danh, công pháp, pháp bảo, cảnh giới: dùng âm Hán Việt (张无忌 → Trương Vô Kỵ,
  峨眉派 → Nga Mi phái, 筑基 → Trúc Cơ). Không dịch nghĩa tên người.
- Danh hiệu, ngoại hiệu dùng Hán Việt quen thuộc (毒手药王 → Độc Thủ Dược Vương).
- Nguồn không phải tiếng Trung: dùng Hán Việt cho thuật ngữ thể loại nếu tương đương rõ ràng, tên người giữ theo nguồn.
""",
        entity_types=("character", "sect", "technique", "realm", "artifact", "place", "title", "beast", "term"),
        analysis_focus=(
            "nhân vật, ngoại hiệu, sư môn và quan hệ sư đồ/huynh đệ, môn phái, công pháp/chiêu thức, cảnh giới (theo thứ tự), "
            "pháp bảo/binh khí, địa danh; các sự kiện: đột phá cảnh giới, kết thù, báo thù, đổi phe, thân thế bị lộ."
        ),
    )
)

_register(
    GenreProfile(
        key="mystery",
        name="Trinh thám / Ly kỳ (Mystery & Thriller)",
        description="Phá án, điều tra, gián điệp, tâm lý tội phạm.",
        effort="high",
        style="""
- Chính xác tuyệt đối với chi tiết: thời gian, địa điểm, số liệu, đồ vật, lời khai. Mọi chi tiết đều có thể là manh mối.
- Giữ nguyên sự mơ hồ có chủ đích của tác giả: không làm rõ đại từ nếu bản gốc cố tình mập mờ, không tiết lộ trước hung thủ,
  không thêm ý suy diễn.
- Nhịp văn nhanh, câu gọn ở cảnh căng thẳng; thuật ngữ pháp y, cảnh sát, pháp lý dùng từ chuẩn tiếng Việt.
""",
        naming="""
- Tên người, địa danh giữ nguyên chính tả gốc.
- Cấp bậc cảnh sát, chức danh dịch theo tương đương thông dụng (Detective Inspector → Thanh tra, Sergeant → Trung sĩ)
  và giữ thống nhất cho từng nhân vật.
- Tên vật chứng, mật danh, bí danh: dịch hoặc giữ nguyên nhưng phải y hệt ở mọi lần xuất hiện để người đọc nhận ra manh mối.
""",
        entity_types=("character", "place", "organization", "clue", "alias", "term"),
        analysis_focus=(
            "nhân vật (nghi phạm, nhân chứng, nạn nhân, điều tra viên) và chức danh, bí danh, địa điểm, vật chứng/manh mối, "
            "dòng thời gian của vụ việc, chứng cứ ngoại phạm, những tiết lộ và cú lật."
        ),
    )
)

_register(
    GenreProfile(
        key="horror",
        name="Kinh dị (Horror)",
        description="Kinh dị, siêu nhiên, tâm lý rùng rợn.",
        style="""
- Ưu tiên không khí: chọn từ gợi cảm giác (lạnh, ẩm, tĩnh lặng, mục ruỗng), giữ nhịp chậm khi tác giả dồn nén
  và câu ngắn, dồn dập khi cao trào.
- Không làm giảm hay phóng đại mức độ ghê rợn so với bản gốc.
- Tên gọi thực thể siêu nhiên, nghi lễ, câu thần chú giữ nhất quán; thần chú bằng tiếng lạ thì giữ nguyên.
""",
        naming="""
- Tên người, địa danh giữ nguyên chính tả gốc.
- Thực thể có tên gọi mang nghĩa (the Thing in the Walls) thì dịch nghĩa, viết hoa và dùng y hệt mỗi lần xuất hiện.
""",
        entity_types=("character", "entity", "place", "ritual", "artifact", "term"),
        analysis_focus="nhân vật, thực thể siêu nhiên, địa điểm ám ảnh, nghi lễ, đồ vật bị nguyền; ai chết, ai bị nhập, bí mật được hé lộ.",
    )
)

_register(
    GenreProfile(
        key="comic",
        name="Giả tưởng hài hước / Châm biếm (Discworld, Hitchhiker's Guide)",
        description="Fantasy/sci-fi trào phúng: chơi chữ, châm biếm, chú thích hài của tác giả.",
        effort="high",
        style="""
- Cái hài là nội dung chính: mỗi câu đùa, lối nói mỉa, phép so sánh ngớ ngẩn phải buồn cười bằng tiếng Việt.
  Chơi chữ không dịch được nghĩa đen thì sáng tác lại một trò chơi chữ tiếng Việt cùng ý và cùng chỗ đặt.
- Giữ giọng tỉnh bơ, nói giảm nói tránh kiểu Anh (understatement): chuyện kinh thiên động địa kể bằng giọng bình thản.
- Câu dài kiểu liệt kê, lạc đề rồi quay lại của tác giả: giữ cấu trúc và cú "chốt" ở cuối câu, vì cú chốt thường
  là chỗ gây cười — đừng đảo cú chốt lên đầu.
- Chú thích (footnote) của tác giả là một phần của truyện, thường là chỗ hài nhất: dịch đầy đủ, cùng giọng, giữ
  nguyên vị trí và liên kết.
- Lời thoại viết IN HOA toàn bộ trong bản gốc (ví dụ lời của Thần Chết - DEATH trong Discworld) phải giữ IN HOA,
  không dấu ngoặc kép nếu bản gốc không có.
- Ám chỉ văn hóa (nhại văn học, phim, thành ngữ Anh) thì tìm cách nói tương đương để người Việt vẫn bắt được ý,
  không thêm chú thích giải thích.
""",
        naming="""
- Tên người kiểu bịa không có nghĩa rõ (Rincewind, Vimes) giữ nguyên chính tả.
- Tên có nghĩa hoặc là trò đùa (địa danh, tổ chức, chức danh, biệt danh, đồ vật biết đi) dịch thành tên tiếng Việt
  ngắn gọn, dí dỏm, viết hoa như tên riêng (Unseen University → Đại học Vô Hình, the Luggage → Cái Rương,
  the Patrician → Ngài Thống lĩnh). Một khi đã chọn thì dùng mãi một tên.
- Tên thành phố/vùng đất bịa giữ nguyên (Ankh-Morpork) trừ khi tác giả cố ý dùng như một từ có nghĩa.
""",
        entity_types=("character", "title", "place", "organization", "creature", "artifact", "magic", "term"),
        analysis_focus=(
            "nhân vật và danh xưng/biệt danh, tổ chức và chức danh, địa danh, sinh vật, đồ vật có tính cách, các trò "
            "đùa và tên gọi lặp lại xuyên suốt (running gags) cần dịch thống nhất, nhân vật nói IN HOA."
        ),
    )
)

_register(
    GenreProfile(
        key="literary",
        name="Văn học kinh điển / Văn chương",
        description="Tiểu thuyết văn chương, kinh điển, sách có văn phong đặc trưng.",
        effort="high",
        style="""
- Trung thành tối đa với văn phong tác giả: độ dài câu, nhịp điệu, hình ảnh ẩn dụ, lối chơi chữ; không rút gọn câu dài
  và không gộp câu ngắn.
- Giữ không khí thời đại: từ ngữ không quá hiện đại với truyện cổ, không quá cổ với truyện đương đại.
- Phương ngữ, giọng nói riêng của nhân vật (quê mùa, quý tộc, trẻ con) thể hiện bằng cách dùng từ tương ứng và giữ ổn định.
""",
        naming="""
- Tên người, địa danh giữ nguyên chính tả gốc; tên đã có bản dịch kinh điển quen thuộc ở Việt Nam thì có thể dùng.
- Tước hiệu quý tộc dịch thống nhất (Duke → Công tước, Count → Bá tước, Lady → Phu nhân/Tiểu thư tùy vai vế).
""",
        entity_types=("character", "place", "organization", "title", "term"),
        analysis_focus="nhân vật, tước hiệu, quan hệ gia đình/xã hội, cách xưng hô, các biểu tượng và motif lặp lại, sự kiện chính.",
    )
)


def load_genre(key: str | None = None, path: Path | None = None) -> GenreProfile:
    """Lấy thể loại có sẵn theo tên, hoặc đọc thể loại tự định nghĩa từ file JSON."""
    if path is not None:
        data = json.loads(path.read_text(encoding="utf-8"))
        base = BUILTIN.get(data.get("base", "general"), BUILTIN["general"]).to_dict()
        base.update({k: v for k, v in data.items() if k != "base"})
        base["key"] = data.get("key", path.stem)  # không dùng chung key (và cache) với thể loại gốc
        base["entity_types"] = tuple(base["entity_types"])
        return GenreProfile(**base)
    key = key or "general"
    if key not in BUILTIN:
        raise KeyError(f"Không có thể loại '{key}'. Có sẵn: {', '.join(BUILTIN)}")
    return BUILTIN[key]
