"""
test_propose_wiring.py — Tests for _wiring.py and POST /propose model wiring.

Covers:
- resolve_propose_kwargs passes proposer_model/judge_model into propose_for_profile
- equal proposer==judge models → clean ValueError (not 500)
- judge_fn boolean parser: yes/no/true/false/ambiguous
- POST /propose passes models to propose_for_profile (monkeypatched, no real LLM calls)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure plugin directory is on path (conftest does this too, but be explicit).
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


# ---------------------------------------------------------------------------
# _parse_verdict tests (judge_fn boolean parser)
# ---------------------------------------------------------------------------

def test_parse_verdict_yes():
    from _wiring import _parse_verdict
    assert _parse_verdict("yes") is True
    assert _parse_verdict("YES") is True
    assert _parse_verdict("yes, definitely") is True


def test_parse_verdict_no():
    from _wiring import _parse_verdict
    assert _parse_verdict("no") is False
    assert _parse_verdict("NO") is False
    assert _parse_verdict("no, it doesn't") is False


def test_parse_verdict_true_false():
    from _wiring import _parse_verdict
    assert _parse_verdict("true") is True
    assert _parse_verdict("false") is False
    assert _parse_verdict("True") is True
    assert _parse_verdict("False") is False


def test_parse_verdict_pass_fail():
    from _wiring import _parse_verdict
    assert _parse_verdict("pass") is True
    assert _parse_verdict("fail") is False


def test_parse_verdict_ambiguous_defaults_false():
    from _wiring import _parse_verdict
    assert _parse_verdict("") is False
    assert _parse_verdict("maybe") is False
    assert _parse_verdict("it depends") is False
    assert _parse_verdict("unclear response from model") is False


# ---------------------------------------------------------------------------
# resolve_propose_kwargs — equal models → ValueError
# ---------------------------------------------------------------------------

def test_resolve_propose_kwargs_equal_models_raises():
    from _wiring import resolve_propose_kwargs
    with patch("_wiring._load_models", return_value=("gpt-5.4", "gpt-5.4")):
        with pytest.raises(ValueError, match="must differ"):
            resolve_propose_kwargs()


def test_resolve_propose_kwargs_distinct_models_ok():
    from _wiring import resolve_propose_kwargs
    with patch("_wiring._load_models", return_value=("auto", "gpt-5.4")):
        kwargs = resolve_propose_kwargs()
    assert kwargs["proposer_model"] == "auto"
    assert kwargs["judge_model"] == "gpt-5.4"
    assert callable(kwargs["llm_fn"])
    assert callable(kwargs["judge_fn"])
    assert callable(kwargs["scenario_runner"])


# ---------------------------------------------------------------------------
# POST /propose — model kwargs flow into propose_for_profile
# ---------------------------------------------------------------------------

def _make_app():
    """Import the FastAPI router and wrap it in a TestClient."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _dashboard_dir = _PLUGIN_DIR / "dashboard"
    if str(_dashboard_dir) not in sys.path:
        sys.path.insert(0, str(_dashboard_dir))

    import importlib
    import plugin_api as pa
    # Re-import to pick up fresh state.
    pa = importlib.import_module("plugin_api")
    app = FastAPI()
    app.include_router(pa.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def _mock_propose_for_profile():
    """Monkeypatch propose_for_profile to capture kwargs without real LLM calls."""
    captured = {}

    import _proposer as prop_mod

    from dataclasses import dataclass, field

    @dataclass
    class FakeResult:
        ok: bool = True
        skipped: bool = False
        skip_reason: str = ""
        experiment_id: str = "exp-test-1"
        offline_score: float = 1.0
        error: str = ""

    def fake_propose(db, profile, target_relpath, profile_root, **kwargs):
        captured.update(kwargs)
        return FakeResult()

    with patch.object(prop_mod, "propose_for_profile", side_effect=fake_propose):
        yield captured


def _make_test_app(tmp_path):
    """Build a FastAPI TestClient with dashboard on sys.path."""
    _dashboard_dir = _PLUGIN_DIR / "dashboard"
    if str(_dashboard_dir) not in sys.path:
        sys.path.insert(0, str(_dashboard_dir))

    import importlib
    if "plugin_api" in sys.modules:
        pa = sys.modules["plugin_api"]
    else:
        pa = importlib.import_module("plugin_api")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(pa.router)
    return TestClient(app, raise_server_exceptions=False)


def test_post_propose_passes_models_to_propose_for_profile(_mock_propose_for_profile, tmp_path):
    """POST /propose should pass proposer_model and judge_model to propose_for_profile."""
    captured = _mock_propose_for_profile

    # Ensure dashboard is importable before any patching.
    _dashboard_dir = _PLUGIN_DIR / "dashboard"
    if str(_dashboard_dir) not in sys.path:
        sys.path.insert(0, str(_dashboard_dir))

    fake_kwargs = {
        "proposer_model": "auto",
        "judge_model": "gpt-5.4",
        "llm_fn": lambda p: "ok",
        "judge_fn": lambda r, s: True,
        "scenario_runner": lambda i: "ok",
    }

    profile_root = tmp_path / "ksi-demo"
    profile_root.mkdir()
    (profile_root / "system_prompt.md").write_text("You are helpful.\n")

    import _db as db_mod
    mock_db = MagicMock()
    mock_db.list_experiments.return_value = []

    with patch("_wiring.resolve_propose_kwargs", return_value=fake_kwargs), \
         patch.object(db_mod, "get_db", return_value=mock_db), \
         patch("hermes_cli.profiles.get_profile_dir", return_value=str(profile_root)):
        client = _make_test_app(tmp_path)
        resp = client.post(
            "/propose",
            json={"profile": "ksi-demo"},
            headers={"X-KSI-Auth": ""},
        )

    assert captured.get("proposer_model") == "auto", f"captured={captured}, status={resp.status_code}, body={resp.text}"
    assert captured.get("judge_model") == "gpt-5.4", f"captured={captured}"


def test_post_propose_returns_400_on_equal_models(tmp_path):
    """POST /propose should return 400 when proposer_model == judge_model."""
    _dashboard_dir = _PLUGIN_DIR / "dashboard"
    if str(_dashboard_dir) not in sys.path:
        sys.path.insert(0, str(_dashboard_dir))

    profile_root = tmp_path / "ksi-demo"
    profile_root.mkdir()
    (profile_root / "system_prompt.md").write_text("You are helpful.\n")

    import _db as db_mod
    mock_db = MagicMock()
    mock_db.list_experiments.return_value = []

    with patch("_wiring._load_models", return_value=("gpt-5.4", "gpt-5.4")), \
         patch.object(db_mod, "get_db", return_value=mock_db), \
         patch("hermes_cli.profiles.get_profile_dir", return_value=str(profile_root)):
        client = _make_test_app(tmp_path)
        resp = client.post(
            "/propose",
            json={"profile": "ksi-demo"},
            headers={"X-KSI-Auth": ""},
        )

    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "differ" in body.get("error", "").lower()
