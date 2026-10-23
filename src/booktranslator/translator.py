"""Phân tích chương và dịch từng đoạn theo thể loại + phong cách dịch + sổ tay truyện.

Thiết kế tiết kiệm token:
- Prompt hệ thống (quy tắc thể loại, phong cách, đoạn mẫu) cố định cho cả cuốn
  sách → luôn trúng prompt cache của nhà cung cấp.
- Mỗi đoạn chỉ gửi phần sổ tay liên quan tới đoạn đó (tên có mặt + xưng hô của họ).
- Mọi kết quả (phân tích, bản nháp, bản biên tập) được lưu; chạy lại không tốn thêm.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Callable

from .bible import NARRATOR, Addressing, Entity, SectionNotes, StoryBible
from .cache import TranslationCache
from .chunker import split_html, split_text
from .consistency import contains
from .document import Section
from .genres import BUILTIN as BUILTIN_GENRES, GenreProfile
from .providers import ProviderChain
from .styles import BUILTIN as BUILTIN_STYLES, StyleProfile

log = logging.getLogger(__name__)

CONTEXT_TAIL_CHARS = 1200
MIN_SPLIT_CHARS = 400
ANALYSIS_PART_CHARS = 60_000
CACHE_VERSION = "3"

_TRANSLATION_RE = re.compile(r"<translation>\n?(.*?)\n?</translation>", re.DOTALL)
_UPDATES_RE = re.compile(r"<bible_updates>(.*?)</bible_updates>", re.DOTALL)


class TranslationError(RuntimeError):
    pass


@dataclass
class TranslatorConfig:
    target_lang: str = "Tiếng Việt"
    source_lang: str = "tự nhận diện"
    effort: str = "medium"
    analysis_effort: str = "low"
    note: str = ""  # yêu cầu riêng của người dùng, ưu tiên cao nhất
    brief: str = ""  # hướng dẫn dịch riêng cho cuốn/bộ sách (file --huong-dan)
    polish: bool = True  # thêm bước biên tập nhuận sắc sau khi dịch nháp


_FORMAT_RULES = {
    "text": "Văn bản là chữ thuần. Giữ nguyên cách ngắt đoạn và xuống dòng.",
    "markdown": (
        "Văn bản là Markdown. Giữ nguyên toàn bộ cú pháp Markdown (tiêu đề #, danh sách, "
        "in đậm/nghiêng, liên kết, khối code). Không dịch nội dung trong khối code hay URL."
    ),
    "html": (
        "Văn bản là một đoạn XHTML trích từ sách EPUB. Chỉ dịch phần chữ hiển thị và các "
        "thuộc tính alt/title. Giữ nguyên chính xác mọi thẻ, thuộc tính, id, class, href, "
        "thực thể HTML và thứ tự thẻ; không thêm, bớt hay đóng/mở thẻ nào khác với bản gốc."
    ),
}

# Các lỗi "văn dịch" hay gặp khi dịch Anh → Việt. Dùng cho cả bước dịch và bước biên tập.
NATURALNESS_RULES = """Dịch ý, không dịch chữ — tránh "văn dịch", viết như người Việt viết:
- Đọc hiểu cả đoạn rồi mới viết: dịch ý, cảm xúc và dụng ý của câu, không dịch từng chữ theo trật tự tiếng Anh.
- Không lạm dụng "một cách + tính từ": "cười một cách vui vẻ" → "cười tươi", "nói một cách giận dữ" → "gắt".
- Hạn chế bị động "được/bị" khi câu chủ động tự nhiên hơn; "bị" chỉ dùng cho điều không may.
- Lược bớt sở hữu "của anh ấy/của cô ấy/của họ" khi đã rõ ai: "He put his hand in his pocket" → "Anh đút tay vào túi".
- Không lặp "anh ấy/cô ấy/nó" ở mọi câu: lược chủ ngữ, dùng tên, hoặc cách gọi mang sắc thái (gã, lão, cô nàng,
  thằng bé, bà ta, ông cụ...) đúng với thái độ người kể.
