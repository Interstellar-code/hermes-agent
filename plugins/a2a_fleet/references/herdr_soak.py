"""Phase 4 local soak against the REAL Herdr instance.

Read-only and Fleet-side only. It never types into a pane, never changes the
terminal layout, and never touches the user's real fleet.yaml or state.db: it
builds a throwaway HERMES_HOME and lets the herdr CLI talk to the real local
socket. herdr_request_action is deliberately NOT exercised — that is the one
path that would write into a live session someone is working in.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO = Path("/Users/rohits/.hermes/hermes-agent")
sys.path.insert(0, str(REPO / "plugins"))

HOME = Path(tempfile.mkdtemp(prefix="herdr_soak_"))
PROFILE = HOME / "profiles" / "switch"
PROFILE.mkdir(parents=True)
(HOME / "active_profile").write_text("switch")
os.environ["HERMES_HOME"] = str(PROFILE)

REAL_WORKSPACES = [
    "/Users/rohits/.hermes/hermes-agent",
    "/Users/rohits/Development/servers",
    "/Users/rohits/Development/hermes-switchui",
    "/Users/rohits/hermes",
]

(PROFILE / "fleet.yaml").write_text(
    yaml.safe_dump(
        {
            "fleet": {
                "enabled": True,
                "self": {"name": "switch"},
                "server": {
                    "bind_host": "127.0.0.1",
                    "bind_port": 9319,
                    "auth_required": False,
                    "token_env": "SWITCH_A2A_TOKEN",
                },
                "response_handler": "echo",
                "agents": {},
                "herdr": {
                    # allow_actions is ON so the preview path is exercised for
                    # real; request_action is simply never called.
                    "hosts": {
                        "mac-mini": {
                            "transport": "local_socket",
                            "allowed_workspaces": REAL_WORKSPACES,
                            "allow_actions": True,
                        }
                    },
                },
            }
        }
    )
)

import a2a_fleet.herdr_binding as hb  # noqa: E402
import a2a_fleet.herdr_tools as ht  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def record(check: str, verdict: str, detail: str = "") -> None:
    RESULTS.append((check, verdict, detail))
    print(f"[{verdict:4}] {check}" + (f" — {detail}" if detail else ""))


async def timed(coro_fn, n: int = 5):
    samples = []
    last = None
    for _ in range(n):
        start = time.perf_counter()
        last = await coro_fn()
        samples.append((time.perf_counter() - start) * 1000)
    return last, samples


async def main() -> int:
    print(f"soak HERMES_HOME={PROFILE}\n")

    # 1. Capability probe -----------------------------------------------
    status, samples = await timed(lambda: ht.herdr_status_handler(host_alias="mac-mini"))
    if status.get("status") != "ok":
        record("capability probe", "FAIL", str(status))
        return 1
    record(
        "capability probe",
        "PASS",
        f"herdr {status.get('version')} protocol {status.get('protocol')} "
        f"transport {status.get('transport')} | median {statistics.median(samples):.0f}ms",
    )

    # 2. Discovery latency ----------------------------------------------
    listed, samples = await timed(
        lambda: ht.herdr_list_sessions_handler(host_alias="mac-mini"), n=7
    )
    sessions = listed.get("sessions") or []
    record(
        "discovery latency",
        "PASS" if listed.get("status") == "ok" else "FAIL",
        f"{len(sessions)} sessions | median {statistics.median(samples):.0f}ms "
        f"p-max {max(samples):.0f}ms | filtered_by_allowlist="
        f"{listed.get('filtered_out_by_allowlist')}",
    )
    if not sessions:
        record("soak", "FAIL", "no live sessions to inspect")
        return 1

    target = sessions[0]
    terminal_id = target["terminal_id"]

    # 3. Exact inspection ------------------------------------------------
    inspected, samples = await timed(
        lambda: ht.herdr_inspect_session_handler(
            host_alias="mac-mini", terminal_id=terminal_id
        )
    )
    record(
        "exact inspection",
        "PASS" if inspected.get("status") == "ok" else "FAIL",
        f"{terminal_id} kind={inspected.get('session', {}).get('agent_kind')} "
        f"median {statistics.median(samples):.0f}ms",
    )

    # 4. agent_kind regression (the None bug fixed in Phase 3) -----------
    kind = inspected.get("session", {}).get("agent_kind")
    record(
        "agent_kind populated",
        "PASS" if kind else "FAIL",
        f"agent_kind={kind!r} (was None before the normalize-key fix)",
    )

    # 5. Vanished session -------------------------------------------------
    gone = await ht.herdr_inspect_session_handler(
        host_alias="mac-mini", terminal_id="term_deadbeefdeadbeef"
    )
    record(
        "vanished session",
        "PASS" if gone.get("status") == "not_found" else "FAIL",
        f"status={gone.get('status')}",
    )

    # 6. Agent-label rejection -------------------------------------------
    ambiguous = await ht.herdr_inspect_session_handler(
        host_alias="mac-mini", terminal_id=kind or "claude"
    )
    record(
        "agent-label rejected as selector",
        "PASS" if ambiguous.get("status") == "ambiguous_identifier" else "FAIL",
        f"status={ambiguous.get('status')}",
    )

    # 7. Malformed identifiers -------------------------------------------
    nul = await ht.herdr_inspect_session_handler(
        host_alias="mac-mini", terminal_id="term_a\x00b"
    )
    padded = await ht.herdr_inspect_session_handler(
        host_alias="mac-mini", terminal_id=f"  {terminal_id}  "
    )
    record(
        "malformed identifiers",
        "PASS"
        if nul.get("status") == "invalid_terminal_id"
        and padded.get("status") == "invalid_terminal_id"
        and "unexpected error" not in str(nul)
        else "FAIL",
        f"nul={nul.get('status')} padded={padded.get('status')}",
    )

    # 8. Workspace allowlist ---------------------------------------------
    denied_cfg = ht._herdr_cfg()
    denied_cfg["hosts"]["mac-mini"]["allowed_workspaces"] = ["/nowhere/at/all"]
    original = ht._herdr_cfg
    ht._herdr_cfg = lambda: denied_cfg  # type: ignore[assignment]
    try:
        denied = await ht.herdr_inspect_session_handler(
            host_alias="mac-mini", terminal_id=terminal_id
        )
    finally:
        ht._herdr_cfg = original  # type: ignore[assignment]
    record(
        "workspace allowlist enforced",
        "PASS" if denied.get("status") == "workspace_denied" else "FAIL",
        f"status={denied.get('status')}",
    )

    # 9. Preview mints a real token, mutating nothing ---------------------
    preview, samples = await timed(
        lambda: ht.herdr_preview_action_handler(
            host_alias="mac-mini",
            terminal_id=terminal_id,
            action="# soak probe — never sent",
            summary="phase 4 soak",
        )
    )
    record(
        "preview (no mutation)",
        "PASS" if preview.get("status") == "preview" else "FAIL",
        f"token issued, revision_at_preview={preview.get('revision_at_preview')} "
        f"median {statistics.median(samples):.0f}ms",
    )

    # 10. Human takeover is Fleet-side and durable ------------------------
    claimed = await ht.herdr_claim_human_takeover_handler(
        host_alias="mac-mini", terminal_id=terminal_id
    )
    blocked = await ht.herdr_preview_action_handler(
        host_alias="mac-mini", terminal_id=terminal_id, action="x", summary="y"
    )
    conn = hb.connect()
    try:
        durable = hb.automation_blocked(conn, "mac-mini", terminal_id)
    finally:
        conn.close()
    released = await ht.herdr_release_human_takeover_handler(
        host_alias="mac-mini", terminal_id=terminal_id
    )
    still_live = await ht.herdr_inspect_session_handler(
        host_alias="mac-mini", terminal_id=terminal_id
    )
    record(
        "human takeover blocks + session survives",
        "PASS"
        if claimed.get("status") == "human_takeover"
        and blocked.get("status") == "human_takeover"
        and durable
        and released.get("status") == "automation_permitted"
        and still_live.get("status") == "ok"
        else "FAIL",
        "session still reachable after claim+release",
    )

    # 11. Audit completeness ---------------------------------------------
    conn = hb.connect()
    try:
        trail = hb.recent_audit(conn, "mac-mini", terminal_id, limit=50)
    finally:
        conn.close()
    events = [row["event"] for row in trail]
    needed = {"preview", "human_takeover_claimed", "human_takeover_released"}
    record(
        "audit completeness",
        "PASS" if needed.issubset(set(events)) else "FAIL",
        f"{len(trail)} rows: {sorted(set(events))}",
    )

    # 12. Write path untouched -------------------------------------------
    record(
        "no live pane written",
        "PASS",
        "herdr_request_action never called; only read verbs and Fleet-side state",
    )

    fails = [r for r in RESULTS if r[1] == "FAIL"]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
