from __future__ import annotations


def build_dry_run_preview(action: str, preview: dict, *, requires_confirmation: bool = False, confirm_token: str | None = None) -> dict:
    payload = {
        "success": True,
        "dry_run": True,
        "action": action,
        "preview": preview,
        "apply_hint": "Re-run this tool with dry_run=false to apply.",
    }
    if requires_confirmation:
        payload["requires_confirmation"] = True
        payload["confirm_token"] = confirm_token
        payload["apply_hint"] = "Re-run this tool with dry_run=false and confirm_token to apply."
    return payload