- Đại từ người kể dùng cho MỖI nhân vật phải cố định: nếu <so_tay> ghi "Người kể gọi X là ..." thì luôn dùng đúng
  đại từ đó; không đổi qua lại anh ta / hắn / gã / y cho cùng một người, kể cả trong cùng một cảnh.
- Bỏ khung câu tiếng Anh: "It was... that...", "There was a..." → nói thẳng; "Đó là một ngày..." → "Hôm ấy...".
- Sắp xếp lại trật tự câu theo tiếng Việt; tách câu dài nhiều mệnh đề, nhưng giữ cú chốt ở cuối nếu nó gây hiệu ứng.
- Thành ngữ, câu đùa, chơi chữ: dùng thành ngữ, tục ngữ, lối nói dân gian tương đương thay cho nghĩa đen.
- Lời thoại là khẩu ngữ: có tiểu từ tình thái (à, ư, nhỉ, nhé, chứ, đấy, hả, mà, cơ) đúng tính cách và tình huống;
  không để nhân vật nói như đọc văn bản.
- Ưu tiên từ thuần Việt, từ láy, tượng thanh, tượng hình; tránh từ Hán Việt nặng nề khi có từ thường dùng
  (trừ khi văn phong thể loại cần).
- "said" không phải lúc nào cũng là "nói": dùng đáp, hỏi, gắt, thì thầm, lẩm bẩm, càu nhàu... hoặc bỏ hẳn nếu đã rõ."""

_OUTPUT_RULES = """Định dạng trả lời (bắt buộc):
<translation>
...toàn bộ bản dịch...
</translation>
<bible_updates>
{"entities": [{"source": "tên gốc", "target": "bản dịch", "type": "loại", "note": "vai trò ngắn gọn"}],
 "addressing": [{"speaker": "tên gốc", "listener": "tên gốc", "self_term": "xưng", "other_term": "gọi", "context": "hoàn cảnh"}]}
