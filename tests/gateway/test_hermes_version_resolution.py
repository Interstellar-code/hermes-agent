"""`/health` must not report a stale version for a source checkout.

Regression: metadata-first resolution reported 0.18.3 — read from a dist-info
stamped `hermes_agent-0.15.1` — for a checkout actually running 0.19.0, right
after the 0.19 cutover. Installed metadata only refreshes on reinstall, so for
an editable/source checkout it can be arbitrarily stale, while the in-tree
`__version__` is by definition the code that is running.
"""
from pathlib import Path

import hermes_cli
from gateway.platforms.api_server import _hermes_version


def _is_source_checkout() -> bool:
    pkg_dir = Path(hermes_cli.__file__).resolve().parent
    return (pkg_dir.parent / "pyproject.toml").is_file()


def test_source_checkout_reports_the_in_tree_version():
    if not _is_source_checkout():
        return  # installed (non-editable) — metadata is authoritative there
    assert _hermes_version() == hermes_cli.__version__


def test_never_raises_and_always_returns_a_string():
    # A version probe must not be able to break the health endpoint.
    v = _hermes_version()
    assert isinstance(v, str) and v


if __name__ == "__main__":
    test_source_checkout_reports_the_in_tree_version()
    test_never_raises_and_always_returns_a_string()
    print("ok:", _hermes_version())
