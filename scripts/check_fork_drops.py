#!/usr/bin/env python3
"""Detect fork features silently dropped by an "adopt + replay" upstream upgrade.

An adopt+replay upgrade takes the upstream tree wholesale (`--after`, default
HEAD) and re-ports fork features on top of the pre-adopt tree (`--before`).
Anything a fork commit added between `--fork-base` (merge-base with upstream)
and `--before` that silently failed to survive the replay is a drop this
script should catch, even though no test failed.

Two passes, both scoped to fork commits (`--fork-base..--before`, no merges):

  Pass 1 (symbols): names added by `+def`/`+async def`/`+class`/
  `+UPPER_CONSTANT =` lines in non-test .py files (tests/ and the
  plugins/memory/_matrix-memory-mnemosyne/ submodule are excluded). A name is
  a real drop if it is DEFINED in --before and defined NOWHERE in --after.

  Pass 2 (strings): distinctive literal strings (28+ chars) inside
  logger.*/raise/print/HTTPException/_set_fatal_error calls, added by the
  same fork commits. A string is a real drop if it is present in --before and
  absent from --after. This catches logic dropped from inside a function that
  still exists (so pass 1's "still defined" check would miss it).

Intentional drops go in scripts/fork_drops_allowlist.txt (`name  # reason`,
one per line). Anything left unclassified is reported but NOT allowlisted --
treat it as a possible real drop.

Run: python scripts/check_fork_drops.py --before <pre-adopt-ref> --fork-base <merge-base> [--after HEAD]
Exit 1 if any unexplained drop remains, 0 otherwise.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWLIST = ROOT / "scripts" / "fork_drops_allowlist.txt"
REPO = ROOT  # overridable via --repo, mainly for tests against a throwaway repo

EXCLUDED_PREFIXES = ("tests/", "plugins/memory/_matrix-memory-mnemosyne/")

DEF_LINE_RE = re.compile(r"(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
CONST_LINE_RE = re.compile(r"([A-Z][A-Z0-9_]*)\s*=")
CALL_STRING_RE = re.compile(
    r"(?:logger\.\w+|logging\.\w+|raise\s+\w*(?:Exception|Error)|print|HTTPException|_set_fatal_error)"
    r"\s*\([^)]*?[\"']([^\"']{28,})[\"']"
)


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def is_excluded(path: str) -> bool:
    return path.endswith(".py") is False or any(
        path.startswith(p) or f"/{p}" in path for p in EXCLUDED_PREFIXES
    )


def fork_commits(fork_base: str, before: str) -> list[str]:
    out = git("log", "--no-merges", "--reverse", "--format=%H", f"{fork_base}..{before}")
    return [line for line in out.splitlines() if line]


def commit_subject(sha: str) -> str:
    return git("log", "-1", "--format=%s", sha).strip()


def extract_added(sha: str) -> tuple[dict[str, str], set[str]]:
    """Return ({symbol_name: kind}, {string}) added by this commit's diff."""
    diff = subprocess.run(
        ["git", "show", "--unified=0", "--no-color", sha],
        cwd=REPO, capture_output=True, text=True,
    ).stdout
    symbols: dict[str, str] = {}
    strings: set[str] = set()
    current_file = None
    included = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            current_file = parts[1] if len(parts) == 2 else None
            included = bool(current_file) and not is_excluded(current_file)
            continue
        if not included or not line.startswith("+") or line.startswith("+++"):
            continue
        content = line[1:].lstrip()
        m = DEF_LINE_RE.match(content)
        if m:
            symbols.setdefault(m.group(1), "def")
        else:
            m = CONST_LINE_RE.match(content)
            if m:
                symbols.setdefault(m.group(1), "const")
        for sm in CALL_STRING_RE.finditer(content):
            strings.add(sm.group(1))
    return symbols, strings


