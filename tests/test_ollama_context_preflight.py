"""Tests for Ollama context-window preflight check at gateway startup.

Covers gateway/run._ollama_context_preflight():
  - Under-minimum context → WARNING logged
  - Sufficient context → no WARNING
  - Unreachable server (probe returns None) → DEBUG log, no crash
  - Non-Ollama base_url → skipped silently (no probe)
  - No base_url configured → skipped silently (no probe)
  - Exception during probe → DEBUG log, no crash
"""

import asyncio
import logging
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _config(base_url="http://localhost:11434", model="llama3.1:8b", quiet_mode=False):
    """Return a minimal gateway config dict for the preflight function."""
    cfg: dict = {
        "model": {
            "default": model,
            "base_url": base_url,
        },
    }
    if quiet_mode:
        cfg["agent"] = {"quiet_mode": True}
    return cfg


def _patch_probe(return_value):
    """Patch query_ollama_num_ctx at its source so run_in_executor returns it."""
    return patch(
        "agent.model_metadata.query_ollama_num_ctx",
        return_value=return_value,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOllamaContextPreflight:
    """Unit tests for _ollama_context_preflight."""

    def test_under_minimum_emits_warning(self, caplog):
        """When runtime context < MINIMUM_CONTEXT_LENGTH, a WARNING is logged."""
        from gateway.run import _ollama_context_preflight
        from agent.model_metadata import MINIMUM_CONTEXT_LENGTH

        small_ctx = MINIMUM_CONTEXT_LENGTH - 1  # e.g. 63999

        with _patch_probe(small_ctx):
            with caplog.at_level(logging.WARNING, logger="gateway.run"):
                _run(_ollama_context_preflight(_config()))

        assert any(
            "context window too small" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        ), f"Expected WARNING about context; got: {[r.message for r in caplog.records]}"

    def test_sufficient_context_no_warning(self, caplog):
        """When runtime context >= MINIMUM_CONTEXT_LENGTH, no WARNING is emitted."""
        from gateway.run import _ollama_context_preflight
        from agent.model_metadata import MINIMUM_CONTEXT_LENGTH

        ok_ctx = MINIMUM_CONTEXT_LENGTH  # exactly at the limit

        with _patch_probe(ok_ctx):
            with caplog.at_level(logging.WARNING, logger="gateway.run"):
                _run(_ollama_context_preflight(_config()))

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warnings, f"Unexpected WARNING(s): {[r.message for r in warnings]}"

    def test_unreachable_server_skips_gracefully(self, caplog):
        """When probe returns None (server unreachable), DEBUG is logged, no crash."""
        from gateway.run import _ollama_context_preflight

        with _patch_probe(None):
            with caplog.at_level(logging.DEBUG, logger="gateway.run"):
                _run(_ollama_context_preflight(_config()))  # must not raise

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warnings, f"Unexpected WARNING(s): {[r.message for r in warnings]}"
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("preflight skipped" in m for m in debug_msgs), (
            f"Expected DEBUG 'preflight skipped'; got: {debug_msgs}"
        )

    def test_non_ollama_base_url_skipped(self, caplog):
        """When base_url has no 11434/ollama signal, preflight is skipped entirely."""
        from gateway.run import _ollama_context_preflight

        cfg = _config(base_url="https://api.openai.com/v1")

        with patch("agent.model_metadata.query_ollama_num_ctx") as mock_probe:
            with caplog.at_level(logging.WARNING, logger="gateway.run"):
                _run(_ollama_context_preflight(cfg))

        mock_probe.assert_not_called()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warnings

    def test_no_base_url_skipped(self, caplog):
        """When no base_url is configured, preflight is skipped without side-effects."""
        from gateway.run import _ollama_context_preflight

        cfg: dict = {"model": {"default": "gpt-4o"}}

        with patch("agent.model_metadata.query_ollama_num_ctx") as mock_probe:
            with caplog.at_level(logging.WARNING, logger="gateway.run"):
                _run(_ollama_context_preflight(cfg))

        mock_probe.assert_not_called()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warnings

    def test_exception_in_probe_skips_gracefully(self, caplog):
        """Any exception during probe is caught and logged at DEBUG — never crashes."""
        from gateway.run import _ollama_context_preflight

        with patch(
            "agent.model_metadata.query_ollama_num_ctx",
            side_effect=RuntimeError("connection refused"),
        ):
            with caplog.at_level(logging.DEBUG, logger="gateway.run"):
                _run(_ollama_context_preflight(_config()))  # must not raise

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warnings
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("preflight skipped" in m for m in debug_msgs), (
            f"Expected DEBUG 'preflight skipped'; got: {debug_msgs}"
        )
