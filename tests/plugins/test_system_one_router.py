"""Tests for system-one-router (dormant advisory plugin). Mock HTTP; no live calls.

Loader note (finding 4): the plugin dir is hyphenated, so it is loaded with the
spec_from_file_location + submodule_search_locations pattern used by the real
plugin loader (hermes_cli/plugins_loader.py:_load_directory_module). That makes
the plugin's RELATIVE imports (from ._client import ...) resolve without any
sys.path injection. Each fixture loads a FRESH module (pop from sys.modules in
teardown) so state can't leak between tests.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "system-one-router"
MODULE_NAME = "system_one_router_plugin"

_loaded: list[str] = []


def _load_plugin() -> object:
    """Fresh load of the hyphenated plugin dir as a package-like module with
    submodule_search_locations set, so relative imports resolve (AGENTS.md §18 /
    production-loader pattern). Tracks the name for teardown eviction."""
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME, PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)])
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = MODULE_NAME
    sys.modules[MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    _loaded.append(MODULE_NAME)
    return mod


def _client_module() -> object:
    """The freshly-loaded _client submodule (owns the _send_request seam)."""
    return sys.modules[f"{MODULE_NAME}._client"]


@pytest.fixture()
def plugin():
    mod = _load_plugin()
    # Teardown (yield-based, so sys.modules stays populated DURING the test
    # body): evict this load (and its relative-import submodules) so the next
    # fixture gets fresh state — the old cache-in-sys.modules loader returned
    # the same module across tests.
    yield mod
    sys.modules.pop(MODULE_NAME, None)
    for sub in ("_client", "_config", "_decisionlog"):
        sys.modules.pop(f"{MODULE_NAME}.{sub}", None)


@pytest.fixture()
def tmp_db(tmp_path):
    return tmp_path / "s1r-test.db"


class FakeCtx:
    def __init__(self):
        self.tools = {}
        self.skills = []

    def register_tool(self, **kw):
        self.tools[kw["name"]] = kw

    def register_skill(self, **kw):
        self.skills.append(kw)


def _ok_response(usage=None, choice="fresh_task", confidence=0.95):
    """Fake urlopen returning a well-formed ok payload."""
    recorded = {
        "answers": {"q": {"type": "choice", "choice": choice, "confidence": confidence}},
        "usage": usage or {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},
    }

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(recorded).encode()

    return lambda req, timeout: FakeResp()


# ── register(): never raises, tools + skill registered ──────────────────

def test_register_never_raises_and_registers(plugin):
    ctx = FakeCtx()
    plugin.register(ctx)  # no key, endpoint down — must not raise
    assert "system_one_status" in ctx.tools
    assert "system_one_route" in ctx.tools
    assert "system_one_decide" in ctx.tools
    assert len(ctx.skills) == 1
    for name in ("system_one_route", "system_one_decide"):
        assert callable(ctx.tools[name].get("check_fn"))


def test_registered_handlers_return_json_strings(plugin, monkeypatch):
    # tools/registry.py rejects dict results ("unsupported result type: dict").
    monkeypatch.setenv("SYSTEM_ONE_ROUTER_ENABLED", "0")
    ctx = FakeCtx()
    plugin.register(ctx)
    for name, args in (("system_one_status", {}),
                       ("system_one_route", {"message": "hi"}),
                       ("system_one_decide", {"state": {}, "questions": {}})):
        out = ctx.tools[name]["handler"](args)
        assert isinstance(out, str), name
        assert isinstance(json.loads(out), dict), name


def test_register_exception_is_swallowed(plugin):
    class BoomCtx:
        def register_tool(self, **kw):
            raise RuntimeError("boom")

        def register_skill(self, **kw):
            pass

    plugin.register(BoomCtx())  # must not raise


def test_plugin_uses_relative_imports_no_syspath(plugin):
    # Finding 4: the module must resolve its siblings through the package
    # machinery (relative imports), not a sys.path-injected flat import.
    assert plugin.__package__ == MODULE_NAME
    assert plugin.__spec__.submodule_search_locations
    assert plugin.JevClient is not None
    assert plugin.load_config is not None
    assert plugin.DecisionLog is not None


# ── finding 1: gate is cheap — dormant + never any network call ─────────

def test_gate_dormant_enabled_false_no_network(plugin, monkeypatch, tmp_path):
    """enabled=false → check_fn False AND urlopen is NEVER called (call counter)."""
    monkeypatch.setenv("SYSTEM_ONE_ROUTER_ENABLED", "false")
    monkeypatch.setenv("SYSTEM_ONE_ROUTER_BASE_URL", "http://127.0.0.1:1/decide")

    calls = []

    def counting_send(req, timeout=None):
        calls.append(req)
        raise AssertionError("network call attempted while plugin disabled")

    with patch.object(_client_module(), "_send_request", counting_send):
        assert plugin._tool_gate() is False
    assert calls == []


def test_gate_true_only_when_enabled_and_key_present(plugin, monkeypatch, tmp_path):
    monkeypatch.setenv("SYSTEM_ONE_ROUTER_ENABLED", "true")
    with patch.object(plugin, "read_api_key", lambda env_path=None: ""):
        assert plugin._tool_gate() is False  # enabled but no key
    with patch.object(plugin, "read_api_key", lambda env_path=None: "test-key"):
        assert plugin._tool_gate() is True


def test_gate_exception_is_false(plugin, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config layer broken")

    monkeypatch.setattr(plugin, "load_config", boom)
    assert plugin._tool_gate() is False


def test_registered_route_tools_use_cheap_gate(plugin):
    ctx = FakeCtx()
    plugin.register(ctx)
    assert ctx.tools["system_one_route"]["check_fn"] is plugin._tool_gate
    assert ctx.tools["system_one_decide"]["check_fn"] is plugin._tool_gate


# ── finding 5: status tool is ungated, cheap, no probe ──────────────────

def test_status_handler_cheap_no_network(plugin, monkeypatch, tmp_path):
    """status must not fire the paid probe: the request seam is NEVER hit, even when enabled."""
    monkeypatch.setenv("SYSTEM_ONE_ROUTER_ENABLED", "true")
    with patch.object(_client_module(), "_send_request",
                      side_effect=AssertionError("status must not touch the network")):
        with patch.object(plugin, "read_api_key", lambda env_path=None: "test-key"):
            st = plugin._status_handler({})
    assert st["enabled"] is True
    assert st["key_present"] is True
    assert "log" in st
    assert "reachability" in st  # points at per-call fallback reasons
    assert "endpoint_reachable" not in st


def test_status_tool_gated_like_paid_tools(plugin, monkeypatch):
    """Finding 5: the status tool is registered with the same cheap gate —
    hidden when the plugin is disabled, visible when enabled."""
    ctx = FakeCtx()
    plugin.register(ctx)
    assert ctx.tools["system_one_status"]["check_fn"] is plugin._tool_gate
    monkeypatch.setenv("SYSTEM_ONE_ROUTER_ENABLED", "false")
    assert ctx.tools["system_one_status"]["check_fn"]() is False
    monkeypatch.setenv("SYSTEM_ONE_ROUTER_ENABLED", "true")
    with patch.object(plugin, "read_api_key", lambda env_path=None: "test-key"):
        assert ctx.tools["system_one_status"]["check_fn"]() is True


def test_status_handler_never_raises(plugin, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("everything is broken")

    monkeypatch.setattr(plugin, "load_config", boom)
    st = plugin._status_handler({})
    assert st["status"] if "status" in st else st["enabled"] is False


# ── client: fail-closed paths ───────────────────────────────────────────

def test_missing_key_falls_back(plugin):
    client = plugin.JevClient(api_key="")
    res = client.decide("state", {"q": {"type": "noul", "instructions": "x"}})
    assert res["status"] == "fallback" and res["reason"] == "missing_api_key"


def test_http_429_maps_to_reason(plugin):
    client = plugin.JevClient(api_key="k")
    import urllib.error

    def raise_429(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "rate", None, None)

    with patch.object(_client_module(), "_send_request", raise_429):
        res = client.decide("state", {"q": {"type": "noul", "instructions": "x"}})
    assert res["status"] == "fallback" and res["reason"] == "http_429"


def test_http_500_maps_to_http_error(plugin):
    client = plugin.JevClient(api_key="k")
    import urllib.error

    def raise_500(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", None, None)

    with patch.object(_client_module(), "_send_request", raise_500):
        res = client.decide("state", {"q": {"type": "noul", "instructions": "x"}})
    assert res["status"] == "fallback" and res["reason"] == "http_error"


def test_timeout_maps_to_reason(plugin):
    client = plugin.JevClient(api_key="k")
    import urllib.error

    def raise_timeout(req, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))

    with patch.object(_client_module(), "_send_request", raise_timeout):
        res = client.decide("state", {"q": {"type": "noul", "instructions": "x"}})
    assert res["status"] == "fallback" and res["reason"] == "timeout"


# ── happy path parse (recorded real response shape) ─────────────────────

def test_ok_parse_real_shape(plugin):
    client = plugin.JevClient(api_key="k")
    recorded = {
        "model": "jev-1.13.0",
        "answers": {"lane": {"type": "choice", "choice": "neo", "confidence": 1,
                             "probabilities": {"neo": 1, "switch": 0}}},
        "usage": {"input_tokens": 378, "output_tokens": 48,
                  "cost_usd": 0.000159, "credits_remaining_usd": 19.7},
    }

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(recorded).encode()

    with patch.object(_client_module(), "_send_request", lambda req, timeout=None: FakeResp()):
        res = client.decide("state", {"lane": {"type": "choice",
                                               "instructions": "x",
                                               "criteria": {"neo": "code"}}},
                            question_key="lane")
    assert res["status"] == "ok" and res["choice"] == "neo"
    assert res["confidence"] == 1
    assert res["usage"]["cost_usd"] == pytest.approx(0.000159)


# ── handler tests with an injected fake client ──────────────────────────

class FakeClient:
    """Stands in for JevClient: scripted decide() results, counts calls/costs."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def decide(self, state, questions, question_key=None):
        self.calls.append(question_key)
        return self.script.pop(0)


