"""Phong cách dịch: dịch giả viết thế nào — tách biệt với thể loại sách.

Thể loại trả lời "sách này là gì" (sci-fi, fantasy, hài châm biếm...). Phong cách
dịch trả lời "người dịch có giọng văn ra sao": độ Việt hóa, cách đặt tên có nghĩa,
xưng hô, nhịp câu. Có sẵn vài phong cách; tốt nhất là tự học từ một cuốn sách
tham chiếu bằng `booktranslator hoc-phong-cach` (xem stylelearn.py).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class StyleProfile:
    key: str
    name: str
    description: str
    principles: str
    naming: str = ""
    addressing: str = ""
    # (bản gốc, bản dịch); bản gốc có thể rỗng khi chỉ có văn mẫu tiếng Việt
    examples: tuple[tuple[str, str], ...] = ()

    def prompt_block(self) -> str:
        parts = [f"PHONG CÁCH DỊCH: {self.name}", "", self.principles.strip()]
        if self.naming.strip():
            parts += ["", "Đặt tên theo phong cách này (ưu tiên hơn quy tắc tên của thể loại nếu mâu thuẫn):", self.naming.strip()]
        if self.addressing.strip():
            parts += ["", "Xưng hô theo phong cách này:", self.addressing.strip()]
        if self.examples:
            parts += [
                "",
                "Đoạn mẫu của phong cách này. Hãy học cách diễn đạt, nhịp câu, độ Việt hóa, cách xưng hô; "
                "KHÔNG chép nội dung hay tên riêng của mẫu vào bản dịch:",
            ]
            for src, dst in self.examples:
                src_part = f"<source>{src}</source>\n" if src.strip() else ""
                parts.append(f"<example>\n{src_part}<translation>{dst}</translation>\n</example>")
        return "\n".join(parts)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["examples"] = [{"source": s, "target": t} for s, t in self.examples]
        return d

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


BUILTIN: dict[str, StyleProfile] = {}


def _register(v: StyleProfile) -> None:
    BUILTIN[v.key] = v


_register(
    StyleProfile(
        key="natural",
        name="Tự nhiên, trung tính",
        description="Văn dịch trong sáng, lưu loát, ít Việt hóa tên riêng. Mặc định.",
        principles="""
- Viết như một dịch giả giỏi: người đọc không nhận ra đây là văn dịch.
- Giữ giọng văn tác giả; Việt hóa vừa phải ở thành ngữ và lời thoại, giữ nguyên phần lớn tên riêng.
""",
    )
)

_register(
    StyleProfile(
        key="lylan",
        name="Việt hóa sinh động, dí dỏm (kiểu bản dịch Harry Potter của Lý Lan)",
        description="Mô tả sẵn các đặc điểm; muốn sát hơn hãy học từ sách tham chiếu bằng `hoc-phong-cach`.",
        principles="""
- Giọng kể trong sáng, dễ đọc cho cả thiếu nhi lẫn người lớn; câu gọn, nhịp nhanh, hóm hỉnh. Người đọc phải
  có cảm giác truyện được viết thẳng bằng tiếng Việt.
- Việt hóa mạnh tay nhưng không làm mất hồn bản gốc: lời thoại là khẩu ngữ người Việt nói thật, có tiểu từ tình
  thái (à, ư, nhỉ, nhé, chứ, đấy, hả, mà, cơ), có thành ngữ, lối nói dân gian khi hợp cảnh.
- Mỗi nhân vật một giọng: nhân vật quê mùa, ít học nói dân dã, nói tắt, hơi trật ngữ pháp (thể hiện bằng khẩu ngữ
  Việt dân dã chứ không bằng lỗi chính tả); kẻ kiêu kỳ nói kênh kiệu, châm chọc; trẻ con nói hồn nhiên.
- Câu đùa và chơi chữ: viết lại thành câu đùa tiếng Việt có cùng tác dụng, không dịch nghĩa đen.
- Dùng nhiều từ láy, từ tượng thanh, tượng hình cho cảnh sinh động (lom khom, lạch bạch, cái rụp, tỉnh bơ).
""",
        naming="""
