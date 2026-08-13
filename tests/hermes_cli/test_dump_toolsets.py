"""`hermes debug` / `hermes debug share` must render `toolsets` correctly
regardless of whether the raw config value is a comma-separated string or
a list.

Real installs write ``toolsets: hermes-cli,kanban`` (a plain YAML string,
type ``str``) to config.yaml. The dump code used to do a naive
``', '.join(toolsets)`` on that value, which iterates a string
character-by-character instead of splitting on commas, producing garbage
like ``h, e, r, m, e, s, -, c, l, i, ,, k, a, n, b, a, n`` in support
dumps. The fix normalizes with the same helper the rest of the codebase
uses for this shape ambiguity: ``hermes_cli.oneshot._normalize_toolsets``.
"""

from pathlib import Path
from types import SimpleNamespace


def _toolsets_line(out: str) -> str:
    for line in out.splitlines():
        if line.strip().startswith("toolsets:"):
            return line
    raise AssertionError(f"no 'toolsets:' line in dump output:\n{out}")


def _seed(home: Path, *, config_yaml: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(config_yaml)
    (home / ".env").write_text("")


def _run_dump_toolsets_line(monkeypatch, capsys, tmp_path, config_yaml: str) -> str:
    from hermes_cli import dump
    from hermes_cli.config import get_hermes_home

    monkeypatch.setattr(dump, "get_project_root", lambda: tmp_path / "noproject")

    home = get_hermes_home()
    _seed(home, config_yaml=config_yaml)

    dump.run_dump(SimpleNamespace(show_keys=False))
    return _toolsets_line(capsys.readouterr().out)


class TestRunDumpToolsetsLine:
    """End-to-end: seed config.yaml, run the real dump, check the printed line."""

    def test_string_value_renders_as_comma_list_not_chars(self, monkeypatch, capsys, tmp_path):
        line = _run_dump_toolsets_line(
            monkeypatch, capsys, tmp_path, "toolsets: hermes-cli,kanban\n"
        )
        assert "hermes-cli, kanban" in line
        # The historic bug: character-by-character join of the raw string.
        assert "h, e, r, m, e, s" not in line

    def test_list_value_renders_as_comma_list(self, monkeypatch, capsys, tmp_path):
        line = _run_dump_toolsets_line(
            monkeypatch, capsys, tmp_path, "toolsets:\n  - hermes-cli\n  - kanban\n"
        )
        assert "hermes-cli, kanban" in line

    def test_missing_value_falls_back_to_default(self, monkeypatch, capsys, tmp_path):
        # No `toolsets:` key at all in config.yaml.
        line = _run_dump_toolsets_line(monkeypatch, capsys, tmp_path, "model: gpt-4\n")
        assert "hermes-cli" in line

    def test_empty_value_renders_as_default_placeholder(self, monkeypatch, capsys, tmp_path):
        line = _run_dump_toolsets_line(monkeypatch, capsys, tmp_path, "toolsets: []\n")
        assert "(default)" in line

    def test_whitespace_around_commas_is_stripped(self, monkeypatch, capsys, tmp_path):
        line = _run_dump_toolsets_line(
            monkeypatch, capsys, tmp_path, "toolsets: ' hermes-cli , kanban '\n"
        )
        # Each item is trimmed — no stray leading/trailing spaces baked in
        # from the source string (which would otherwise render as
        # " hermes-cli ,  kanban " with doubled/uneven spacing).
        assert "hermes-cli, kanban" in line


class TestConfigOverridesToolsets:
    """Unit tests for `_config_overrides`, which also reads config["toolsets"]
    and used to render it with a raw `str(...)` (giving a Python list-repr
    like "['hermes-cli', 'kanban']" for list configs, and spuriously flagging
    a string value as an "override" of the equivalent default list).
    """

    def test_string_value_matching_default_is_not_an_override(self):
        from hermes_cli.dump import _config_overrides

        overrides = _config_overrides({"toolsets": "hermes-cli"})
        assert "toolsets" not in overrides

    def test_string_value_differing_from_default_is_an_override(self):
        from hermes_cli.dump import _config_overrides

        overrides = _config_overrides({"toolsets": "hermes-cli,kanban"})
        assert overrides["toolsets"] == "hermes-cli, kanban"

    def test_list_value_differing_from_default_renders_without_python_repr(self):
        from hermes_cli.dump import _config_overrides

        overrides = _config_overrides({"toolsets": ["hermes-cli", "kanban"]})
        assert overrides["toolsets"] == "hermes-cli, kanban"
        assert "[" not in overrides["toolsets"]
        assert "'" not in overrides["toolsets"]

    def test_empty_value_is_flagged_with_readable_placeholder(self):
        # Preexisting behavior (unchanged by this fix): an explicit empty
        # toolsets list/string still differs from the non-empty default
        # and is reported as an override — but now with a readable
        # "(none)" placeholder instead of the old str([]) -> "[]" repr.
        from hermes_cli.dump import _config_overrides

        assert _config_overrides({"toolsets": []})["toolsets"] == "(none)"
        assert _config_overrides({"toolsets": ""})["toolsets"] == "(none)"

    def test_missing_value_is_flagged_with_readable_placeholder(self):
        # Same preexisting quirk as above: a bare dict with no "toolsets"
        # key at all (never happens via the real load_config() merge, but
        # _config_overrides() doesn't assume that) also renders as "(none)"
        # rather than the old "[]".
        from hermes_cli.dump import _config_overrides

        assert _config_overrides({})["toolsets"] == "(none)"

    def test_whitespace_around_commas_is_stripped(self):
        from hermes_cli.dump import _config_overrides

        overrides = _config_overrides({"toolsets": " hermes-cli , kanban "})
        assert overrides["toolsets"] == "hermes-cli, kanban"
