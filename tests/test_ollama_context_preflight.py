"""Tests for Ollama context-window preflight check at gateway startup."""

import asyncio
import logging
from unittest.mock import patch


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _config(base_url="http://localhost:11434", model="llama3.1:8b", quiet_mode=False):
    cfg: dict = {"model": {"default": model, "base_url": base_url}}
    if quiet_mode:
        cfg["agent"] = {"quiet_mode": True}
    return cfg


def _patch_config(cfg):
    return patch("hermes_cli.config.load_config", return_value=cfg)


def _patch_probe(return_value=None, side_effect=None):
    if side_effect is not None:
        return patch("agent.model_metadata.query_ollama_num_ctx", side_effect=side_effect)
    return patch("agent.model_metadata.query_ollama_num_ctx", return_value=return_value)


class TestOllamaContextPreflight:
    def test_under_minimum_emits_warning(self, caplog):
        from gateway.run import _ollama_context_preflight
        from agent.model_metadata import MINIMUM_CONTEXT_LENGTH

        with _patch_config(_config()), _patch_probe(MINIMUM_CONTEXT_LENGTH - 1):
            with caplog.at_level(logging.WARNING, logger="gateway.run"):
                _run(_ollama_context_preflight())

        assert any(
            "context window too small" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_sufficient_context_no_warning(self, caplog):
        from gateway.run import _ollama_context_preflight
        from agent.model_metadata import MINIMUM_CONTEXT_LENGTH

        with _patch_config(_config()), _patch_probe(MINIMUM_CONTEXT_LENGTH):
            with caplog.at_level(logging.WARNING, logger="gateway.run"):
                _run(_ollama_context_preflight())

        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_unreachable_server_skips_gracefully(self, caplog):
        from gateway.run import _ollama_context_preflight

        with _patch_config(_config()), _patch_probe(None):
            with caplog.at_level(logging.DEBUG, logger="gateway.run"):
                _run(_ollama_context_preflight())

        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("preflight skipped" in r.message for r in caplog.records if r.levelno == logging.DEBUG)

    def test_non_ollama_base_url_skipped(self, caplog):
        from gateway.run import _ollama_context_preflight

        with _patch_config(_config(base_url="https://api.openai.com/v1")):
            with patch("agent.model_metadata.query_ollama_num_ctx") as mock_probe:
                with caplog.at_level(logging.WARNING, logger="gateway.run"):
                    _run(_ollama_context_preflight())

        mock_probe.assert_not_called()
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_no_base_url_skipped(self, caplog):
        from gateway.run import _ollama_context_preflight

        with _patch_config({"model": {"default": "gpt-4o"}}):
            with patch("agent.model_metadata.query_ollama_num_ctx") as mock_probe:
                with caplog.at_level(logging.WARNING, logger="gateway.run"):
                    _run(_ollama_context_preflight())

        mock_probe.assert_not_called()
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_quiet_mode_skipped(self, caplog):
        from gateway.run import _ollama_context_preflight

        with _patch_config(_config(quiet_mode=True)):
            with patch("agent.model_metadata.query_ollama_num_ctx") as mock_probe:
                with caplog.at_level(logging.DEBUG, logger="gateway.run"):
                    _run(_ollama_context_preflight())

        mock_probe.assert_not_called()
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_exception_in_probe_skips_gracefully(self, caplog):
        from gateway.run import _ollama_context_preflight

        with _patch_config(_config()), _patch_probe(side_effect=RuntimeError("connection refused")):
            with caplog.at_level(logging.DEBUG, logger="gateway.run"):
                _run(_ollama_context_preflight())

        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("preflight skipped" in r.message for r in caplog.records if r.levelno == logging.DEBUG)
