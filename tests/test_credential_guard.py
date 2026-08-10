"""The autouse credential guard in conftest.py must actually be armed.

``_block_real_copilot_credentials`` stops the suite reading a developer's real
GitHub credential via ``gh auth token`` and sending it to GitHub's Copilot
token-exchange endpoint. That leak was silent for a long time because
``get_copilot_api_token`` swallows every exception, so a guard that quietly
stopped working would be equally silent. These tests fail loudly if it does.
"""

from __future__ import annotations

import urllib.request

import pytest


def test_gh_cli_token_shellout_is_blocked_by_default():
    """Without opting in, the real `gh auth token` path must be unreachable."""
    from hermes_cli import copilot_auth

    with pytest.raises(RuntimeError, match="credential guard"):
        copilot_auth._try_gh_cli_token()


def test_resolve_copilot_token_cannot_reach_the_gh_cli():
    """The guard must hold through the public entry point, not just the helper.

    ``resolve_copilot_token`` checks env vars first and only then falls back to
    the CLI; ``_hermetic_environment`` blanks those env vars, so this exercises
    the fallback branch that the env-var scrubbing does not cover.
    """
    from hermes_cli import copilot_auth

    with pytest.raises(RuntimeError, match="credential guard"):
        copilot_auth.resolve_copilot_token()


def test_copilot_token_exchange_endpoint_is_blocked():
    """A placeholder token must not be able to trigger a live exchange."""
    from hermes_cli import copilot_auth

    with pytest.raises(Exception) as excinfo:
        copilot_auth.exchange_copilot_token("gho_placeholder_not_a_real_token")

    assert "credential guard" in str(excinfo.value)


def test_guard_does_not_block_unrelated_urlopen_traffic():
    """The URL guard is deliberately narrow — it must not be a blanket block.

    Only the Copilot OAuth/exchange paths are blocked; other urllib callers in
    the codebase must be unaffected. A blanket host block would silently change
    behavior for unrelated code.
    """
    req = urllib.request.Request("https://api.github.com/repos/octocat/hello-world")

    # Reaching the network guard is what we are testing, not the response —
    # so a connection error is a pass and the guard's RuntimeError is a fail.
    try:
        urllib.request.urlopen(req, timeout=0.01)
    except RuntimeError as exc:  # pragma: no cover - only on regression
        if "credential guard" in str(exc):
            pytest.fail(
                "credential guard blocked an unrelated github.com URL; it is "
                "meant to match only the Copilot OAuth/exchange paths"
            )
        raise
    except Exception:
        pass  # network error / timeout — the guard let it through, as intended


def test_opt_out_fixture_restores_the_real_function(real_try_gh_cli_token):
    """The documented escape hatch must actually hand back the real callable."""
    from hermes_cli import copilot_auth

    assert copilot_auth._try_gh_cli_token is real_try_gh_cli_token
    assert real_try_gh_cli_token.__name__ == "_try_gh_cli_token"
