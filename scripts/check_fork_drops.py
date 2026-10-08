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

  Pass 3 (pinned seams): fork-only core seams that are pure inline logic (no
  new def, no log string) so passes 1-2 cannot see them -- e.g. 9d75ee0504's
  trusted pre_llm_call routing, dropped in the 0.19 migration. Each SEAMS
  entry is a (path, literal) that must exist at --after. Not allowlistable:
  if a seam is intentionally retired, delete its entry here.

Intentional drops go in scripts/fork_drops_allowlist.txt (`name  # reason`,
one per line). Anything left unclassified is reported but NOT allowlisted --
treat it as a possible real drop.

Run: python scripts/check_fork_drops.py --before <pre-adopt-ref> --fork-base <merge-base> [--after HEAD]
     python scripts/check_fork_drops.py --seams-only [--after HEAD|WORKTREE]   # pass 3 only
Exit 1 if any unexplained drop or missing seam remains, 0 otherwise.
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

SUBMODULE_PREFIX = "plugins/memory/_matrix-memory-mnemosyne/"
EXCLUDED_PREFIXES = ("tests/", "plugins/memory/_matrix-memory-mnemosyne/")

DEF_LINE_RE = re.compile(r"(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
CONST_LINE_RE = re.compile(r"([A-Z][A-Z0-9_]*)\s*=")
CALL_STRING_RE = re.compile(
    r"(?:logger\.\w+|logging\.\w+|raise\s+\w*(?:Exception|Error)|print|HTTPException|_set_fatal_error)"
    r"\s*\([^)]*?[\"']([^\"']{28,})[\"']"
)


# (path, literal, why) -- fork-only core seams; a plugin depends on each.
SEAMS = [
    ("agent/turn_context.py", 'r.get("target") in ("system", "developer")',
     "trusted pre_llm_call routing (9d75ee0504; personas overlay -> system prompt)"),
    ("agent/turn_context.py", 'agent._plugin_trusted_context = "\\n\\n".join(_trusted_parts)',
     "trusted pre_llm_call context stashed on the agent by the collector"),
    ("agent/turn_context.py", 'getattr(agent, "_plugin_trusted_context", "")',
     "trusted pre_llm_call context appended to effective_system"),
    ("agent/turn_api_request.py", '"transform_tools"', "transform_tools hook invocation (mcp_lazy)"),
    ("hermes_cli/plugins.py", '"transform_tools"', "transform_tools in VALID_HOOKS (mcp_lazy)"),
    ("agent/usage_pricing.py", "def register_usage_observer(", "usage observer registry (mcp_lazy)"),
    ("agent/usage_pricing.py", "_notify_usage_observers(usage)", "usage observer notify call (mcp_lazy)"),
    ("plugins/memory/_matrix-memory-mnemosyne/hermes_memory_provider/__init__.py",
     "sleep_beam.canonical_owner_id = self._canonical_owner()",
     "Mnemosyne sleep threads inherit canonical owner (model refresh writes to the right owner)"),
    ("plugins/memory/_matrix-memory-mnemosyne/mnemosyne/core/beam.py",
     "beam.canonical_owner_id = self.canonical_owner_id",
     "Mnemosyne cross-session sweep beams inherit canonical owner"),
    ("plugins/memory/_matrix-memory-mnemosyne/mnemosyne/core/beam.py",
     "SLEEP_AGE_HOURS = int(",
     "Mnemosyne sleep age decoupled from WM TTL (TTL=87600 must not stop consolidation)"),
    ("plugins/memory/_matrix-memory-mnemosyne/hermes_memory_provider/__init__.py",
     "self._beam.canonical_hits(",
     "Mnemosyne recall merges canonical facts (plan step 11b)"),
    ("plugins/memory/_matrix-memory-mnemosyne/hermes_memory_provider/__init__.py",
     '_NIGHTLY_META_KEY = "nightly_sleep_last_run"',
     "Mnemosyne nightly consolidation timer (nightly_sleep_enabled)"),
    ("plugins/memory/_matrix-memory-mnemosyne/hermes_memory_provider/__init__.py",
     "def _strip_reply_quotes(",
     "Mnemosyne memory hygiene (skip/strip rules in sync_turn, sleep skip, retention)"),
]


def missing_seams(after: str) -> list[tuple[str, str, str]]:
    missing = []
    for path, text, why in SEAMS:
        ref = [] if after == "WORKTREE" else [after]  # WORKTREE = uncommitted tree
        cwd = REPO
        if path.startswith(SUBMODULE_PREFIX):
            # ponytail: parent git can't grep into a submodule; check its checked-out
            # worktree (ignores --after). Upgrade to `git -C sub grep <gitlink-sha>` if needed.
            cwd, path, ref = REPO / SUBMODULE_PREFIX.rstrip("/"), path[len(SUBMODULE_PREFIX):], []
        r = subprocess.run(["git", "grep", "-q", "-F", text, *ref, "--", path],
                           cwd=cwd, capture_output=True, text=True)
        if r.returncode not in (0, 1):
            raise RuntimeError(f"git grep failed: {r.stderr.strip()}")
        if r.returncode == 1:
            missing.append((path, text, why))
    return missing


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
    ap.add_argument("--before", help="pre-adopt tree ref (required unless --seams-only)")
    ap.add_argument("--after", default="HEAD", help="post-adopt tree ref (default: HEAD)")
    ap.add_argument("--fork-base", help="merge-base with upstream (required unless --seams-only)")
    ap.add_argument("--seams-only", action="store_true", help="only run pass 3 (pinned seams)")
    ap.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    ap.add_argument("--repo", type=Path, default=ROOT, help="repo to inspect (default: this repo)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.seams_only and not (args.before and args.fork_base):
        ap.error("--before and --fork-base are required unless --seams-only")
    if not args.seams_only and args.after == "WORKTREE":
        ap.error("--after WORKTREE is only valid with --seams-only (passes 1-2 need a git ref)")

    global REPO
    REPO = args.repo

    # SEAMS name paths in THIS repo; a throwaway --repo (tests) has none of them.
    seams = missing_seams(args.after) if REPO.resolve() == ROOT else []
    if args.seams_only:
        for path, text, why in seams:
            print(f"missing seam: {path}: {text!r}  ({why})")
        if not seams:
            print(f"All {len(SEAMS)} pinned fork seams present.")
        return 1 if seams else 0

    report = find_drops(args.before, args.after, args.fork_base)
    allowed = load_allowlist(args.allowlist)

    unexplained: dict[str, dict] = {}
    for sha, data in report.items():
        syms = [n for n in data["symbols"] if n not in allowed]
        strs = [s for s in data["strings"] if s not in allowed]
        if syms or strs:
            unexplained[sha] = {"subject": data["subject"], "symbols": syms, "strings": strs}

    if args.json:
        print(json.dumps({"drops": unexplained, "missing_seams": seams}, indent=2))
    else:
        for path, text, why in seams:
            print(f"missing seam: {path}: {text!r}  ({why})")
        if not unexplained:
            print("No unexplained fork drops found.")
        for sha, data in unexplained.items():
            print(f"\n{sha[:10]}  {data['subject']}")
            for n in data["symbols"]:
                print(f"  symbol: {n}")
            for s in data["strings"]:
                print(f"  string: {s}")

    return 1 if unexplained or seams else 0


if __name__ == "__main__":
    sys.exit(main())
