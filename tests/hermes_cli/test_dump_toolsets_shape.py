"""`hermes dump` must handle both shapes of the `toolsets` config value.

config.yaml may carry toolsets as a comma-separated STRING ("hermes-cli,kanban")
or a LIST (["hermes-cli", "kanban"]). Two bugs followed from assuming a list:

  * `', '.join(value)` on a string iterates characters, so the dump rendered
    "h, e, r, m, e, s, -, c, l, i, ,, k, a, n, b, a, n".
  * comparing the raw value against the list-shaped default reported the string
    "hermes-cli" as an override of ["hermes-cli"].

A dump is a diagnostic artifact people paste into bug reports, so a wrong one
sends the reader chasing a config problem that does not exist.
"""

from __future__ import annotations

import pytest

from hermes_cli.dump import _config_overrides


@pytest.mark.parametrize("value", ["hermes-cli,kanban", ["hermes-cli", "kanban"]])
def test_both_shapes_render_identically(value):
    assert _config_overrides({"toolsets": value})["toolsets"] == "hermes-cli, kanban"


def test_no_character_splitting():
    rendered = _config_overrides({"toolsets": "hermes-cli,kanban"})["toolsets"]
    assert "h, e, r" not in rendered, f"string was iterated as characters: {rendered!r}"


def test_string_equal_to_list_default_is_not_an_override():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    default = DEFAULT_CONFIG.get("toolsets")
    if not default:
        pytest.skip("no default toolsets to compare against")
    as_string = ",".join(default) if isinstance(default, (list, tuple)) else str(default)
    assert _config_overrides({"toolsets": as_string}).get("toolsets") is None


def test_whitespace_is_trimmed():
    assert _config_overrides({"toolsets": "hermes-cli , kanban"})["toolsets"] == "hermes-cli, kanban"
