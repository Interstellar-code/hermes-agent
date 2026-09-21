"""Confirm prompts must cancel, not block, on a surface with no terminal (#220).

The TUI gateway's slash_worker subprocess runs HermesCLI with stdin bound to the
framed JSON-RPC line protocol and no prompt_toolkit app. The pre-existing
slash-worker guard only fires when `self._app` is truthy AND we are off the main
thread -- the worker is neither, so a confirm-prompting command fell through to
a bare input() that read the protocol pipe: the worker hung until the gateway's
timeout killed it, and the read consumed the next request line.
"""

from __future__ import annotations

import pytest

from hermes_cli.cli_modal_mixin import CLIModalMixin


class _Surface(CLIModalMixin):
    """Minimal stand-in: no app, main thread, stdin that must never be read."""

    def __init__(self, noninteractive: bool):
        self._app = None
        self._invalidated = False
        if noninteractive:
            self._noninteractive_confirm = True

    def _invalidate(self, min_interval: float = 0.25) -> None:
        self._invalidated = True


CHOICES = [("yes", "Yes", "do it"), ("no", "No", "skip")]


def _explode(*_a, **_k):
    raise AssertionError("input() was called on a non-interactive surface")


def test_plain_prompt_cancels_without_reading_stdin(monkeypatch):
    monkeypatch.setattr("builtins.input", _explode)
    assert _Surface(noninteractive=True)._prompt_text_input("Choice: ") is None


def test_modal_prompt_cancels_without_reading_stdin(monkeypatch):
    monkeypatch.setattr("builtins.input", _explode)
    surface = _Surface(noninteractive=True)
    assert surface._prompt_text_input_modal(
        title="t", detail="d", choices=CHOICES) is None


def test_cancel_invalidates_the_display(monkeypatch):
    monkeypatch.setattr("builtins.input", _explode)
    surface = _Surface(noninteractive=True)
    surface._prompt_text_input("Choice: ")
    assert surface._invalidated, "cancel must repaint; a stale prompt line would linger"


def test_flag_absent_keeps_existing_behaviour(monkeypatch):
    """getattr default: every other construction path is untouched."""
    calls = []
    monkeypatch.setattr("builtins.input", lambda *a, **k: calls.append(a) or "answer")
    assert _Surface(noninteractive=False)._prompt_text_input("Choice: ") == "answer"
    assert calls, "the normal path must still reach input()"


def test_worker_sets_the_flag():
    """The guard is inert unless slash_worker actually arms it."""
    import inspect
    from tui_gateway import slash_worker
    assert "_noninteractive_confirm = True" in inspect.getsource(slash_worker.main)
