"""The matrix-memory shim must name the real problem when the submodule is absent.

An UNINITIALIZED git submodule is an EMPTY DIRECTORY, and Path.exists() returns
True for one — so a `_FORK_ROOT.exists()` guard never fires in the exact case it
was written for. Control fell through to `from hermes_memory_provider import ...`
and the operator saw `ModuleNotFoundError: No module named
'hermes_memory_provider'` from inside a third-party import, with no hint that the
fix is one git command.

This is the default state of a fresh `git worktree`, which does not inherit
initialized submodules — so it is the first thing anyone hits on a new checkout.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parents[2] / "plugins" / "memory" / "matrix-memory" / "__init__.py"


def _load_shim():
    spec = importlib.util.spec_from_file_location("mm_shim_under_test", SHIM)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Ctx:
    def register_memory_provider(self, *a, **k):
        raise AssertionError("register_memory_provider reached despite a missing submodule")


def test_empty_submodule_dir_raises_the_actionable_error(tmp_path):
    """tmp_path exists but is empty — exactly an uninitialized submodule."""
    shim = _load_shim()
    shim._FORK_ROOT = tmp_path
    with pytest.raises(RuntimeError) as excinfo:
        shim.register(_Ctx())
    message = str(excinfo.value)
    assert "git submodule update --init" in message, \
        f"error does not tell the operator how to fix it: {message}"


def test_missing_submodule_dir_also_raises(tmp_path):
    shim = _load_shim()
    shim._FORK_ROOT = tmp_path / "does-not-exist"
    with pytest.raises(RuntimeError):
        shim.register(_Ctx())


def test_guard_does_not_use_bare_exists():
    """Pin the cause, not just the symptom: .exists() cannot distinguish an empty
    directory from a populated one, so it must not be what gates this."""
    # Strip comments first: the file explains WHY bare .exists() is wrong, and a
    # naive substring search matches that prose instead of the code.
    code = "\n".join(
        line for line in SHIM.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "if not _FORK_ROOT.exists()" not in code, \
        "bare .exists() is True for an uninitialized submodule's empty directory"