def _patch_client(monkeypatch, plugin, fake):
    monkeypatch.setattr(plugin, "JevClient", lambda **kw: fake)
    monkeypatch.setattr(plugin, "read_api_key", lambda env_path=None: "test-key")


def _enabled(monkeypatch, tmp_path, *, plugin, enabled=True, max_usd=2.0):
    monkeypatch.setenv("SYSTEM_ONE_ROUTER_ENABLED", "true" if enabled else "false")
    # Keep the real config layer but point the DecisionLog at a tmp db by
    # patching the default path resolution.
    monkeypatch.setattr(plugin.DecisionLog, "_default_path",
                        staticmethod(lambda: tmp_path / "h.db"))


def test_route_happy_path_general(plugin, monkeypatch, tmp_path):
    _enabled(monkeypatch, tmp_path, plugin=plugin)
    fake = FakeClient([
        {"status": "ok", "choice": "general", "confidence": 0.9, "reason": None,
         "latency_ms": 100, "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01}},
    ])
    _patch_client(monkeypatch, plugin, fake)
    out = plugin._route_handler({"message": "what's the status"})
    assert out["status"] == "ok" and out["kind"] == "general"
    assert out["lane"] is None and out["confidence"] == pytest.approx(0.9)
    assert out["usage"]["cost_usd"] == pytest.approx(0.01)
    assert fake.calls == ["kind"]  # no lane call for non-fresh_task


def test_route_two_call_cost_aggregation(plugin, monkeypatch, tmp_path):
    """THE finding-2 test: fresh_task fires kind+lane; combined cost_usd is
    the SUM of both calls (old code always added 0.0)."""
    _enabled(monkeypatch, tmp_path, plugin=plugin)
    fake = FakeClient([
        {"status": "ok", "choice": "fresh_task", "confidence": 0.9, "reason": None,
         "latency_ms": 100, "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.02}},
        {"status": "ok", "choice": "neo", "confidence": 0.8, "reason": None,
         "latency_ms": 50, "usage": {"input_tokens": 7, "output_tokens": 3, "cost_usd": 0.03}},
    ])
    _patch_client(monkeypatch, plugin, fake)
    out = plugin._route_handler({"message": "fix the failing auth test"})
    assert fake.calls == ["kind", "lane"]
    assert out["lane"] == "neo"
    assert out["confidence"] == pytest.approx(0.8)  # min(0.9, 0.8)
    assert out["usage"]["cost_usd"] == pytest.approx(0.02 + 0.03)
    assert out["usage"]["input_tokens"] == 17 and out["usage"]["output_tokens"] == 8
    assert out["latency_ms"] == 150
    # spend recorded to the log matches the COMBINED cost (cap accounting)
    assert plugin.DecisionLog().month_spend_usd() == pytest.approx(0.05)


def test_route_lane_fallback_propagates(plugin, monkeypatch, tmp_path):
    """Lane call falls back → outer result is the fallback (kind preserved,
    confidence None, lane None, no masquerading as fresh ok)."""
    _enabled(monkeypatch, tmp_path, plugin=plugin)
    fake = FakeClient([
        {"status": "ok", "choice": "fresh_task", "confidence": 0.9, "reason": None,
         "latency_ms": 100, "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.02}},
        {"status": "fallback", "reason": "timeout", "choice": None, "confidence": None,
         "latency_ms": 5000, "usage": {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}},
    ])
    _patch_client(monkeypatch, plugin, fake)
    out = plugin._route_handler({"message": "fix the failing auth test"})
    assert out["status"] == "fallback"
    assert out["reason"] == "timeout"
    assert out["kind"] == "fresh_task"  # preserved
    assert out["lane"] is None and out["confidence"] is None
    assert out["usage"]["cost_usd"] == pytest.approx(0.02)  # kind call still cost money


def test_route_disabled_returns_fallback_no_network(plugin, monkeypatch, tmp_path):
    _enabled(monkeypatch, tmp_path, plugin=plugin, enabled=False)
    with patch.object(_client_module(), "_send_request",
                      side_effect=AssertionError("no network when disabled")):
        out = plugin._route_handler({"message": "hi"})
    assert out == {"status": "fallback", "reason": "disabled"}


def test_route_cap_exceeded(plugin, monkeypatch, tmp_path):
    """month_spend >= max_monthly_usd → fallback cap_exceeded, NO client call."""
    _enabled(monkeypatch, tmp_path, plugin=plugin, max_usd=2.0)
    log = plugin.DecisionLog()
    log.record("h", "route", "neo", 0.9, "ok", None, 10, 1.5)
    log.record("h2", "route", "trinity", 0.9, "ok", None, 10, 0.5)
    fake = FakeClient([])
    _patch_client(monkeypatch, plugin, fake)
    out = plugin._route_handler({"message": "deploy v2"})
    assert out == {"status": "fallback", "reason": "cap_exceeded"}
    assert fake.calls == []  # paid call never fired
    assert log.month_spend_usd() == pytest.approx(2.0)


def test_route_handler_never_raises(plugin, monkeypatch, tmp_path):
    _enabled(monkeypatch, tmp_path, plugin=plugin)

    class ExplodingClient:
        def decide(self, *a, **k):
            raise RuntimeError("socket exploded")

    _patch_client(monkeypatch, plugin, ExplodingClient())
    out = plugin._route_handler({"message": "hi"})
    assert out["status"] == "fallback"


def test_decide_handler_happy_and_cap(plugin, monkeypatch, tmp_path):
    _enabled(monkeypatch, tmp_path, plugin=plugin)
    fake = FakeClient([
        {"status": "ok", "choice": "yes", "confidence": 0.7, "reason": None,
         "latency_ms": 80, "usage": {"input_tokens": 4, "output_tokens": 2, "cost_usd": 0.005}},
    ])
    _patch_client(monkeypatch, plugin, fake)
    out = plugin._decide_handler({"state": {"x": 1}, "questions": {"go": {"type": "choice"}}})
    assert out["status"] == "ok" and out["choice"] == "yes"
    assert plugin.DecisionLog().month_spend_usd() == pytest.approx(0.005)

    # now blow through the cap
    plugin.DecisionLog().record("h", "generic", "yes", 0.7, "ok", None, 5, 5.0)
    out2 = plugin._decide_handler({"state": {"x": 1}, "questions": {"go": {"type": "choice"}}})
    assert out2 == {"status": "fallback", "reason": "cap_exceeded"}


def test_decide_handler_bad_payload(plugin, monkeypatch, tmp_path):
    _enabled(monkeypatch, tmp_path, plugin=plugin)
    fake = FakeClient([])
    _patch_client(monkeypatch, plugin, fake)
    out = plugin._decide_handler({"state": None, "questions": "not-a-dict"})
    assert out == {"status": "fallback", "reason": "bad_payload"}
    assert fake.calls == []


# ── decision log: schema-once, rollover, warnings ────────────────────────

def test_log_schema_executed_once_per_process(tmp_path, monkeypatch):
    """Finding 6: executescript runs exactly once per resolved db path even
    across multiple DecisionLog instances/records."""
    import importlib.util as _ilu

    dl_spec = _ilu.spec_from_file_location(
        "s1r_decisionlog_fresh", PLUGIN_DIR / "_decisionlog.py")
    dl = importlib.util.module_from_spec(dl_spec)
    dl_spec.loader.exec_module(dl)

    db = tmp_path / "t.db"
    calls = []
    real_connect = sqlite3.connect

    class SpyConn:
        def __init__(self, conn):
            self._conn = conn

        def executescript(self, script):
            calls.append(script)
            return self._conn.executescript(script)

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, *a):
            return self._conn.__exit__(*a)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def spy_connect(*a, **k):
        return SpyConn(real_connect(*a, **k))

    with patch("sqlite3.connect", spy_connect):
        log = dl.DecisionLog(db_path=db)
        log.record("h1", "route", "neo", 0.9, "ok", None, 10, 0.1)
        log.record("h2", "route", "neo", 0.9, "ok", None, 10, 0.1)
        assert dl.DecisionLog(db_path=db).month_spend_usd() == pytest.approx(0.2)
        dl.DecisionLog(db_path=db).stats()
    assert len(calls) == 1, f"executescript ran {len(calls)} times, expected 1"


def test_log_month_rollover_excludes_previous_month(tmp_path, plugin):
    db = tmp_path / "t.db"
    log = plugin.DecisionLog(db_path=db)
    log.record("now", "route", "neo", 0.9, "ok", None, 10, 1.0)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO decisions (input_hash, question_kind, status, latency_ms, cost_usd, month)"
        " VALUES ('old', 'route', 'ok', 10, 9.0, '2000-01')")
    con.commit()
    con.close()
    assert log.month_spend_usd() == pytest.approx(1.0)  # 2000-01 row excluded


def test_log_record_failure_logs_warning(tmp_path, plugin, caplog):
    """record() failure is logged (cap enforcement must not fail open silently)."""
    db = tmp_path / "t.db"
    log = plugin.DecisionLog(db_path=db)
    # Point record's connection at a directory → sqlite3.connect fails.
    with patch.object(plugin.DecisionLog, "_connect",
                      side_effect=sqlite3.OperationalError("unable to open database file")):
        import logging

        with caplog.at_level(logging.WARNING, logger="system_one_router_plugin._decisionlog"):
            log.record("h", "route", "neo", 0.9, "ok", None, 10, 1.0)
    assert any("record failed" in r.message for r in caplog.records)


def test_log_month_spend_failure_logs_warning_and_returns_zero(tmp_path, plugin, caplog):
    log = plugin.DecisionLog(db_path=tmp_path / "x.db")
    with patch.object(plugin.DecisionLog, "_connect",
                      side_effect=sqlite3.OperationalError("nope")):
        import logging

        with caplog.at_level(logging.WARNING, logger="system_one_router_plugin._decisionlog"):
            assert log.month_spend_usd() == 0.0
    assert any("month_spend" in r.message for r in caplog.records)


def test_log_connections_closed(tmp_path, plugin):
    """Finding 6: connections are closed after each call (no leak)."""
    db = tmp_path / "t.db"
    log = plugin.DecisionLog(db_path=db)
    log.record("h", "route", "neo", 0.9, "ok", None, 10, 0.1)
    log.month_spend_usd()
    con = sqlite3.connect(db)
    # everything written is committed and visible from a fresh connection
    n = con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    con.close()
    assert n == 1


def test_log_hash_only(tmp_path, plugin):
    log = plugin.DecisionLog(db_path=tmp_path / "t.db")
    secret = "my-secret-message-about-invoices"
    log.record(plugin.hash_state(secret), "route", "neo", 0.9, "ok", None, 100, 0.001)
    con = sqlite3.connect(tmp_path / "t.db")
    row = con.execute("SELECT input_hash, choice FROM decisions").fetchone()
    assert secret not in (row[0] or "")
    assert row[0] != secret and len(row[0]) == 64  # sha256 hex
    assert row[1] == "neo"


def test_month_spend_sums_and_cap_boundary(tmp_path, plugin):
    db = tmp_path / "t.db"
    log = plugin.DecisionLog(db_path=db)
    log.record("h1", "route", "neo", 0.9, "ok", None, 10, 1.5)
    assert log.month_spend_usd() == pytest.approx(1.5)
    cfg = dict(plugin.load_config(hermes_home=tmp_path))
    assert cfg["max_monthly_usd"] == 2.0  # default present
    assert log.month_spend_usd() >= cfg["max_monthly_usd"] * 0.75  # near cap


# ── config: real overlay merge + env escape hatch ───────────────────────

def test_config_defaults_when_no_file(tmp_path, plugin, monkeypatch):
    monkeypatch.delenv("SYSTEM_ONE_ROUTER_ENABLED", raising=False)
    cfg = plugin.load_config(hermes_home=tmp_path)
    assert cfg["enabled"] is False
    assert cfg["max_monthly_usd"] == 2.0


def test_config_yaml_overlay(tmp_path, plugin, monkeypatch):
    import yaml
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"system-one-router": {"enabled": True, "confidence_floor": 0.8}}}))
    monkeypatch.delenv("SYSTEM_ONE_ROUTER_ENABLED", raising=False)
    cfg = plugin.load_config(hermes_home=tmp_path)
    assert cfg["enabled"] is True
    assert cfg["confidence_floor"] == 0.8
    assert cfg["max_monthly_usd"] == 2.0  # default preserved


