"""Summarise the Phase 0 baseline log.

Reads ``~/.hermes/mcp-lazy/cache-baseline.jsonl`` (path overridable
via ``--file``) and prints aggregate cache hit-rate stats so we can
pick the Phase 1 promotion strategy.

Hit-rate denominator (locked in v4 plan, zero-div safe):

    total = cache_read + cache_creation + input_tokens
    rate  = (cache_read / total) if total > 0 else 0.0

Decision rule (from plan):
    rate > 0.60  →  deferred-promotion (mutating tools mid-turn would
                    erase a real cache win)
    rate < 0.30  →  immediate-promotion (cache already cold, low cost)
    in between   →  deferred (conservative default)

Run::

    python -m plugins.mcp_lazy.scripts.cache_report
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean, median


def _iter_rows(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                print(f"warning: line {line_no} not valid JSON; skipping", file=sys.stderr)


def _hit_rate(row: dict) -> float:
    read = int(row.get("cache_read", 0) or 0)
    creation = int(row.get("cache_creation", 0) or 0)
    inp = int(row.get("input_tokens", 0) or 0)
    total = read + creation + inp
    return (read / total) if total > 0 else 0.0


def _strategy(rate: float) -> str:
    if rate > 0.60:
        return "deferred"
    if rate < 0.30:
        return "immediate"
    return "deferred (conservative default — rate in middle band)"


def summarise(path: Path) -> int:
    if not path.exists():
        print(f"no log at {path} — has the plugin been enabled and the gateway run?")
        return 1

    rows = list(_iter_rows(path))
    if not rows:
        print(f"log at {path} is empty")
        return 1

    rates = [_hit_rate(r) for r in rows]
    total_read = sum(int(r.get("cache_read", 0) or 0) for r in rows)
    total_creation = sum(int(r.get("cache_creation", 0) or 0) for r in rows)
    total_input = sum(int(r.get("input_tokens", 0) or 0) for r in rows)
    pooled_total = total_read + total_creation + total_input
    pooled_rate = (total_read / pooled_total) if pooled_total > 0 else 0.0

    print(f"records: {len(rows)}")
    print(f"pooled hit rate:    {pooled_rate:.3f}  ({total_read:,} read / {pooled_total:,} total)")
    print(f"per-request mean:   {mean(rates):.3f}")
    print(f"per-request median: {median(rates):.3f}")
    print(f"cache_read   total: {total_read:,}")
    print(f"cache_create total: {total_creation:,}")
    print(f"input_tokens total: {total_input:,}")
    print(f"strategy:           {_strategy(pooled_rate)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_path = Path.home() / ".hermes" / "mcp-lazy" / "cache-baseline.jsonl"
    parser.add_argument("--file", type=Path, default=default_path, help="path to baseline JSONL")
    args = parser.parse_args(argv)
    return summarise(args.file)


if __name__ == "__main__":
    raise SystemExit(main())