- Tên người giữ nguyên chính tả gốc (Harry, Hermione...).
- Tên có nghĩa của địa danh, cửa hiệu, đồ vật thần kỳ, sinh vật, tổ chức, chức danh, biệt danh: đặt tên tiếng Việt
  ngắn, gợi hình, dễ nhớ, đôi khi dí dỏm — như cách bản Harry Potter tiếng Việt làm: Diagon Alley → Hẻm Xéo,
  Sorting Hat → Nón Phân loại, Death Eaters → Tử thần Thực tử, Dementors → Giám ngục, Ministry of Magic → Bộ
  Pháp thuật, He-Who-Must-Not-Be-Named → Kẻ-Mà-Ai-Cũng-Biết-Là-Ai-Đấy.
- Từ bịa không có nghĩa tiếng Anh (Quidditch, Muggle) và thần chú kiểu Latin giữ nguyên.
""",
        addressing="""
- Bạn bè cùng lứa tuổi học trò: xưng "tớ"/"mình", gọi "bồ"/"cậu"; nói về nhóm mình là "tụi mình".
- Học trò với thầy cô: xưng "con"/"em", gọi "thầy"/"cô"/"giáo sư"; thầy cô gọi học trò là "trò" (nghiêm) hoặc
  "con" (thân, hiền từ).
- Người lớn nói về đám trẻ: "tụi nó", "bọn trẻ"; kể chuyện gọi nhân vật nhỏ tuổi là "thằng bé", "cô bé", "nó".
- Họ hàng: dượng, dì, bác, chú theo vai; người già hiền hậu xưng "ta" gọi "con"/"cháu".
- Kẻ ác, kẻ ngạo mạn: xưng "ta", gọi "ngươi"/"mi"/"mày" tùy mức khinh miệt.
""",
        examples=(
            (
                "“I’m not saying it was a good idea,” said Tom, scratching his ear. “I’m just saying it was the only idea we had.”",
                "“Tớ đâu có nói đó là ý hay,” Tom vừa gãi tai vừa nói. “Tớ chỉ nói đó là ý duy nhất tụi mình có thôi.”",
            ),
            (
                "The old caretaker glared at them as though they were something unpleasant he had found on the bottom of his boot.",
                "Lão gác cổng trừng mắt nhìn tụi nó như thể tụi nó là thứ gì gớm ghiếc lão vừa giẫm phải dưới đế giày.",
            ),
            (
                "“Yeh’ll be wantin’ a cuppa, I ’spect,” said the giant, putting the kettle on.",
                "“Chắc tụi bây muốn làm một tách trà hả,” ông khổng lồ nói, tay bắc ấm nước lên bếp.",
            ),
            (
                "They hurried down Crooked Lane, past the Shop of Improbable Hats.",
                "Tụi nó hối hả đi xuống Ngõ Quẹo, ngang qua Tiệm Nón Khó Tin.",
            ),
        ),
    )
)


def load_style(key: str | None = None, path: Path | None = None) -> StyleProfile:
    """Lấy phong cách có sẵn theo tên, hoặc đọc phong cách tự định nghĩa / tự học (có thể kế thừa `base`) từ file JSON."""
    if path is None:
        key = key or "natural"
        if key not in BUILTIN:
            raise KeyError(f"Không có phong cách dịch '{key}'. Có sẵn: {', '.join(BUILTIN)}")
        return BUILTIN[key]
    data = json.loads(path.read_text(encoding="utf-8"))
    base = BUILTIN.get(data.get("base", "natural"), BUILTIN["natural"])
    fields = asdict(base)
    fields.update({k: v for k, v in data.items() if k in fields and k != "examples"})
    fields["key"] = data.get("key", path.stem)
    examples = [(e.get("source", ""), e["target"]) for e in data.get("examples", [])]
    # mặc định: mẫu của người dùng thêm vào sau mẫu có sẵn; "replace_examples": true để chỉ dùng mẫu riêng
    fields["examples"] = tuple(examples) if data.get("replace_examples") else tuple(base.examples) + tuple(examples)
    return StyleProfile(**fields)
