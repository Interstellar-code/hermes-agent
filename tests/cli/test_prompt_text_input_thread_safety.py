"""Tests for ``HermesCLI._prompt_text_input`` thread-safe input dispatch.

Raw ``input()`` prompts can race with prompt_toolkit when called from the TUI.
The normal slash confirmations now use a prompt_toolkit-native modal, but
``_prompt_text_input`` remains as a fallback for non-interactive calls and edge
cases.
"""

import threading
from unittest.mock import MagicMock, patch


def _make_cli():
    """Minimal HermesCLI shell exposing prompt fallback helpers."""
    import cli as cli_mod

    obj = object.__new__(cli_mod.HermesCLI)
    obj._app = MagicMock()
    obj._status_bar_visible = True
    return obj


class TestPromptTextInputThreadSafety:
    def test_main_thread_uses_run_in_terminal(self):
        """On the main thread with an active app, route through run_in_terminal."""
        cli = _make_cli()

        with patch("prompt_toolkit.application.run_in_terminal") as mock_rit, \
             patch("builtins.input", return_value="2"):
            cli._prompt_text_input("Choice: ")

        # run_in_terminal was invoked; the _ask closure passed to it would
        # call input() when driven by the event loop.  We assert dispatch path,
        # not the orphaned-coroutine result.
        assert mock_rit.called

    def test_background_thread_cancels_instead_of_hanging(self):
        """On a daemon thread with an active app, cancel cleanly (return None).

        stdin is owned by the prompt_toolkit event loop / JSON-RPC pipe on the
        non-main (process_loop / slash-worker) thread, so a bare input() there
        would block until the worker's timeout (#23185 / billing auto-reload
        hang). The guard cancels to None instead of hanging — it must NOT call
        run_in_terminal (orphaned coroutine) and must NOT call input().
        """
        cli = _make_cli()

        result_holder = {}

        def run_on_daemon():
            with patch("prompt_toolkit.application.run_in_terminal") as mock_rit, \
                 patch("builtins.input", side_effect=AssertionError("input() must not be called off-main-thread")) as mock_input:
                result_holder["value"] = cli._prompt_text_input("Choice [1/2/3]: ")
                result_holder["rit_called"] = mock_rit.called
                result_holder["input_called"] = mock_input.called

        t = threading.Thread(target=run_on_daemon, daemon=True)
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive(), "daemon thread hung — guard did not cancel cleanly"

        # Cancelled cleanly: None returned, neither run_in_terminal nor input() called.
        assert result_holder["value"] is None
        assert result_holder["rit_called"] is False
        assert result_holder["input_called"] is False

    def test_no_app_uses_direct_input(self):
        """Without an active prompt_toolkit app, always call input() directly."""
        cli = _make_cli()
        cli._app = None

        with patch("builtins.input", return_value="cancel") as mock_input:
            result = cli._prompt_text_input("Choice: ")

        assert mock_input.called
        assert result == "cancel"

    def test_run_in_terminal_exception_falls_back(self):
        """If run_in_terminal raises (WSL / Warp edge cases), fall back to input()."""
        cli = _make_cli()

        with patch(
            "prompt_toolkit.application.run_in_terminal",
            side_effect=RuntimeError("event loop dropped the coroutine"),
        ), patch("builtins.input", return_value="3") as mock_input:
            result = cli._prompt_text_input("Choice: ")

        assert mock_input.called
        assert result == "3"

    def test_eof_returns_none(self):
        """EOFError from input() yields None, not an unhandled exception."""
        cli = _make_cli()
        cli._app = None

        with patch("builtins.input", side_effect=EOFError()):
            result = cli._prompt_text_input("Choice: ")

        assert result is None


class TestNonInteractiveConfirmGuard:
    """Issue #220 — the TUI gateway's slash worker has no answerable stdin.

    It runs command handlers on the MAIN thread with ``_app is None``, so the
    #23185 thread guard never fires — but its stdin is the JSON-RPC line
    protocol, so ``input()`` there hangs the worker until the gateway's 45s
    timeout kills it and eats the next request line.  The worker sets
    ``cli._noninteractive_confirm = True``; the prompt helpers must then cancel
    cleanly (``None``) exactly like the thread guard does.
    """

    def test_flag_cancels_instead_of_calling_input(self, capsys):
        cli = _make_cli()
        cli._app = None
        cli._noninteractive_confirm = True

        with patch(
            "builtins.input",
            side_effect=AssertionError("input() must not be called on a non-interactive surface"),
        ) as mock_input:
            result = cli._prompt_text_input("Choice [1/2/3]: ")

        assert result is None
        assert mock_input.called is False
        # The user must see why, not a silent no-op.
        out = capsys.readouterr().out
        assert "aren't available on this surface" in out

    def test_flag_cancels_even_on_main_thread_with_app(self, capsys):
        """The flag wins over the app/main-thread path (no run_in_terminal)."""
        cli = _make_cli()  # _app is a MagicMock
        cli._last_invalidate = 0.0
        cli._noninteractive_confirm = True

        with patch("prompt_toolkit.application.run_in_terminal") as mock_rit, \
             patch("builtins.input", side_effect=AssertionError("input() must not be called")) as mock_input:
            result = cli._prompt_text_input("Choice: ")

        assert result is None
        assert mock_rit.called is False
        assert mock_input.called is False

    def test_modal_cancels_without_input(self, capsys):
        """``_prompt_text_input_modal`` cancels too — both with and without an app."""
        choices = [
            ("once", "Approve Once", "proceed this time only"),
            ("cancel", "Cancel", "keep current conversation"),
        ]

        for app in (None, MagicMock()):
            cli = _make_cli()
            cli._app = app
            cli._last_invalidate = 0.0
            cli._noninteractive_confirm = True

            with patch(
                "builtins.input",
                side_effect=AssertionError("input() must not be called on a non-interactive surface"),
            ) as mock_input:
                result = cli._prompt_text_input_modal(
                    title="⚠️  /new — destroys conversation state",
                    detail="",
                    choices=choices,
                )

            assert result is None
            assert mock_input.called is False
            assert "aren't available on this surface" in capsys.readouterr().out

    def test_flag_defaults_off_so_existing_paths_are_unchanged(self):
        """No flag set (the CLI/test construction path) → direct input()."""
        cli = _make_cli()
        cli._app = None

        assert getattr(cli, "_noninteractive_confirm", False) is False
        with patch("builtins.input", return_value="cancel") as mock_input:
            result = cli._prompt_text_input("Choice: ")

        assert mock_input.called
        assert result == "cancel"

    def test_flag_explicitly_false_still_prompts(self):
        cli = _make_cli()
        cli._app = None
        cli._noninteractive_confirm = False

        with patch("builtins.input", return_value="1") as mock_input:
            result = cli._prompt_text_input("Choice: ")

        assert mock_input.called
        assert result == "1"