</bible_updates>
Trong bible_updates chỉ ghi tên riêng/thuật ngữ MỚI chưa có trong <so_tay>, và cách xưng hô mới xuất hiện hoặc vừa
thay đổi trong đoạn này (kèm lý do trong context). Nhân vật mới chưa có dòng "Người kể gọi X là ..." thì thêm một mục
addressing với speaker "NGƯỜI KỂ", self_term rỗng, other_term là đại từ người kể dùng cho nhân vật đó.
Không có gì mới thì ghi {"entities": [], "addressing": []}."""


def _is_vietnamese(lang: str) -> bool:
    return lang.strip().casefold() in ("tiếng việt", "vietnamese", "vi")


def _profile_parts(config: TranslatorConfig, genre: GenreProfile, style: StyleProfile) -> list[str]:
    parts = ["", genre.prompt_block()]
    if _is_vietnamese(config.target_lang):
        parts += ["", NATURALNESS_RULES]
    parts += ["", style.prompt_block()]
    if config.brief.strip():
        parts += [
            "",
            "HƯỚNG DẪN DỊCH RIÊNG CHO CUỐN/BỘ SÁCH NÀY (ưu tiên hơn quy tắc thể loại và phong cách khi mâu thuẫn):",
            config.brief.strip(),
        ]
    if config.note:
        parts += ["", "Yêu cầu riêng của người dùng (ưu tiên hơn mọi quy tắc trên):", config.note]
    return parts


def build_rules(config: TranslatorConfig, genre: GenreProfile, kind: str, style: StyleProfile) -> str:
    parts = [
        f"Bạn là một dịch giả văn học hàng đầu, chuyên dịch thể loại {genre.name}. "
        f"Nhiệm vụ: dịch sách từ ngôn ngữ nguồn ({config.source_lang}) sang {config.target_lang}.",
        "",
        "Nguyên tắc chung:",
        "- Dịch đầy đủ, trung thành với ý nghĩa, giọng văn và cảm xúc; không tóm tắt, không bỏ sót câu, "
        "không thêm chú thích hay lời bình của người dịch.",
        f"- Câu văn tự nhiên như được viết thẳng bằng {config.target_lang}.",
        "- NHẤT QUÁN LÀ ƯU TIÊN CAO NHẤT: mọi tên riêng, thuật ngữ, cách xưng hô có trong <so_tay> phải dùng đúng "
        "như vậy ở mọi lần xuất hiện. Diễn biến phải khớp với <story_so_far> (ai còn sống, ai đã biết bí mật gì, "
        "quan hệ đang ở giai đoạn nào, thế giới vận hành ra sao).",
        "- Tên mới chưa có trong <so_tay>: đặt bản dịch theo quy tắc của phong cách và thể loại, rồi khai báo trong bible_updates.",
        "- Nếu một phần đã ở sẵn ngôn ngữ đích, giữ nguyên phần đó.",
        f"- {_FORMAT_RULES[kind]}",
        "- Các phần <so_tay>, <story_so_far>, <current_chapter>, <context> chỉ để tham khảo; không dịch lại chúng.",
        *_profile_parts(config, genre, style),
        "",
        _OUTPUT_RULES,
    ]
    return "\n".join(parts)


def build_editor_rules(config: TranslatorConfig, genre: GenreProfile, kind: str, style: StyleProfile) -> str:
    parts = [
        f"Bạn là biên tập viên văn học kỳ cựu, chuyên nhuận sắc bản dịch {genre.name} sang {config.target_lang}. "
        "Bạn nhận bản gốc và bản dịch nháp của cùng một đoạn.",
        "",
        "Nhiệm vụ: viết lại bản nháp thành bản hoàn chỉnh, đọc như văn chương viết thẳng bằng "
        f"{config.target_lang} bởi một dịch giả hàng đầu, đúng phong cách mô tả dưới đây.",
        "- Đối chiếu từng câu với bản gốc: sửa chỗ hiểu sai, dịch sót, dịch thừa; không thêm hay bớt ý.",
        "- Câu đùa, chơi chữ, ẩn ý phải giữ được tác dụng (buồn cười, gây sợ, gây xúc động) như bản gốc.",
        "- Giọng của từng nhân vật phải phân biệt và ổn định; xưng hô đúng <so_tay> và đúng giai đoạn quan hệ.",
        "- Thống nhất đại từ người kể dùng cho mỗi nhân vật (theo dòng \"Người kể gọi X là ...\" trong <so_tay>); "
        "sửa mọi chỗ đổi qua lại anh ta / hắn / gã cho cùng một người.",
        "- Câu nào nháp dịch tối nghĩa hoặc làm mất ý so với bản gốc thì viết lại cho rõ ý trước, rồi mới trau chuốt.",
        "- GIỮ NGUYÊN mọi tên riêng và thuật ngữ đúng như <so_tay>; không đổi tên dù thấy cách khác hay hơn.",
        f"- {_FORMAT_RULES[kind]}",
        "- Chỗ nào nháp đã tốt thì giữ nguyên; chỉ sửa chỗ thật sự làm văn hay hơn hoặc đúng hơn.",
        *_profile_parts(config, genre, style),
        "",
        "Trả lời duy nhất bằng bản đã biên tập đặt trong cặp thẻ <translation></translation>.",
    ]
    return "\n".join(parts)


def _analysis_schema(genre: GenreProfile) -> dict:
    def obj(props: dict) -> dict:
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}

    s = {"type": "string"}
    return obj(
        {
            "summary": s,
            "events": {"type": "array", "items": s},
            "entities": {
                "type": "array",
                "items": obj(
                    {
                        "source": s,
                        "target": s,
                        "type": {"type": "string", "enum": list(genre.entity_types)},
                        "aliases": {"type": "array", "items": s},
                        "gender": s,
                        "note": s,
                    }
                ),
            },
            "addressing": {
                "type": "array",
                "items": obj({"speaker": s, "listener": s, "self_term": s, "other_term": s, "context": s}),
            },
        }
    )


def parse_json(text: str):
    """Đọc JSON từ câu trả lời; chấp nhận cả khi bị bọc trong ```json``` hoặc có chữ thừa trước/sau."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_FLAT_OBJECT_RE = re.compile(r"\{[^{}]*\}")


