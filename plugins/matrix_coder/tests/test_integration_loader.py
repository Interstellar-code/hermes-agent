"""Real-loader integration test for matrix_coder.

Unlike the other test modules (which call ``core`` functions directly), this one
loads the plugin EXACTLY as the Hermes runtime does — via
``PluginManager._load_directory_module`` + ``register(ctx)`` — and drives the
REAL hook machinery (``PluginManager.invoke_hook``). It proves the end-to-end
path: a trigger message injects the composed persona this turn; a non-trigger
message injects nothing (leak-proof); an implicit coding request routes; a
workflow composes; and the ``/matrix`` command registers.

Skips cleanly when ``hermes_cli`` is not importable (e.g. a bare CI box), so it
never fails the suite for environment reasons.

IMPORTANT: invoke through the SAME ``PluginManager`` instance the plugin was
registered into (``pm.invoke_hook``), NOT the module-level ``invoke_hook``
wrapper, which targets the global singleton.
"""

from __future__ import annotations

from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _load():
    """Load + register matrix_coder via the real loader. Returns (pm, mod) or None."""
    try:
        from hermes_cli import plugins as P  # type: ignore
    except Exception:
        return None
    pm = P.PluginManager()
    man = P.PluginManifest(name="matrix_coder", path=str(_PLUGIN_DIR))
    ctx = P.PluginContext(man, pm)
    mod = pm._load_directory_module(man)
    mod.register(ctx)
    return pm, mod


def _injected(pm, message: str) -> str:
    res = pm.invoke_hook("pre_llm_call", user_message=message, session_id="itest")
    # Hook now returns {"context": str, "target": "developer"} dicts (issue #140).
    # Extract the context text from both dict and plain-string returns.
    parts = []
    for r in res:
        if isinstance(r, dict) and r.get("context"):
            parts.append(str(r["context"]))
        elif isinstance(r, str) and r:
            parts.append(r)
    return "\n".join(parts)


def test_real_loader_trigger_injects_persona_and_lens():
    loaded = _load()
    if loaded is None:
        return  # hermes_cli unavailable -> skip
    pm, _ = loaded
    out = _injected(pm, "matrix review security: check auth in login.py")
    assert out, "trigger turn must inject the composed persona"
    assert "Review" in out, "review persona missing from injection"
    assert "security" in out.lower(), "security lens missing from injection"


def test_real_loader_non_trigger_injects_nothing():
    loaded = _load()
    if loaded is None:
        return
    pm, _ = loaded
    assert _injected(pm, "just a normal question") == ""


def test_real_loader_implicit_request_injects_inferred_persona():
    loaded = _load()
    if loaded is None:
        return
    pm, _ = loaded
    # Strong-signal implicit security review ("is this auth safe?" now quiets
    # under the #140 policy; an explicit-role lead still routes).
    out = _injected(pm, "review the auth login flow for security")
    assert out and "Review Specialist" in out
    assert "Review Lens: Security" in out


def test_real_loader_direct_candidate_injects_right_sizing_question():
    loaded = _load()
    if loaded is None:
        return
    pm, _ = loaded
    out = _injected(pm, "fix README typo")
    assert "<verdict>direct</verdict>" in out
    assert "Invoke Matrix Coder anyway" in out


def test_real_loader_workflow_composes():
    loaded = _load()
    if loaded is None:
        return
    pm, _ = loaded
    out = _injected(pm, "matrix ralph: make the auth tests pass")
    assert out and "Ralph" in out


def test_real_loader_registers_hooks_and_command():
    loaded = _load()
    if loaded is None:
        return
    pm, _ = loaded
    assert {"pre_llm_call", "post_llm_call", "transform_llm_output"} <= set(pm._hooks)
    assert "matrix" in pm._plugin_commands


def test_real_loader_post_llm_call_clears_without_error():
    loaded = _load()
    if loaded is None:
        return
    pm, _ = loaded
    # open a card-less dispatch, then ensure the backstop clear runs clean
    _injected(pm, "matrix verify: confirm the fix")
    pm.invoke_hook("post_llm_call", session_id="itest", assistant_response="done")
    # a following non-trigger turn must inject nothing (state cleared)
    assert _injected(pm, "ordinary message") == ""


if __name__ == "__main__":  # pragma: no cover - stdlib smoke runner
    if _load() is None:
        print("SKIP (hermes_cli unavailable)")
    else:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
                print(f"PASS {name}")
        print("ALL INTEGRATION TESTS PASSED")
