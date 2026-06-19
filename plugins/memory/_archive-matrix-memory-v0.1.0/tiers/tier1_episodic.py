from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

ENTRY_DELIMITER = "\n§\n"


class Tier1Store:
    def __init__(self, hermes_home: Path):
        self.hermes_home = Path(hermes_home)
        self.root = self.hermes_home / "memories"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, target: str) -> Path:
        return self.root / ("USER.md" if target == "user" else "MEMORY.md")

    def _read_entries(self, path: Path) -> List[str]:
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        return [part.strip() for part in text.split("§") if part.strip()]

    def _write_entries(self, path: Path, entries: List[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = ENTRY_DELIMITER.join(entry.strip() for entry in entries if entry.strip())
        if rendered:
            rendered += "\n"
        path.write_text(rendered, encoding="utf-8")

    def read_all(self) -> Tuple[List[str], List[str]]:
        return self._read_entries(self._path("memory")), self._read_entries(self._path("user"))

    def add_entry(self, target: str, content: str) -> str:
        normalized = content.strip()
        path = self._path(target)
        entries = self._read_entries(path)
        entries.append(normalized)
        self._write_entries(path, entries)
        return normalized

    def search(self, query: str) -> list[dict]:
        needle = query.lower()
        hits = []
        for target in ("memory", "user"):
            for entry in self._read_entries(self._path(target)):
                if needle in entry.lower():
                    hits.append({"target": target, "entry": entry})
        return hits

    def remove_matching(self, target: str, substring: str) -> int:
        path = self._path(target)
        entries = self._read_entries(path)
        needle = substring.lower()
        kept = [entry for entry in entries if needle not in entry.lower()]
        removed = len(entries) - len(kept)
        self._write_entries(path, kept)
        return removed