def parse_updates(text: str) -> dict | None:
    """Đọc khối <bible_updates>. AI (nhất là DeepSeek/Codex) đôi khi viết JSON hơi lỗi: thừa dấu phẩy, bọc ```json,
    hay một mục có dấu ngoặc kép chưa thoát. Khi đó cứu từng mục còn đọc được thay vì bỏ cả khối."""
    for candidate in (text, _TRAILING_COMMA_RE.sub(r"\1", text)):
        try:
            data = parse_json(candidate)
            return data if isinstance(data, dict) else None
        except ValueError:
            continue
    entities, addressing = [], []
    for m in _FLAT_OBJECT_RE.finditer(text):
        try:
            item = json.loads(_TRAILING_COMMA_RE.sub(r"\1", m.group(0)))
        except ValueError:
            continue
        if isinstance(item, dict) and "speaker" in item:
            addressing.append(item)
        elif isinstance(item, dict) and "source" in item:
            entities.append(item)
    return {"entities": entities, "addressing": addressing} if entities or addressing else None


def _str(value) -> str:
    return value if isinstance(value, str) else ""


class Translator:
    def __init__(
        self,
        config: TranslatorConfig,
        llm: ProviderChain,
        genre: GenreProfile | None = None,
        style: StyleProfile | None = None,
        bible: StoryBible | None = None,
        cache: TranslationCache | None = None,
        analysis_llm: ProviderChain | None = None,
        editor_llm: ProviderChain | None = None,
    ):
        self.config = config
        self.llm = llm  # người dịch nháp
        self.analysis_llm = analysis_llm or llm
        self.editor_llm = editor_llm or llm  # người biên tập (có thể là AI khác, vd. Codex dịch, Claude biên tập)
        self.genre = genre or BUILTIN_GENRES["general"]
        self.style = style or BUILTIN_STYLES["natural"]
        self.bible = bible if bible is not None else StoryBible()
        self.cache = cache if cache is not None else TranslationCache(None)
        # Kết quả đã lưu chỉ dùng lại khi thể loại và phong cách không đổi (kể cả file tự định nghĩa bị sửa).
        self._profile = TranslationCache.make_key(
            json.dumps(self.genre.to_dict(), sort_keys=True, ensure_ascii=False),
            json.dumps(self.style.to_dict(), sort_keys=True, ensure_ascii=False),
            config.brief,
        )

    def fork(self, bible: StoryBible) -> "Translator":
        """Bản sao dùng chung API, cache và cấu hình nhưng ghi vào một sổ tay khác."""
        return Translator(self.config, self.llm, self.genre, self.style, bible, self.cache, self.analysis_llm, self.editor_llm)

    def usage_report(self) -> str:
        chains = []
        for chain in (self.analysis_llm, self.llm, self.editor_llm):
            if all(chain is not c for c in chains):
                chains.append(chain)
        return "\n".join(c.usage_report() for c in chains)

    def _complete(self, llm: ProviderChain, system: str, user: str, what: str, **kw):
        result = llm.complete(system, user, **kw)
        if result.stop_reason == "refusal":
            raise TranslationError(f"Mô hình từ chối {what} (category={result.refusal_category}).")
        return result

    # ---- phân tích chương (lập sổ tay) --------------------------------

    def analyze_section(self, section: Section) -> SectionNotes | None:
        """Đọc một chương, ghi nhân vật/tên riêng/xưng hô/sự kiện vào sổ tay. Kết quả được cache."""
        text = section.plain_text()
        if len(text) < 20:  # trang bìa, trang chỉ có ảnh
            return None
        parts = split_text(text, ANALYSIS_PART_CHARS) if len(text) > ANALYSIS_PART_CHARS else [text]
        results = [self._analyze_text(section.title, p) for p in parts]
        for r in results:
            for e in r.get("entities", []):
                if isinstance(e, dict):
                    aliases = [a for a in e.get("aliases", []) if isinstance(a, str)]
                    self.bible.add_entity(
                        Entity(_str(e.get("source")), _str(e.get("target")), _str(e.get("type")) or "term",
                               aliases, _str(e.get("gender")), _str(e.get("note")))
                    )
            for a in r.get("addressing", []):
                if isinstance(a, dict):
                    self.bible.add_addressing(
                        Addressing(*(_str(a.get(k)) for k in ("speaker", "listener", "self_term", "other_term", "context")))
                    )
        notes = SectionNotes(
            title=section.title,
            summary=" ".join(_str(r.get("summary")) for r in results).strip(),
            events=[ev for r in results for ev in r.get("events", []) if isinstance(ev, str)],
        )
        self.bible.set_section(section.id, notes)
        return notes

    def analysis_rules(self) -> str:
        c, g = self.config, self.genre
        return "\n".join(
            [
                f"Bạn là biên tập viên chuẩn bị dịch một cuốn sách thể loại {g.name} sang {c.target_lang}. "
                "Đọc chương được cung cấp và lập ghi chú cho sổ tay truyện để bản dịch nhất quán về thế giới, "
                "nhân vật, tên riêng và diễn biến.",
                "",
                f"Cần theo dõi: {g.analysis_focus}",
                "",
                "Yêu cầu:",
                f"- summary: tóm tắt diễn biến chương bằng {c.target_lang}, 3–6 câu, dùng tên đã dịch.",
                "- events: sự kiện then chốt ảnh hưởng tới các chương sau (cái chết, bị thương, tiết lộ bí mật, "
                "đổi tên/danh hiệu, thay đổi quan hệ, quy luật thế giới mới lộ ra). Mỗi sự kiện một câu ngắn.",
                "- entities: tên riêng và thuật ngữ đặc thù của thế giới truyện (không lấy từ thông dụng). PHẢI lấy cả tên "
                "loài, chủng tộc, sinh vật, chức danh, đơn vị đặc thù dù viết thường (vd. troll, sea troll, dwarf, imp), "
                "để cả cuốn dùng một cách dịch; source viết thường như trong bản gốc. source là đúng "
                "chữ trong bản gốc; aliases là các cách gọi khác trong bản gốc; target theo quy tắc đặt tên của phong "
                "cách và thể loại. Tên đã có trong <known_names> thì dùng lại đúng bản dịch đó, không cần liệt kê lại "
                "trừ khi có thêm alias.",
                "- gender: giới tính nếu là nhân vật (nam/nữ/không rõ), để trống nếu không phải.",
                "- addressing: cách từng cặp nhân vật xưng hô với nhau trong chương (dùng tên gốc cho speaker/listener), "
                "phù hợp tuổi tác, vai vế, mức thân mật và phong cách dịch. Thêm một mục cho mỗi nhân vật quan trọng với "
                f"speaker = \"{NARRATOR}\", self_term rỗng, other_term = đại từ ngôi thứ ba người kể dùng cho nhân vật đó "
                "(anh, gã, lão, cô, nàng, hắn, bà, ông...) theo tuổi tác và thái độ của giọng kể; đại từ này dùng cố định cả truyện."
                if g.track_addressing
                else "- addressing: để mảng rỗng.",
                "",
                g.prompt_block(),
                "",
                # Phân tích chỉ cần quy tắc đặt tên/xưng hô của phong cách, không cần đoạn mẫu → bớt token.
                f"PHONG CÁCH DỊCH: {self.style.name}\n{self.style.naming.strip()}\n{self.style.addressing.strip()}".strip(),
                *(["", "HƯỚNG DẪN DỊCH RIÊNG CHO CUỐN/BỘ SÁCH NÀY:", c.brief.strip()] if c.brief.strip() else []),
            ]
        )

    def _analyze_text(self, title: str, text: str) -> dict:
        c = self.config
        key = TranslationCache.make_key("analysis", CACHE_VERSION, self.analysis_llm.id, self._profile, c.target_lang, text)
        cached = self.cache.get(key)
        if cached is not None:
            return json.loads(cached)
        known = self.bible.names_index()
        user = (f"<known_names>\n{known}\n</known_names>\n\n" if known else "") + f"<chapter title=\"{title}\">\n{text}\n</chapter>"
        result = self._complete(
            self.analysis_llm, self.analysis_rules(), user, "phân tích chương",
            effort=c.analysis_effort, json_schema=_analysis_schema(self.genre), max_tokens=16000,
        )
        try:
            data = parse_json(result.text)
            if not isinstance(data, dict):
                raise ValueError
        except ValueError as e:  # JSONDecodeError là ValueError
            raise TranslationError(f"Kết quả phân tích chương không hợp lệ ({result.provider}, {result.stop_reason}).") from e
        self.cache.put(key, json.dumps(data, ensure_ascii=False))
        return data

    # ---- dịch ----------------------------------------------------------

    def _cache_key(self, chunk: str, kind: str) -> str:
        c = self.config
        return TranslationCache.make_key(
            "translate", CACHE_VERSION, self.llm.id, c.source_lang, c.target_lang, self._profile, c.note, kind, chunk
        )

    def _build_user_message(
        self,
        chunk: str,
        context: tuple[str, str] | None,
        section_id: str | None,
        order: list[str] | None,
        reminder: str | None,
        draft: str | None = None,
    ) -> str:
        parts = []
        so_tay = self.bible.passage_block(chunk, contains, self.genre.track_addressing)
        if so_tay:
            parts.append(f"<so_tay>\n{so_tay}\n</so_tay>")
        if section_id and order:
            so_far = self.bible.story_so_far(order, section_id)
            if so_far:
                parts.append(f"<story_so_far>\n{so_far}\n</story_so_far>")
            notes = self.bible.sections.get(section_id)
            if notes and notes.summary:
                parts.append(f"<current_chapter>\n{notes.title}: {notes.summary}\n</current_chapter>")
        if context and context[0]:
            src_tail, dst_tail = context
            parts.append(
                "<context>\n"
                f"<previous_source>\n{src_tail}\n</previous_source>\n"
                f"<previous_translation>\n{dst_tail}\n</previous_translation>\n"
                "</context>"
            )
        if reminder:
            parts.append(f"<reminder>\n{reminder}\n</reminder>")
        if draft is None:
            parts.append(f"Dịch đoạn sau:\n<source>\n{chunk}\n</source>")
        else:
            parts.append(f"<source>\n{chunk}\n</source>")
            parts.append(f"Biên tập bản dịch nháp sau:\n<draft>\n{draft}\n</draft>")
        return "\n\n".join(parts)

    def _apply_updates(self, reply: str) -> None:
        match = _UPDATES_RE.search(reply)
        if not match:
            return
        updates = parse_updates(match.group(1))
        if updates is None:
            # Chỉ mất các tên MỚI mà AI định ghi thêm vào sổ tay ở đoạn này; bản dịch vẫn giữ nguyên.
            log.warning("Bỏ qua bible_updates không đọc được (tên mới của đoạn này chưa được ghi vào sổ tay; bản dịch không ảnh hưởng)")
            return
        changed = False
        for e in updates.get("entities", []):
            if isinstance(e, dict):
                changed |= self.bible.add_entity(
                    Entity(_str(e.get("source")), _str(e.get("target")), _str(e.get("type")) or "term", note=_str(e.get("note")))
                )
        for a in updates.get("addressing", []):
            if isinstance(a, dict):
                changed |= self.bible.add_addressing(
                    Addressing(*(_str(a.get(k)) for k in ("speaker", "listener", "self_term", "other_term", "context")))
                )
        if changed:
            self.bible.save()

    def translate_chunk(
        self,
        chunk: str,
        kind: str = "text",
        context: tuple[str, str] | None = None,
        section_id: str | None = None,
        order: list[str] | None = None,
        reminder: str | None = None,
    ) -> str:
        """Dịch nháp, rồi (nếu bật) biên tập nhuận sắc. Trả về bản cuối cùng."""
        draft = self.draft_chunk(chunk, kind, context, section_id, order, reminder)
        if not self.config.polish or not chunk.strip():
            return draft
        return self.polish_chunk(chunk, draft, kind, context, section_id, order)

    def draft_chunk(
        self,
        chunk: str,
        kind: str = "text",
        context: tuple[str, str] | None = None,
        section_id: str | None = None,
        order: list[str] | None = None,
        reminder: str | None = None,
    ) -> str:
        if not chunk.strip():
            return chunk
        key = self._cache_key(chunk, kind)
        if reminder is None:  # dịch lại để sửa lỗi thì bỏ qua cache
            cached = self.cache.get(key)
            if cached is not None:
                return cached

        system = build_rules(self.config, self.genre, kind, self.style)
        result = self._complete(
            self.llm, system, self._build_user_message(chunk, context, section_id, order, reminder),
            "dịch đoạn này", effort=self.config.effort,
        )
        match = _TRANSLATION_RE.search(result.text)
        translation = match.group(1) if match else None

        if translation is None:
            if result.stop_reason != "max_tokens" and result.text.strip() and len(chunk) < MIN_SPLIT_CHARS:
                translation = result.text.strip()  # mô hình quên thẻ, lấy nguyên câu trả lời
            elif len(chunk) < MIN_SPLIT_CHARS:
                raise TranslationError(f"Không dịch được đoạn ngắn ({result.provider}, {result.stop_reason}).")
            else:
                # Bản dịch bị cắt hoặc sai định dạng: chia nhỏ chunk rồi dịch lại.
                log.warning("Chunk %d ký tự bị cắt/sai định dạng, chia đôi để dịch lại", len(chunk))
                splitter = split_html if kind == "html" else split_text
                pieces = splitter(chunk, max(len(chunk) // 2, MIN_SPLIT_CHARS))
                if len(pieces) < 2:
                    raise TranslationError("Không thể chia nhỏ đoạn để dịch lại.")
                out, ctx = [], context
                for piece in pieces:
                    t = self.draft_chunk(piece, kind, ctx, section_id, order, reminder)
                    out.append(t)
                    ctx = (piece[-CONTEXT_TAIL_CHARS:], t[-CONTEXT_TAIL_CHARS:])
                return ("" if kind == "html" else "\n\n").join(out)

        self._apply_updates(result.text)
        self.cache.put(key, translation)
        return translation

    def polish_chunk(
        self,
        chunk: str,
        draft: str,
        kind: str = "text",
        context: tuple[str, str] | None = None,
        section_id: str | None = None,
        order: list[str] | None = None,
    ) -> str:
        """Bước biên tập: đối chiếu bản gốc, viết lại bản nháp cho hay và tự nhiên hơn."""
        key = TranslationCache.make_key("polish", self.editor_llm.id, self._cache_key(chunk, kind), draft)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        system = build_editor_rules(self.config, self.genre, kind, self.style)
        user = self._build_user_message(chunk, context, section_id, order, None, draft=draft)
        try:
            result = self._complete(self.editor_llm, system, user, "biên tập đoạn này", effort=self.config.effort)
        except TranslationError as e:
            log.warning("Bỏ qua bước biên tập cho đoạn này: %s", e)
            return draft
        match = _TRANSLATION_RE.search(result.text)
        if not match:
            log.warning("Bản biên tập không đúng định dạng (%s, %s), giữ bản nháp", result.provider, result.stop_reason)
            return draft
        self.cache.put(key, match.group(1))
        return match.group(1)

    def fix_names(self, chunk: str, translation: str, kind: str, names: list[tuple[str, str]]) -> str:
        """Soát tên: chỉ thay các tên dịch sai / biến thể / còn sót tên gốc bằng đúng bản dịch trong sổ tay.

        Rẻ và an toàn hơn dịch lại cả đoạn: câu chữ đã biên tập được giữ nguyên.
        """
        listing = "\n".join(f"- {src} → {dst}" for src, dst in names)
        key = TranslationCache.make_key("fixnames", CACHE_VERSION, self.editor_llm.id, kind, chunk, translation, listing)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        system = "\n".join([
            f"Bạn là biên tập viên soát tên riêng cho bản dịch {self.config.target_lang}.",
            "Nhiệm vụ DUY NHẤT: sửa bản dịch để mọi tên trong <names> dùng đúng bản dịch bắt buộc ở mọi chỗ xuất hiện "
            "(kể cả số nhiều, đầu câu, cách gọi tắt): thay các cách dịch khác, biến thể đảo chữ, hoặc tên gốc tiếng Anh "
            "còn sót bằng đúng bản dịch bắt buộc; viết hoa như trong danh sách.",
            "Thay TRỌN cả cụm dịch sai, không để thừa chữ nào của cách dịch cũ: vd. bắt buộc \"troll → Quỷ Đá\" thì "
            "\"quỷ lùn\", \"quỷ cầu\", \"con troll\" đều thành \"Quỷ Đá\" (không phải \"Quỷ Đá lùn\").",
            "KHÔNG sửa bất cứ thứ gì khác: giữ nguyên câu chữ, giọng văn, dấu câu, xuống dòng. Nếu một tên được thay bằng "
            "đại từ hợp lý thì để nguyên.",
            f"- {_FORMAT_RULES[kind]}",
            "Trả lời duy nhất bằng bản đã sửa đặt trong cặp thẻ <translation></translation>.",
        ])
        user = (f"<names>\n{listing}\n</names>\n\n<source>\n{chunk}\n</source>\n\n"
                f"Soát tên trong bản dịch sau:\n<translation_to_fix>\n{translation}\n</translation_to_fix>")
        try:
            result = self._complete(self.editor_llm, system, user, "soát tên", effort="low")
        except TranslationError as e:
            log.warning("Bỏ qua bước soát tên cho đoạn này: %s", e)
            return translation
        match = _TRANSLATION_RE.search(result.text)
        fixed = match.group(1) if match else ""
        # Chỉ được thay tên: độ dài phải gần như không đổi, nếu không thì giữ bản cũ cho an toàn.
        if not fixed.strip() or abs(len(fixed) - len(translation)) > max(0.15 * len(translation), 40):
            log.warning("Bản soát tên thay đổi quá nhiều (%s), giữ bản cũ", result.provider)
            return translation
        self.cache.put(key, fixed)
        return fixed

    def translate_section(
        self,
        section: Section,
        order: list[str] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[str]:
        """Dịch tuần tự các đoạn của một chương, mỗi đoạn nhận đuôi đoạn trước làm ngữ cảnh."""
        out: list[str] = []
        context: tuple[str, str] | None = None
        for i, chunk in enumerate(section.chunks):
            translated = self.translate_chunk(chunk, section.kind, context, section.id, order)
            out.append(translated)
            context = (chunk[-CONTEXT_TAIL_CHARS:], translated[-CONTEXT_TAIL_CHARS:])
            if on_progress:
                on_progress(i + 1, len(section.chunks))
        return out
