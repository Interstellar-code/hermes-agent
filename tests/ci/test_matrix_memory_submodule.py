"""Regression guard for #157: the matrix-memory Mnemosyne engine must stay
reproducibly vendored as a committed git submodule.

The bug was a *half-committed* submodule — the engine was wired locally
(`.git/config` + `.git/modules`) but `.gitmodules` was never committed AND the
worktree path was gitignored, so fresh clones / CI got nothing and the
`mnemosyne_*` provider silently failed to register.

These checks fail loudly if either half of that regression reappears.
"""
from __future__ import annotations

import configparser
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SUBMODULE_PATH = "plugins/memory/_matrix-memory-mnemosyne"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def test_gitmodules_declares_matrix_memory():
    """.gitmodules must exist and declare the Mnemosyne submodule path+url."""
    gitmodules = REPO_ROOT / ".gitmodules"
    assert gitmodules.exists(), ".gitmodules missing — Mnemosyne submodule not committed (#157)"

    cp = configparser.ConfigParser()
    cp.read(gitmodules)
    paths = {cp.get(s, "path", fallback=None) for s in cp.sections()}
    assert SUBMODULE_PATH in paths, f"{SUBMODULE_PATH} not declared in .gitmodules (#157)"

    section = next(s for s in cp.sections() if cp.get(s, "path", fallback=None) == SUBMODULE_PATH)
    url = cp.get(section, "url", fallback="")
    assert url.startswith("http"), f"submodule url must be a fetchable remote, got {url!r} (#157)"


def test_submodule_path_is_a_committed_gitlink():
    """The path must be tracked as a gitlink (mode 160000), not loose files/absent."""
    out = _git("ls-files", "--stage", SUBMODULE_PATH).strip()
    assert out, f"{SUBMODULE_PATH} is not tracked at all — engine unreachable on clone (#157)"
    assert out.split()[0] == "160000", f"{SUBMODULE_PATH} must be a gitlink (160000), got: {out!r}"


def test_submodule_path_not_gitignored():
    """The `.gitignore:163` line that hid the submodule must stay gone."""
    # git check-ignore exits 0 when the path IS ignored — that is the failure here.
    res = subprocess.run(
        ["git", "check-ignore", SUBMODULE_PATH],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert res.returncode != 0, (
        f"{SUBMODULE_PATH} is gitignored — this is the #157 saboteur, remove the "
        f".gitignore rule (matched: {res.stdout.strip()!r})"
    )