def defined_at(ref: str, name: str, kind: str, _cache: dict = {}) -> bool:
    key = (ref, name, kind)
    if key in _cache:
        return _cache[key]
    if kind == "def":
        pattern = rf"(^|[^A-Za-z0-9_])(async[[:space:]]+def|def|class)[[:space:]]+{name}[[:space:]]*[(:]"
    else:
        pattern = rf"(^|[^A-Za-z0-9_]){name}[[:space:]]*="
    r = subprocess.run(
        ["git", "grep", "-I", "-n", "-E", pattern, ref, "--", "*.py"],
        cwd=REPO, capture_output=True, text=True,
    )
    found = False
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            parts = line.split(":", 3)
            if len(parts) < 2:
                continue
            path = parts[1]
            if not any(path.startswith(p) or f"/{p}" in path for p in EXCLUDED_PREFIXES):
                found = True
                break
    elif r.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed: {r.stderr.strip()}")
    _cache[key] = found
    return found


def present_at(ref: str, text: str, _cache: dict = {}) -> bool:
    key = (ref, text)
    if key in _cache:
        return _cache[key]
    r = subprocess.run(
        ["git", "grep", "-q", "-F", text, ref], cwd=REPO, capture_output=True, text=True
    )
    if r.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed: {r.stderr.strip()}")
    found = r.returncode == 0
    _cache[key] = found
    return found


def load_allowlist(path: Path) -> set[str]:
    if not path.exists():
        return set()
    allowed = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.partition("#")[0].strip()
        if name:
            allowed.add(name)
    return allowed


def find_drops(before: str, after: str, fork_base: str) -> dict[str, dict]:
    commits = fork_commits(fork_base, before)
    per_commit_symbols: dict[str, dict[str, str]] = {}
    per_commit_strings: dict[str, set[str]] = {}
    all_symbols: dict[str, str] = {}
    all_strings: set[str] = set()
    for sha in commits:
        symbols, strings = extract_added(sha)
        per_commit_symbols[sha] = symbols
        per_commit_strings[sha] = strings
        all_symbols.update(symbols)
        all_strings.update(strings)

    dropped_symbols = {
        name for name, kind in all_symbols.items()
        if defined_at(before, name, kind) and not defined_at(after, name, kind)
    }
    dropped_strings = {
        s for s in all_strings if present_at(before, s) and not present_at(after, s)
    }

    report: dict[str, dict] = {}
    for sha in commits:
        syms = sorted(n for n in per_commit_symbols[sha] if n in dropped_symbols)
        strs = sorted(s for s in per_commit_strings[sha] if s in dropped_strings)
        if syms or strs:
            report[sha] = {"subject": commit_subject(sha), "symbols": syms, "strings": strs}
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--before", required=True, help="pre-adopt tree ref")
    ap.add_argument("--after", default="HEAD", help="post-adopt tree ref (default: HEAD)")
    ap.add_argument("--fork-base", required=True, help="merge-base with upstream")
    ap.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    ap.add_argument("--repo", type=Path, default=ROOT, help="repo to inspect (default: this repo)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    global REPO
    REPO = args.repo

    report = find_drops(args.before, args.after, args.fork_base)
    allowed = load_allowlist(args.allowlist)

    unexplained: dict[str, dict] = {}
    for sha, data in report.items():
        syms = [n for n in data["symbols"] if n not in allowed]
        strs = [s for s in data["strings"] if s not in allowed]
        if syms or strs:
            unexplained[sha] = {"subject": data["subject"], "symbols": syms, "strings": strs}

    if args.json:
        print(json.dumps(unexplained, indent=2))
    else:
        if not unexplained:
            print("No unexplained fork drops found.")
        for sha, data in unexplained.items():
            print(f"\n{sha[:10]}  {data['subject']}")
            for n in data["symbols"]:
                print(f"  symbol: {n}")
            for s in data["strings"]:
                print(f"  string: {s}")

    return 1 if unexplained else 0


if __name__ == "__main__":
    sys.exit(main())
