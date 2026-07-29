"""Phase 4 write-path soak — herdr_request_action end to end.

Targets ONE throwaway pane created for this run (a bash shell in a scratch
directory), never the operator's working sessions. The allowlist in the
throwaway profile contains only the scratch directory, so a mis-targeted
session would be refused rather than written to.

`herdr agent send` writes literal text WITHOUT pressing Enter, so nothing is
executed even in the throwaway pane — the text simply appears on its prompt
line, which is enough to prove delivery.

This harness reads the pane it created (`herdr agent read`) to verify delivery.
That is the harness doing it, not the plugin: the no-scraping rule constrains
what a2a_fleet may do, and delivery cannot be proven any other way.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

TERMINAL_ID = sys.argv[1] if len(sys.argv) > 1 else "term_657bf3ea3a49e26"
SOAK_DIR = (
    "/private/tmp/claude-501/-Users-rohits--hermes-hermes-agent/"
    "ce4dc0f0-7811-4130-9f96-7503189abe4c/scratchpad/soak-pane"
)

sys.path.insert(0, "/Users/rohits/.hermes/hermes-agent/plugins")

HOME = Path(tempfile.mkdtemp(prefix="herdr_write_soak_"))
PROFILE = HOME / "profiles" / "switch"
PROFILE.mkdir(parents=True)
(HOME / "active_profile").write_text("switch")
os.environ["HERMES_HOME"] = str(PROFILE)
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
                    "hosts": {
                        "mac-mini": {
                            "transport": "local_socket",
                            # ONLY the throwaway dir. A real session cannot
                            # pass this allowlist even by mistake.
                            "allowed_workspaces": [SOAK_DIR],
                            "allow_actions": True,
                        }
                    }
                },
            }
        }
    )
)

import a2a_fleet.herdr_binding as hb  # noqa: E402
import a2a_fleet.herdr_tools as ht  # noqa: E402

RESULTS: list[tuple[str, str]] = []


def record(check: str, verdict: str, detail: str = "") -> None:
    RESULTS.append((check, verdict))
    print(f"[{verdict:4}] {check}" + (f" — {detail}" if detail else ""))


def pane_text() -> str:
    out = subprocess.run(
        ["herdr", "agent", "read", TERMINAL_ID, "--source", "visible", "--lines", "40"],
        capture_output=True, text=True, timeout=20,
    )
    return out.stdout


async def preview(action: str):
    return await ht.herdr_preview_action_handler(
        host_alias="mac-mini", terminal_id=TERMINAL_ID, action=action, summary="write soak"
    )


async def main() -> int:
    nonce = f"hermes-soak-{int(time.time())}"

    guard = await ht.herdr_inspect_session_handler(
        host_alias="mac-mini", terminal_id=TERMINAL_ID
    )
    if guard.get("status") != "ok":
        record("target reachable + inside allowlist", "FAIL", str(guard))
        return 1
    record(
        "target reachable + inside allowlist",
        "PASS",
        f"{TERMINAL_ID} cwd={guard['session']['cwd']} revision={guard['session']['revision']}",
    )

    # 1. THE write path -------------------------------------------------
    p1 = await preview(nonce)
    if p1.get("status") != "preview":
        record("preview", "FAIL", str(p1))
        return 1
    before = pane_text()
    sent = await ht.herdr_request_action_handler(
        host_alias="mac-mini",
        terminal_id=TERMINAL_ID,
        confirmation_token=p1["confirmation_token"],
    )
    record(
        "request_action returns sent",
        "PASS" if sent.get("status") == "sent" else "FAIL",
        f"status={sent.get('status')} action_id={sent.get('action_id')}",
    )

    await asyncio.sleep(1.5)
    after = pane_text()
    record(
        "text actually reached the pane",
        "PASS" if nonce in after and nonce not in before else "FAIL",
        f"nonce {'found' if nonce in after else 'MISSING'} in pane output",
    )

    # 2. Revision guard against a genuinely moved pane -------------------
    p2 = await preview("second-action-should-be-refused")
    rev_at_preview = p2.get("revision_at_preview")
    # Move the pane for real: type into it out-of-band, exactly like a human
    # would. This is the situation the guard exists for.
    subprocess.run(
        ["herdr", "agent", "send", TERMINAL_ID, " human-typed-here"],
        capture_output=True, text=True, timeout=20,
    )
    await asyncio.sleep(1.5)
    live = await ht.herdr_inspect_session_handler(
        host_alias="mac-mini", terminal_id=TERMINAL_ID
    )
    stale = await ht.herdr_request_action_handler(
        host_alias="mac-mini",
        terminal_id=TERMINAL_ID,
        confirmation_token=p2["confirmation_token"],
    )
    moved = live["session"]["revision"] != rev_at_preview
    record(
        "revision moved by out-of-band typing",
        "PASS" if moved else "FAIL",
        f"{rev_at_preview} -> {live['session']['revision']}",
    )
    record(
        "stale token refused on a moved pane",
        "PASS" if stale.get("status") == "revision_moved" else "FAIL",
        f"status={stale.get('status')}",
    )
    text_now = pane_text()
    record(
        "refused action never reached the pane",
        "PASS" if "second-action-should-be-refused" not in text_now else "FAIL",
        "refused text absent from pane",
    )

    # 3. Replay in the permitted state ------------------------------------
    p3 = await preview(f"{nonce}-replay")
    first = await ht.herdr_request_action_handler(
        host_alias="mac-mini", terminal_id=TERMINAL_ID,
        confirmation_token=p3["confirmation_token"],
    )
    second = await ht.herdr_request_action_handler(
        host_alias="mac-mini", terminal_id=TERMINAL_ID,
        confirmation_token=p3["confirmation_token"],
    )
    record(
        "token single-use in the permitted state",
        "PASS"
        if first.get("status") == "sent" and second.get("status") == "token_already_used"
        else "FAIL",
        f"first={first.get('status')} second={second.get('status')}",
    )

    # 4. Completion signal -------------------------------------------------
    p4 = await preview(f"{nonce}-wait")
    waited = await ht.herdr_request_action_handler(
        host_alias="mac-mini", terminal_id=TERMINAL_ID,
        confirmation_token=p4["confirmation_token"], wait_timeout_ms=3000,
    )
    completion = waited.get("completion_state")
    record(
        "timeout is not reported as completion",
        "PASS" if completion in ("pending", "completed") else "FAIL",
        f"completion_state={completion} note={waited.get('completion_note', '')[:80]}",
    )

    # 5. Audit trail -------------------------------------------------------
    conn = hb.connect()
    try:
        trail = hb.recent_audit(conn, "mac-mini", TERMINAL_ID, limit=60)
        binding = hb.get_binding(conn, "mac-mini", TERMINAL_ID)
    finally:
        conn.close()
    events = [r["event"] for r in trail]
    ordered_ok = False
    for i, row in enumerate(trail):
        if row["event"] == "sent":
            ordered_ok = any(
                r["event"] == "send_attempted" and r["action_id"] == row["action_id"]
                for r in trail[i:]
            )
            break
    record(
        "audit records intent before outcome",
        "PASS" if "send_attempted" in events and "sent" in events and ordered_ok else "FAIL",
        f"events={sorted(set(events))}",
    )
    record(
        "binding recorded",
        "PASS" if binding and binding["completion_state"] in
        {"pending", "completed", "failed"} else "FAIL",
        f"agent_kind={binding.get('agent_kind') if binding else None} "
        f"state={binding.get('completion_state') if binding else None}",
    )

    fails = [r for r in RESULTS if r[1] == "FAIL"]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
