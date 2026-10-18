"""Lưu bản dịch từng chunk xuống đĩa để chạy lại không phải trả tiền dịch lại."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path


class TranslationCache:
    """Kho key-value dạng JSON lines; mỗi dòng là một chunk đã dịch xong."""

    def __init__(self, path: Path | None, load: bool = True):
        self.path = path
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()
        if load and path and path.exists():
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # dòng cuối bị cắt dở khi tiến trình bị dừng
                    self._data[entry["key"]] = entry["value"]

    @staticmethod
    def make_key(*parts: str) -> str:
        h = hashlib.sha256()
        for part in parts:
            h.update(part.encode("utf-8"))
            h.update(b"\0")
        return h.hexdigest()

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._data[key] = value
            if self.path is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, "value": value}, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def __len__(self) -> int:
        return len(self._data)