def test_config_env_escape_hatch(tmp_path, plugin, monkeypatch):
    monkeypatch.setenv("SYSTEM_ONE_ROUTER_ENABLED", "true")
    cfg = plugin.load_config(hermes_home=tmp_path)
    assert cfg["enabled"] is True


# ── finding 9: read_api_key env-first + export/quote parsing ────────────

def test_read_api_key_env_var_wins(plugin, monkeypatch, tmp_path):
    """Env var takes precedence over the profile .env file."""
    (tmp_path / ".env").write_text("JEV_API_KEY=file-key\n")
    monkeypatch.setenv("JEV_API_KEY", "env-key")
    assert plugin.read_api_key(env_path=tmp_path / ".env") == "env-key"


def test_read_api_key_export_prefix_stripped(plugin, monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    (tmp_path / ".env").write_text("export JEV_API_KEY=abc123\n")
    assert plugin.read_api_key(env_path=tmp_path / ".env") == "abc123"


def test_read_api_key_quoted_values(plugin, monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    for raw, want in (
        ('JEV_API_KEY="dq-key"', "dq-key"),
        ("JEV_API_KEY='sq-key'", "sq-key"),
        ('export JEV_API_KEY="both"', "both"),
    ):
        (tmp_path / ".env").write_text(raw + "\n")
        assert plugin.read_api_key(env_path=tmp_path / ".env") == want


def test_read_api_key_missing_file_returns_empty(plugin, monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    assert plugin.read_api_key(env_path=tmp_path / "nope.env") == ""


# ── finding 10: https-only + no redirect following ──────────────────────

def test_http_base_url_falls_back_insecure(plugin):
    client = plugin.JevClient(api_key="k", base_url="http://jevtypesafeai.com/api/v1/decide")
    with patch.object(_client_module(), "_send_request",
                      side_effect=AssertionError("http:// URL must never be opened")):
        res = client.decide("state", {"q": {"type": "noul", "instructions": "x"}})
    assert res["status"] == "fallback" and res["reason"] == "insecure_url"
    assert "insecure_url" in plugin.FALLBACK_REASONS


def test_redirect_not_followed_maps_http_error(plugin):
    """A 302 must surface as http_error, never be followed (Bearer re-send)."""
    import urllib.error

    client = plugin.JevClient(api_key="k")

    def raise_302(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 302, "Found",
                                     {"Location": "https://evil.example/"}, None)

    with patch.object(_client_module(), "_send_request", raise_302):
        res = client.decide("state", {"q": {"type": "noul", "instructions": "x"}})
    assert res["status"] == "fallback" and res["reason"] == "http_error"


def test_no_redirect_handler_refuses_redirect(plugin):
    import urllib.error
    import urllib.request as ur

    handler = plugin._NoRedirectHandler()
    req = ur.Request("https://jevtypesafeai.com/api/v1/decide")
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://x/") is None


def test_base_url_host_allowlist(plugin):
    """Finding 10: https AND an allowlisted host are required — a poisoned
    config/env base_url cannot exfiltrate the Bearer key to an attacker host."""
    from urllib.parse import urlparse
    assert plugin.DEFAULT_BASE_URL == "https://jevtypesafeai.com/api/v1/decide"
    assert urlparse(plugin.DEFAULT_BASE_URL).hostname in _client_module().ALLOWED_BASE_HOSTS
    assert "api.typesafe.ai" in _client_module().ALLOWED_BASE_HOSTS
    for bad in ("https://evil.example/api/v1/decide",
                "https://jevtypesafeai.com.evil.example/api/v1/decide",
                "http://jevtypesafeai.com/api/v1/decide"):
        client = plugin.JevClient(api_key="k", base_url=bad)
        with patch.object(_client_module(), "_send_request",
                          side_effect=AssertionError("non-allowlisted host must never be opened")):
            res = client.decide("state", {"q": {"type": "noul", "instructions": "x"}})
        assert res["status"] == "fallback" and res["reason"] == "insecure_url"


# ── finding 11: decide-handler input caps + composite hash + question_kind ──

def test_decide_oversized_state_rejected(plugin, monkeypatch, tmp_path):
    _enabled(monkeypatch, tmp_path, plugin=plugin)
    fake = FakeClient([])
    _patch_client(monkeypatch, plugin, fake)
    big = {"blob": "x" * 9000}
    out = plugin._decide_handler({"state": big, "questions": {"go": {"type": "choice"}}})
    assert out == {"status": "fallback", "reason": "payload_too_large"}
    assert fake.calls == []  # paid call never fired


def test_decide_too_many_questions_rejected(plugin, monkeypatch, tmp_path):
    _enabled(monkeypatch, tmp_path, plugin=plugin)
    fake = FakeClient([])
    _patch_client(monkeypatch, plugin, fake)
    qs = {f"q{i}": {"type": "choice"} for i in range(4)}
    out = plugin._decide_handler({"state": {"x": 1}, "questions": qs})
    assert out == {"status": "fallback", "reason": "payload_too_large"}
    assert fake.calls == []


def test_decide_hash_covers_questions(plugin, monkeypatch, tmp_path):
    """Same state, different questions → different input_hash in the log."""
    _enabled(monkeypatch, tmp_path, plugin=plugin)
    fake = FakeClient([
        {"status": "ok", "choice": "a", "confidence": 0.5, "reason": None,
         "latency_ms": 1, "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.001}},
        {"status": "ok", "choice": "b", "confidence": 0.5, "reason": None,
         "latency_ms": 1, "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.001}},
    ])
    _patch_client(monkeypatch, plugin, fake)
    plugin._decide_handler({"state": {"x": 1}, "questions": {"qa": {"type": "choice"}}})
    plugin._decide_handler({"state": {"x": 1}, "questions": {"qb": {"type": "choice"}}})
    con = sqlite3.connect(tmp_path / "h.db")
    hashes = [r[0] for r in con.execute("SELECT input_hash FROM decisions ORDER BY id")]
    kinds = [r[0] for r in con.execute("SELECT question_kind FROM decisions ORDER BY id")]
    con.close()
    assert len(set(hashes)) == 2  # questions are part of the hash
    assert kinds == ["qa", "qb"]  # actual first question key recorded


# ── finding 12: conversation keeps the LAST four turns ──────────────────

def test_build_state_keeps_last_four_turns(plugin):
    conv = [{"from": "user", "text": f"turn-{i}"} for i in range(1, 7)]
    state = plugin._build_state({"message": "m", "conversation": conv})
    texts = [c["text"] for c in state["conversation"]]
    assert texts == ["turn-3", "turn-4", "turn-5", "turn-6"]


def test_build_state_non_list_conversation_never_raises(plugin):
    """Finding 12: a model-supplied non-list conversation is dropped, not a TypeError."""
    for bad in ("not a list", 42, {"from": "user"}, None):
        state = plugin._build_state({"message": "m", "conversation": bad})
        assert "conversation" not in state
        assert state["message"] == "m"


def test_build_state_skips_non_dict_entries_keeps_recent(plugin):
    """Finding 12: non-dict entries are filtered BEFORE the last-four slice, so
    junk trailing turns cannot evict the most recent valid turn."""
    conv = [{"from": "user", "text": "t1"}, "junk", 7,
            {"from": "user", "text": "t2"}, None,
            {"from": "user", "text": "t3"}, {"from": "user", "text": "t4"},
            {"from": "user", "text": "t5"}, {"from": "user", "text": "t6"}]
    state = plugin._build_state({"message": "m", "conversation": conv})
    texts = [c["text"] for c in state["conversation"]]
    assert texts == ["t3", "t4", "t5", "t6"]


# ── finding 13: response validation (fail-closed) ───────────────────────

def test_top_level_list_is_bad_payload(plugin):
    client = plugin.JevClient(api_key="k")

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"[1, 2, 3]"

    with patch.object(_client_module(), "_send_request",
                      lambda req, timeout=None: FakeResp()):
        res = client.decide("state", {"q": {"type": "noul", "instructions": "x"}})
    assert res["status"] == "fallback" and res["reason"] == "bad_payload"


def test_invalid_json_body_is_bad_payload(plugin):
    """Finding 13: a truncated/garbage body (json.JSONDecodeError) is bad_payload."""
    client = plugin.JevClient(api_key="k")

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"answers": {"q": {"choice": "neo"'

    with patch.object(_client_module(), "_send_request",
                      lambda req, timeout=None: FakeResp()):
        res = client.decide("state", {"q": {"type": "noul", "instructions": "x"}})
    assert res["status"] == "fallback" and res["reason"] == "bad_payload"


def test_missing_answers_key_is_bad_payload(plugin):
    client = plugin.JevClient(api_key="k")

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"usage": {"cost_usd": 0.01}}).encode()

    with patch.object(_client_module(), "_send_request",
                      lambda req, timeout=None: FakeResp()):
        res = client.decide("state", {"q": {"type": "noul", "instructions": "x"}})
    assert res["status"] == "fallback" and res["reason"] == "bad_payload"


def test_non_numeric_cost_is_bad_payload(plugin):
    client = plugin.JevClient(api_key="k")

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({
                "answers": {"q": {"type": "noul", "noul": 0.5, "confidence": 0.9}},
                "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": "lots"},
            }).encode()

    with patch.object(_client_module(), "_send_request",
                      lambda req, timeout=None: FakeResp()):
        res = client.decide("state", {"q": {"type": "noul", "instructions": "x"}})
    assert res["status"] == "fallback" and res["reason"] == "bad_payload"


# ── finding 14: bare socket.timeout during read() → timeout ─────────────

def test_socket_timeout_during_read_maps_timeout(plugin):
    import socket

    client = plugin.JevClient(api_key="k")

    class SlowResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            raise socket.timeout("timed out")

    with patch.object(_client_module(), "_send_request", lambda req, timeout=None: SlowResp()):
        res = client.decide("state", {"q": {"type": "noul", "instructions": "x"}})
    assert res["status"] == "fallback" and res["reason"] == "timeout"


# ── finding 16: hygiene ─────────────────────────────────────────────────

def test_config_defaults_have_no_log_decisions(plugin, tmp_path, monkeypatch):
    monkeypatch.delenv("SYSTEM_ONE_ROUTER_ENABLED", raising=False)
    cfg = plugin.load_config(hermes_home=tmp_path)
    assert "log_decisions" not in cfg
    assert "log_decisions" not in plugin._config_defaults()


def test_route_string_confidence_never_crashes_or_stores_raw(plugin, monkeypatch, tmp_path):
    """Finding 16: Jev returning confidence as a string must not crash min()
    nor store the raw string — route records a coerced value (or None)."""
    _enabled(monkeypatch, tmp_path, plugin=plugin)
    fake = FakeClient([
        {"status": "ok", "choice": "general", "confidence": "high", "reason": None,
         "latency_ms": 10, "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.001}},
    ])
    _patch_client(monkeypatch, plugin, fake)
    out = plugin._route_handler({"message": "hi"})
    assert out["status"] == "ok"  # handler did not raise
    con = sqlite3.connect(tmp_path / "h.db")
    row = con.execute("SELECT confidence FROM decisions").fetchone()
    con.close()
    assert row[0] is None  # coerced, not the raw string


def test_decide_handler_records_none_for_string_confidence(plugin, monkeypatch, tmp_path):
    _enabled(monkeypatch, tmp_path, plugin=plugin)
    fake = FakeClient([
        {"status": "ok", "choice": "yes", "confidence": "high", "reason": None,
         "latency_ms": 5, "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.001}},
    ])
    _patch_client(monkeypatch, plugin, fake)
    out = plugin._decide_handler({"state": {"x": 1}, "questions": {"go": {"type": "choice"}}})
    assert out["status"] == "ok"  # handler did not raise
    con = sqlite3.connect(tmp_path / "h.db")
    row = con.execute("SELECT confidence FROM decisions").fetchone()
    con.close()
    assert row[0] is None  # coerced, not the raw string


# ── finding 17 remainder: concurrent writers ────────────────────────────

def test_log_concurrent_writers(tmp_path, plugin):
    """5 threads × 20 record()+month_spend_usd() on one db: 100 rows, no error."""
    from concurrent.futures import ThreadPoolExecutor

    db = tmp_path / "conc.db"
    log = plugin.DecisionLog(db_path=db)

    def worker(n):
        for i in range(20):
            log.record(f"h-{n}-{i}", "route", "neo", 0.9, "ok", None, 1, 0.001)
            log.month_spend_usd()

    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(worker, range(5)))
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    con.close()
    assert n == 100


# ── real discovery: PluginManager registers the plugin's tools ──────────

def test_real_discovery_registers_tools(tmp_path, monkeypatch):
    """One real-discovery register test: PluginManager with a temp HERMES_HOME
    discovers the bundled plugin and register() runs against the real ctx."""
    import yaml

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["system-one-router"]}}))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("SYSTEM_ONE_ROUTER_ENABLED", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    from hermes_cli import plugins as pmod
    from tools.registry import registry
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    assert "system-one-router" in mgr._plugins
    loaded = mgr._plugins["system-one-router"]
    assert loaded.manifest.source == "bundled"
    assert loaded.enabled
    # register() ran against the real registry: the three tools exist
    scope = mgr.scope_key
    tool_names = {name for name in ("system_one_status", "system_one_route",
                                    "system_one_decide")
                  if registry.get_entry(name, scope=scope) is not None}
    assert tool_names == {"system_one_status", "system_one_route", "system_one_decide"}

def test_no_probe_cache_module_state(plugin):
    assert not hasattr(plugin, "_probe_cache"), \
        "module-level probe cache turned one boot-time blip into a permanent outage"
    assert not hasattr(plugin, "_endpoint_reachable")


@pytest.mark.parametrize("bad", ["nan", "inf", "-1"])
def test_nonfinite_cap_fails_closed(plugin, monkeypatch, tmp_path, bad):
    _enabled(monkeypatch, tmp_path, plugin=plugin)
    monkeypatch.setenv("SYSTEM_ONE_ROUTER_MAX_MONTHLY_USD", bad)
    fake = FakeClient([])
    _patch_client(monkeypatch, plugin, fake)
    out = plugin._route_handler({"message": "hi"})
    assert out == {"status": "fallback", "reason": "cap_exceeded"}


def test_state_redacts_secrets(plugin):
    st = plugin._build_state({"message": "key sk-ant-api03-" + "a" * 40 + " here"})
    assert "a" * 40 not in st["message"]


def test_redaction_runs_before_truncation(plugin):
    sec = "sk-ant-api03-" + "b" * 40
    st = plugin._build_state({"message": "x" * (1200 - 10) + " " + sec, "reply_target": "y" * 390 + " " + sec,
                              "conversation": [{"from": "u", "text": "z" * 390 + " " + sec}]})
    for v in (st["message"], st["reply_target"], st["conversation"][0]["text"]):
        assert "b" * 5 not in v  # redactor keeps only a short masked prefix
