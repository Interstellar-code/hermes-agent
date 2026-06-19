from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def append_log_entry(path: Path, action: str, detail: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"- {datetime.now(timezone.utc).isoformat()} [{action}] {detail}\n")
