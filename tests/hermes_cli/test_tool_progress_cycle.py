"""The /tool-progress cycle must cover every value display.tool_progress accepts.

A mode the normaliser accepts but the cycle omits is unreachable: it can never
be selected, and a user who set it in config and pressed the toggle once can
never get back to it (#222). "log" was omitted that way.

"log" is a gateway-only rendering mode -- the tool_calls.log writer lives in the
messaging gateway, so in the CLI it simply means quiet. That is a reason to give
it a quiet label, not a reason to drop it from the cycle.
"""

from __future__ import annotations

from hermes_cli.cli_info_mixin import _TOOL_PROGRESS_CYCLE, _TOOL_PROGRESS_LABELS

# The set gateway/display_config.py::_NORMALISERS accepts for display.tool_progress.
ACCEPTED_MODES = {"off", "new", "all", "verbose", "log"}


def test_cycle_covers_every_accepted_mode():
    missing = ACCEPTED_MODES - set(_TOOL_PROGRESS_CYCLE)
    assert not missing, f"unreachable tool_progress modes: {sorted(missing)}"


def test_every_cycle_entry_has_a_label():
    """A cycle entry with no label prints an empty line on toggle."""
    missing = set(_TOOL_PROGRESS_CYCLE) - set(_TOOL_PROGRESS_LABELS)
    assert not missing, f"cycle modes with no label: {sorted(missing)}"


def test_cycle_has_no_duplicates():
    assert len(_TOOL_PROGRESS_CYCLE) == len(set(_TOOL_PROGRESS_CYCLE))


def test_every_mode_survives_a_full_round_trip():
    """Starting anywhere, len(cycle) steps must return to the start."""
    n = len(_TOOL_PROGRESS_CYCLE)
    for mode in _TOOL_PROGRESS_CYCLE:
        idx = _TOOL_PROGRESS_CYCLE.index(mode)
        for _ in range(n):
            idx = (idx + 1) % n
        assert _TOOL_PROGRESS_CYCLE[idx] == mode


def test_normaliser_and_cycle_agree():
    """Pin the two together so adding a mode to one side fails loudly."""
    from gateway.display_config import _NORMALISERS
    normalise = _NORMALISERS["tool_progress"]
    for mode in _TOOL_PROGRESS_CYCLE:
        assert normalise(mode) == mode, f"{mode!r} is in the cycle but the normaliser rewrites it"
