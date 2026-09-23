"""`/reasoning show|hide` must be session-scoped by default and only persist to
config.yaml with --global (parity with `/reasoning <level>` and `/fast`).
`/reasoning full|clamp` have no session-scoped store on the CLI or gateway/TUI
surface, so they must always save regardless of --global (see #225/#226 fork
regression: an upstream adopt dropped the `explicit_global` gate and both
toggle families saved unconditionally).
"""

from unittest.mock import patch

from hermes_cli.cli_commands_mixin import CLICommandsMixin


class _Stub(CLICommandsMixin):
    def __init__(self):
        self.reasoning_config = None
        self.show_reasoning = True
        self.reasoning_full = False
        self.agent = None

    def _current_reasoning_callback(self):
        return None


def test_hide_without_global_does_not_save():
    s = _Stub()
    with patch("cli.save_config_value") as mock_save:
        s._handle_reasoning_command("/reasoning hide")
    assert s.show_reasoning is False
    mock_save.assert_not_called()


def test_hide_with_global_saves():
    s = _Stub()
    with patch("cli.save_config_value") as mock_save:
        s._handle_reasoning_command("/reasoning hide --global")
    assert s.show_reasoning is False
    mock_save.assert_called_once_with("display.show_reasoning", False)


def test_full_saves_without_global():
    # reasoning_full has no session-scoped store; it must always save.
    s = _Stub()
    with patch("cli.save_config_value") as mock_save:
        s._handle_reasoning_command("/reasoning full")
    assert s.reasoning_full is True
    mock_save.assert_called_once_with("display.reasoning_full", True)
