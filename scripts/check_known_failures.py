#!/usr/bin/env python3
"""Compare a test run against tests/known_failures.txt.

The fork's macOS dev host always has some expected failures (optional extras not
installed, /tmp symlinks, host-dependent tests that also fail upstream). Treating
"there are failures" as normal is how a dropped fork feature once sat at 21 red
tests for days. This check makes a failure NOT on the list block the merge:

    NEW    failing now, not listed          -> exit 1
    FIXED  listed, but passing in this run  -> warning (exit 1 with --strict)

Usage:
    scripts/run_tests.sh -j 6 --file-timeout 300 > run.log 2>&1
    python scripts/check_known_failures.py run.log

The log is the run_tests.sh output: its per-file failure boxes list
``FAILED <nodeid>`` / ``ERROR <nodeid>``. Node ids are taken as the whole rest of
the line (parametrized ids may contain spaces). Only files that actually RAN in
this log are judged for FIXED, so a partial run doesn't report everything else as
fixed.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIST = REPO_ROOT / "tests" / "known_failures.txt"

_FAIL_RE = re.compile(r"^[\s║]*(?:FAILED|ERROR) (tests/\S+?\.py(?:::.+?)?)(?: - .*)?\s*$")
# Per-file progress lines: "✓ tests/x.py (…)" or "✗ tests/x.py (…)".
_RAN_RE = re.compile(r"[✓✗] (tests/\S+?\.py) \(")


def load_known(path: Path) -> set[str]:
    known = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("  # ", 1)[0].strip()
        if line and not line.startswith("#"):
            known.add(line)
    return known


def parse_log(text: str) -> tuple[set[str], set[str]]:
    """Return (failing node ids, test files that ran)."""
    failing, ran = set(), set()
    for line in text.splitlines():
        if m := _FAIL_RE.match(line):
            failing.add(m.group(1).rstrip())
        for f in _RAN_RE.findall(line):
            ran.add(f)
    return failing, ran


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("log", type=Path, help="run_tests.sh output")
    ap.add_argument("--known", type=Path, default=DEFAULT_LIST)
    ap.add_argument("--strict", action="store_true", help="also fail on FIXED entries")
    args = ap.parse_args(argv)

    known = load_known(args.known)
    failing, ran = parse_log(args.log.read_text(encoding="utf-8", errors="replace"))
    new = sorted(failing - known)
    fixed = sorted(k for k in known - failing if k.split("::", 1)[0] in ran)

    for n in new:
        print(f"NEW    {n}")
    for n in fixed:
        print(f"FIXED  {n}   (delete it from {args.known.name})")
    print(f"\n{len(failing)} failing | {len(failing & known)} known | {len(new)} NEW | {len(fixed)} FIXED")
    if new:
        print("FAIL: new failures above are not on the known list.")
        return 1
    if fixed and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
