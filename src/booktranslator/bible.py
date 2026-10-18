"""Sổ tay truyện (story bible): nguồn sự thật duy nhất cho tên riêng, xưng hô và diễn biến.

Quy tắc nhất quán: một tên riêng đã có bản dịch thì bản dịch đó không bao giờ bị
thay tự động. Thông tin mới chỉ được bổ sung (alias, ghi chú). Muốn đổi tên thì
người dùng sửa file JSON.
"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path

BIBLE_VERSION = 1
STORY_SO_FAR_CHARS = 6000
EVENTS_CHARS = 4000


@dataclass
class Entity:
    source: str
    target: str
    type: str = "term"
    aliases: list[str] = field(default_factory=list)
    gender: str = ""
    note: str = ""

    def names(self) -> list[str]:
        return [self.source, *self.aliases]


@dataclass
class Addressing:
    """Cách nhân vật `speaker` xưng mình (`self_term`) và gọi `listener` (`other_term`)."""

    speaker: str
    listener: str
    self_term: str
    other_term: str
    context: str = ""


@dataclass
class SectionNotes:
    title: str = ""
    summary: str = ""
    events: list[str] = field(default_factory=list)


def _norm(name: str) -> str:
    return " ".join(name.split()).casefold()


class StoryBible:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.entities: dict[str, Entity] = {}
        self.addressing: list[Addressing] = []
        self.sections: dict[str, SectionNotes] = {}
        self._alias_index: dict[str, str] = {}
        self._lock = threading.RLock()

    # ---- thực thể ------------------------------------------------------

    def find(self, name: str) -> Entity | None:
        key = self._alias_index.get(_norm(name))
        return self.entities.get(key) if key else None

    def add_entity(self, entity: Entity) -> bool:
        """Thêm thực thể; trả về True nếu là thực thể mới. Không bao giờ đổi `target` đã có."""
        if not entity.source.strip() or not entity.target.strip():
            return False
        with self._lock:
            existing = self.find(entity.source) or next(
                (e for a in entity.aliases if (e := self.find(a))), None
            )
            if existing:
                for alias in [entity.source, *entity.aliases]:
                    if self.find(alias) is None:
                        existing.aliases.append(alias)
                        self._alias_index[_norm(alias)] = _norm(existing.source)
                if not existing.note and entity.note:
                    existing.note = entity.note
                if not existing.gender and entity.gender:
                    existing.gender = entity.gender
                return False
            key = _norm(entity.source)
            entity.aliases = [a for a in dict.fromkeys(entity.aliases) if a.strip() and self.find(a) is None and _norm(a) != key]
            self.entities[key] = entity
            for name in entity.names():
                self._alias_index[_norm(name)] = key
            return True

    def add_addressing(self, item: Addressing) -> bool:
        if not (item.speaker and item.listener and (item.self_term or item.other_term)):
            return False
        with self._lock:
            for a in self.addressing:
                if (_norm(a.speaker), _norm(a.listener), a.self_term, a.other_term) == (
                    _norm(item.speaker), _norm(item.listener), item.self_term, item.other_term
                ):
                    return False
            self.addressing.append(item)
            return True

    def snapshot(self) -> "StoryBible":
        """Bản sao tên riêng và xưng hô, không gắn file (sổ tay tạm khi phân tích song song)."""
        with self._lock:
            copy = StoryBible()
            for e in self.entities.values():
                copy.add_entity(deepcopy(e))
            for a in self.addressing:
                copy.add_addressing(deepcopy(a))
            return copy

    def import_glossary(self, glossary: dict[str, str]) -> None:
        for src, dst in glossary.items():
            self.add_entity(Entity(source=src, target=dst, type="term"))

    def entities_in(self, text: str, contains) -> list[Entity]:
        """Các thực thể có tên (hoặc alias) xuất hiện trong `text`."""
        with self._lock:
            return [e for e in self.entities.values() if any(contains(text, n) for n in e.names())]

    # ---- ghi chú chương ----------------------------------------------

    def set_section(self, section_id: str, notes: SectionNotes) -> None:
        with self._lock:
            self.sections[section_id] = notes

    def story_so_far(self, order: list[str], section_id: str) -> str:
        """Tóm tắt các chương trước `section_id` (gần nhất được ưu tiên) và các sự kiện then chốt."""
        with self._lock:
            idx = order.index(section_id) if section_id in order else len(order)
            previous = [self.sections[s] for s in order[:idx] if s in self.sections]
            summaries: list[str] = []
            size = 0
            for notes in reversed(previous):
                line = f"- {notes.title}: {notes.summary}" if notes.title else f"- {notes.summary}"
                if size + len(line) > STORY_SO_FAR_CHARS:
                    break
                summaries.insert(0, line)
                size += len(line)
            events: list[str] = []
            size = 0
            recent_first = (ev for notes in reversed(previous) for ev in reversed(notes.events))
            for ev in recent_first:
                if size + len(ev) > EVENTS_CHARS:
                    break
                events.insert(0, f"- {ev}")
                size += len(ev)
            parts = []
            if events:
                parts += ["Các sự kiện then chốt đã xảy ra:", *events]
            if summaries:
                parts += ["Tóm tắt các chương trước:", *summaries]
            return "\n".join(parts)

    # ---- prompt --------------------------------------------------------

    def prompt_block(self, track_addressing: bool = True) -> str:
        with self._lock:
            if not self.entities and not self.addressing:
                return "SỔ TAY TRUYỆN: (chưa có mục nào)"
            lines = ["SỔ TAY TRUYỆN — bắt buộc dùng đúng các bản dịch dưới đây, không dùng biến thể khác:"]
            by_type: dict[str, list[Entity]] = {}
            for e in self.entities.values():
                by_type.setdefault(e.type or "term", []).append(e)
            for type_ in sorted(by_type):
                lines.append(f"\n[{type_}]")
                for e in sorted(by_type[type_], key=lambda e: _norm(e.source)):
                    extra = []
                    if e.aliases:
                        extra.append("tên khác: " + ", ".join(e.aliases))
                    if e.gender:
                        extra.append(e.gender)
                    if e.note:
                        extra.append(e.note)
                    suffix = f" ({'; '.join(extra)})" if extra else ""
                    lines.append(f"- {e.source} → {e.target}{suffix}")
            if track_addressing and self.addressing:
                lines.append("\n[xưng hô] (người nói → người nghe: xưng / gọi — hoàn cảnh)")
                for a in self.addressing:
                    ctx = f" — {a.context}" if a.context else ""
                    lines.append(f"- {a.speaker} → {a.listener}: xưng \"{a.self_term}\" / gọi \"{a.other_term}\"{ctx}")
            return "\n".join(lines)

    # ---- lưu / đọc -----------------------------------------------------

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "version": BIBLE_VERSION,
                "entities": [asdict(e) for e in sorted(self.entities.values(), key=lambda e: (e.type, _norm(e.source)))],
                "addressing": [asdict(a) for a in self.addressing],
                "sections": {k: asdict(v) for k, v in self.sections.items()},
            }

    def save(self, path: Path | None = None) -> None:
        path = path or self.path
        if path is None:
            return
        with self._lock:
            data = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(data + "\n", encoding="utf-8")
            os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> "StoryBible":
        bible = cls(path)
        if not path.exists():
            return bible
        data = json.loads(path.read_text(encoding="utf-8"))
        for e in data.get("entities", []):
            bible.add_entity(Entity(**e))
        for a in data.get("addressing", []):
            bible.add_addressing(Addressing(**a))
        for k, v in data.get("sections", {}).items():
            bible.sections[k] = SectionNotes(**v)
        return bible
