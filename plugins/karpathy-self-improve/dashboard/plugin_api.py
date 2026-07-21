"""
FastAPI router for karpathy-self-improve.
Mounted at /api/plugins/karpathy-self-improve/ by web_server._mount_plugin_api_routes().

IMPORTANT: web_server loads this file with spec_from_file_location as a flat
module — NO parent package. Relative imports FAIL. We use sys.path injection
below so absolute imports resolve both here and in tests.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

# Fix imports: add plugin root so _db and _metrics resolve as top-level modules.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent  # plugins/karpathy-self-improve/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

_VERSION = "0.3.0"
_PLUGIN_NAME = "karpathy-self-improve"

# ---------------------------------------------------------------------------
# H-3: Per-route auth dependency
# ---------------------------------------------------------------------------

def _require_auth(request: Request) -> None:
    """Raises 401 if the request is not authenticated.

    Tries to reuse the dashboard's own auth helper.  Falls back to pass in
    test contexts where hermes_cli.web_server is not importable.
    """
    try:
        from hermes_cli.web_server import _is_authenticated  # type: ignore[import]
        if not _is_authenticated(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
    except (ImportError, AttributeError):
        # Test context or standalone mount — accept without auth.
        pass


# Module-level router — web_server looks for exactly this name.
router = APIRouter()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health")
async def health() -> dict:
    try:
        from _db import resolve_db_path
        db_path = resolve_db_path()
        db_exists = db_path.exists()
    except Exception:
        db_path = None
        db_exists = False
    return {
        "ok": True,
        "plugin": _PLUGIN_NAME,
        "version": _VERSION,
        "db_path": str(db_path) if db_path is not None else None,
        "db_exists": db_exists,
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@router.get("/metrics")
async def list_metrics(
    profile: Optional[str] = None,
    limit: int = 100,
) -> JSONResponse:
    if limit < 1 or limit > 1000:
        return JSONResponse({"error": "limit must be between 1 and 1000"}, status_code=400)
    try:
        from _db import get_db
        rows = get_db().list_metrics(profile=profile, limit=limit)
        return JSONResponse({"metrics": rows})
    except Exception as exc:
        log.exception("karpathy /metrics error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/metrics/latest")
async def latest_metrics() -> JSONResponse:
    try:
        from _db import get_db
        rows = get_db().latest_metrics_per_profile()
        return JSONResponse({"metrics": rows})
    except Exception as exc:
        log.exception("karpathy /metrics/latest error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/metrics/collect")
async def collect_metrics(body: dict, _auth: None = Depends(_require_auth)) -> JSONResponse:
    try:
        from _metrics import collect_profile_metrics
        profile = body.get("profile", "")
        if not isinstance(profile, str) or not profile.strip():
            return JSONResponse({"error": "profile is required"}, status_code=400)
        snapshots = collect_profile_metrics(profile=profile)
        return JSONResponse({"collected": len(snapshots), "snapshots": snapshots})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        log.exception("karpathy /metrics/collect error")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------

@router.get("/experiments")
async def list_experiments(
    profile: Optional[str] = None,
    state: Optional[str] = None,
) -> JSONResponse:
    try:
        from _db import get_db
        rows = get_db().list_experiments(profile=profile, state=state)
        return JSONResponse({"experiments": rows})
    except Exception as exc:
        log.exception("karpathy /experiments error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/experiments/{exp_id}")
async def get_experiment(exp_id: int) -> JSONResponse:
    try:
        from _db import get_db
        row = get_db().get_experiment(exp_id)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(row)
    except Exception as exc:
        log.exception("karpathy /experiments/%s error", exp_id)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/experiments")
async def create_experiment(body: dict, _auth: None = Depends(_require_auth)) -> JSONResponse:
    """Manually create a proposal experiment."""
    try:
        from _db import get_db
        from datetime import datetime, timezone
        db = get_db()
        now = datetime.now(timezone.utc).isoformat()
        profile = body.get("profile", "")
        file = body.get("file", "")
        diff = body.get("diff", "")
        rationale = body.get("rationale", "")
        if not profile:
            return JSONResponse({"error": "profile is required"}, status_code=400)

        # H-4: reject dangerous file paths before storing in DB.
        if file and (".." in file or file.startswith("/")):
            return JSONResponse(
                {"error": "file must be a relative path with no '..' components"},
                status_code=400,
            )

        # H-4: validate profile resolves to an existing dir under the profiles root.
        # Use _git_ratchet._PROFILES_ROOT so tests can monkeypatch it.
        from _git_ratchet import _PROFILES_ROOT as _profiles_root  # type: ignore[attr-defined]
        candidate = (_profiles_root / profile).resolve()
        try:
            candidate.relative_to(_profiles_root.resolve())
        except ValueError:
            return JSONResponse(
                {"error": f"profile {profile!r} is not inside {_profiles_root}"},
                status_code=400,
            )
        if not candidate.is_dir():
            return JSONResponse(
                {"error": f"profile directory does not exist: {candidate}"},
                status_code=400,
            )

        exp_id = db.insert_experiment(
            profile=profile,
            file=file,
            state="proposed",
            diff=diff,
            rationale=rationale,
            created_at=now,
            updated_at=now,
        )
        return JSONResponse({"experiment_id": exp_id}, status_code=201)
    except Exception as exc:
        log.exception("karpathy POST /experiments error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/experiments/{exp_id}/approve")
async def approve_experiment(exp_id: int, body: dict, _auth: None = Depends(_require_auth)) -> JSONResponse:
    try:
        from _db import get_db
        from _state_machine import transition
        db = get_db()
        actor = body.get("actor", "")
        exp = db.get_experiment(exp_id)
        if exp is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        transition(db, exp_id, "approved", actor=actor)
        return JSONResponse({"ok": True, "state": "approved"})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("karpathy /experiments/%s/approve error", exp_id)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/experiments/{exp_id}/reject")
async def reject_experiment(exp_id: int, body: dict, _auth: None = Depends(_require_auth)) -> JSONResponse:
    try:
        from _db import get_db
        from _state_machine import transition
        db = get_db()
        actor = body.get("actor", "")
        reason = body.get("reason", "")
        exp = db.get_experiment(exp_id)
        if exp is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        transition(db, exp_id, "rejected", actor=actor, reason=reason)
        return JSONResponse({"ok": True, "state": "rejected"})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("karpathy /experiments/%s/reject error", exp_id)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/experiments/{exp_id}/apply")
async def apply_experiment(exp_id: int, _auth: None = Depends(_require_auth)) -> JSONResponse:
    """approved → live: write the proposed content, commit, store apply_commit_sha."""
    try:
        from _db import get_db
        from datetime import datetime, timezone

        db = get_db()
        exp = db.get_experiment(exp_id)
        if exp is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if exp["state"] != "approved":
            return JSONResponse(
                {"error": f"experiment is in state {exp['state']!r}, must be 'approved' to apply"},
                status_code=422,
            )

        profile_root = exp.get("target_profile_root") or ""
        target_relpath = exp.get("target_relpath") or ""
        diff = exp.get("diff") or ""

        if not profile_root or not target_relpath:
            return JSONResponse(
                {"error": "experiment missing target_profile_root or target_relpath"},
                status_code=422,
            )

        from pathlib import Path as _Path

        target_path = _Path(profile_root) / target_relpath
        original_content = ""
        if target_path.is_file():
            original_content = target_path.read_text(encoding="utf-8", errors="replace")

        # Apply diff to get new content. Raises on failure — return 422
        # BEFORE any DB write so a bad patch never touches state.
        new_content = original_content
        if diff:
            from _proposer import apply_diff_to_text, PatchApplyError
            try:
                new_content = apply_diff_to_text(original_content, diff)
            except PatchApplyError as patch_exc:
                return JSONResponse(
                    {"error": f"failed to apply diff: {patch_exc}"},
                    status_code=422,
                )

        # #173: the DB snapshot row is the rollback source of truth (works on
        # profile dirs that are not git repos); git becomes a best-effort
        # audit trail only. Ordering matters: the DB transaction (snapshot +
        # state=live + transition row) is committed BEFORE the file is
        # written. If we crashed between them, the on-disk file still matches
        # the snapshot's prior_bytes, so a revert is a safe no-op. Writing the
        # file first would risk a live file with no recoverable snapshot if
        # the DB write then failed — exactly the bug this closes.
        import hashlib

        prior_bytes = target_path.read_bytes() if target_path.is_file() else b""
        prior_hash = hashlib.sha256(prior_bytes).hexdigest()
        now = datetime.now(timezone.utc).isoformat()

        db._conn.execute("BEGIN IMMEDIATE")
        try:
            db.insert_snapshot(
                experiment_id=exp_id,
                prior_hash=prior_hash,
                prior_bytes=prior_bytes,
                target_relpath=target_relpath,
                applied_at=now,
                _commit=False,
            )
            db.update_experiment_fields(
                exp_id,
                _commit=False,
                state="live",
                applied_at=now,
                live_sessions_target=10,
                live_takes_effect_at_next_session=1,
                updated_at=now,
            )
            # Self-commits — finalizes the transaction above atomically since
            # nothing committed before it (transition() manages its own
            # BEGIN IMMEDIATE and can't be nested here, so we insert the
            # transition row directly).
            db.insert_state_transition(
                experiment_id=exp_id,
                from_state="approved",
                to_state="live",
                actor="api",
                created_at=now,
            )
        except Exception:
            db._conn.rollback()
            raise

        # Snapshot safely committed — now perform the actual write.
        target_path.write_bytes(new_content.encode("utf-8"))

        # Best-effort audit-trail commit. Never fatal: the snapshot above is
        # the real rollback mechanism, so a git failure here is just a
        # missing history entry, not a broken apply.
        apply_commit_sha = None
        from _git_ratchet import is_git_repo, audit_commit
        if is_git_repo(_Path(profile_root)):
            try:
                audit_result = audit_commit(
                    _Path(profile_root),
                    target_relpath,
                    message=f"feat(karpathy): apply experiment {exp_id}",
                )
                if audit_result.ok:
                    apply_commit_sha = audit_result.commit_sha
                    db.update_experiment_fields(exp_id, apply_commit_sha=apply_commit_sha)
                else:
                    log.warning(
                        "audit-trail commit failed for experiment %d: %s",
                        exp_id, audit_result.error,
                    )
            except Exception as exc:
                log.warning(
                    "audit-trail commit failed; snapshot rollback still safe: %s", exc
                )

        return JSONResponse({
            "ok": True,
            "state": "live",
            "apply_commit_sha": apply_commit_sha,
        })
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("karpathy /experiments/%s/apply error", exp_id)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/experiments/{exp_id}/verify")
async def verify_experiment(exp_id: int, _auth: None = Depends(_require_auth)) -> JSONResponse:
    """live → verified + insert baseline."""
    try:
        from _db import get_db
        from _state_machine import transition
        from datetime import datetime, timezone
        db = get_db()
        exp = db.get_experiment(exp_id)
        if exp is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        transition(db, exp_id, "verified", actor="api")
        now = datetime.now(timezone.utc).isoformat()
        db.insert_baseline(
            profile=exp["profile"],
            file=exp.get("target_relpath", ""),
            commit_sha=exp.get("apply_commit_sha") or "",
            score=exp.get("live_score") or 1.0,
            experiment_id=exp_id,
            created_at=now,
        )
        return JSONResponse({"ok": True, "state": "verified"})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("karpathy /experiments/%s/verify error", exp_id)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/experiments/{exp_id}/revert")
async def revert_experiment(exp_id: int, body: dict, _auth: None = Depends(_require_auth)) -> JSONResponse:
    """live/approved → reverted: revert the apply commit."""
    try:
        from _db import get_db
        from _state_machine import transition
        from _git_ratchet import revert_commit
        from pathlib import Path as _Path
        db = get_db()
        exp = db.get_experiment(exp_id)
        if exp is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        reason = body.get("reason", "")
        apply_commit_sha = exp.get("apply_commit_sha") or ""
        profile_root = exp.get("target_profile_root") or ""

        # #173: the DB snapshot is the source of truth for revert — restore
        # the exact prior bytes, which works even when profile_root is not a
        # git repo. Only pre-#173 rows (apply_commit_sha set, no snapshot)
        # fall back to the legacy git-based revert.
        snap = db.get_snapshot(exp_id)
        if snap is not None:
            if profile_root and snap.get("target_relpath"):
                target_path = _Path(profile_root) / snap["target_relpath"]
                target_path.write_bytes(snap["prior_bytes"])
        elif apply_commit_sha and profile_root:
            revert_result = revert_commit(
                _Path(profile_root),
                apply_commit_sha,
                message=f"chore: revert karpathy experiment {exp_id}: {reason}",
            )
            # H-6: only transition to 'reverted' when git revert actually succeeded.
            if not revert_result.ok:
                log.error("revert_commit failed for experiment %d: %s", exp_id, revert_result.error)
                return JSONResponse(
                    {"error": f"git revert failed: {revert_result.error}"},
                    status_code=500,
                )
        transition(db, exp_id, "reverted", actor="api", reason=reason)
        return JSONResponse({"ok": True, "state": "reverted"})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        log.exception("karpathy /experiments/%s/revert error", exp_id)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/experiments/{exp_id}/history")
async def experiment_history(exp_id: int) -> JSONResponse:
    """Return state transitions + eval runs + scenario results."""
    try:
        from _db import get_db
        db = get_db()
        exp = db.get_experiment(exp_id)
        if exp is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        transitions = db.list_state_transitions(exp_id)
        eval_runs = db.list_eval_runs(exp_id)
        scenario_results = []
        for run in eval_runs:
            results = db.list_scenario_results(run["id"])
            scenario_results.extend(results)
        return JSONResponse({
            "experiment": exp,
            "transitions": transitions,
            "eval_runs": eval_runs,
            "scenario_results": scenario_results,
        })
    except Exception as exc:
        log.exception("karpathy /experiments/%s/history error", exp_id)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

@router.get("/scenarios")
async def list_scenarios(
    profile: Optional[str] = None,
    include_holdout: int = 0,
) -> JSONResponse:
    try:
        from _db import get_db
        db = get_db()
        rows = db.list_scenarios(profile)
        if not include_holdout:
            rows = [r for r in rows if not r.get("holdout")]
        return JSONResponse({"scenarios": rows})
    except Exception as exc:
        log.exception("karpathy GET /scenarios error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/scenarios")
async def create_scenario(body: dict, _auth: None = Depends(_require_auth)) -> JSONResponse:
    try:
        from _db import get_db
        from datetime import datetime, timezone
        import json as _json
        db = get_db()
        now = datetime.now(timezone.utc).isoformat()
        profile = body.get("profile", "")
        name = body.get("name", "")
        if not profile or not name:
            return JSONResponse({"error": "profile and name are required"}, status_code=400)
        checks = body.get("checks", [])
        if isinstance(checks, str):
            try:
                checks = _json.loads(checks)
            except Exception:
                checks = []
        scenario_id = db.insert_scenario(
            profile=profile,
            name=name,
            input=body.get("input", ""),
            checks=checks,
            holdout=1 if body.get("holdout") else 0,
            created_at=now,
        )
        return JSONResponse({"scenario_id": scenario_id}, status_code=201)
    except Exception as exc:
        log.exception("karpathy POST /scenarios error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.delete("/scenarios/{scenario_id}")
async def delete_scenario(scenario_id: int, _auth: None = Depends(_require_auth)) -> JSONResponse:
    try:
        from _db import get_db
        db = get_db()
        deleted = db.delete_scenario(scenario_id)
        if not deleted:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"ok": True})
    except Exception as exc:
        log.exception("karpathy DELETE /scenarios/%s error", scenario_id)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Propose (async trigger)
# ---------------------------------------------------------------------------

@router.post("/propose")
async def trigger_propose(body: dict, _auth: None = Depends(_require_auth)) -> JSONResponse:
    """Trigger the proposer for a profile (202 Accepted — runs synchronously for now)."""
    try:
        from _db import get_db
        from _proposer import propose_for_profile
        db = get_db()
        profile = body.get("profile", "")
        if not profile:
            return JSONResponse({"error": "profile is required"}, status_code=400)
        rows = db.list_experiments(profile=profile)
        target_relpath = "SOUL.md"
        # Default the profile root to the profile's own directory so the very
        # first proposal can resolve its target file. Previously this defaulted
        # to "." (the dashboard process CWD), which only ever worked once a
        # prior experiment had populated target_profile_root — a bootstrap
        # deadlock that made /propose 500 on a fresh profile. The "default"
        # profile lives at the ~/.hermes root, not ~/.hermes/profiles/default.
        # Use the canonical resolver so HERMES_HOME is respected in multi-profile
        # and hermes-switch setups.
        try:
            from hermes_cli.profiles import get_profile_dir as _get_profile_dir
            profile_root = str(_get_profile_dir(profile))
        except Exception:
            log.debug("hermes_cli.profiles unavailable; falling back to Path.home() logic")
            _hermes_home = Path.home() / ".hermes"
            if profile == "default":
                profile_root = str(_hermes_home)
            else:
                profile_root = str(_hermes_home / "profiles" / profile)
        if rows:
            exp = rows[0]
            target_relpath = exp.get("target_relpath") or target_relpath
            profile_root = exp.get("target_profile_root") or profile_root
        # Allow an explicit target file for non-standard profile layouts.
        if isinstance(body.get("target_relpath"), str) and body["target_relpath"]:
            target_relpath = body["target_relpath"]
        target_path = (Path(profile_root) / target_relpath).resolve()
        try:
            target_path.relative_to(Path(profile_root).resolve())
        except ValueError:
            return JSONResponse({"error": "target_relpath escapes the profile root"}, status_code=400)
        # Resolve real gateway-backed model kwargs from config.
        try:
            from _wiring import resolve_propose_kwargs
            propose_kwargs = resolve_propose_kwargs(profile)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        result = propose_for_profile(
            db=db,
            profile=profile,
            target_relpath=target_relpath,
            profile_root=profile_root,
            **propose_kwargs,
        )
        if result.skipped:
            return JSONResponse({"skipped": True, "reason": result.skip_reason}, status_code=200)
        if not result.ok:
            return JSONResponse({"error": result.error}, status_code=500)
        return JSONResponse(
            {"experiment_id": result.experiment_id, "offline_score": result.offline_score},
            status_code=202,
        )
    except Exception as exc:
        log.exception("karpathy POST /propose error")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Pause / Resume profiles
# ---------------------------------------------------------------------------

def _resolve_target(db, profile: str) -> tuple[str, str]:
    """Resolve (target_relpath, profile_root) for *profile*.

    Default target_relpath is "system_prompt.md"; profile_root resolves via
    hermes_cli.profiles.get_profile_dir with a ~/.hermes fallback (the "default"
    profile lives at the ~/.hermes root, not ~/.hermes/profiles/default). The
    newest experiment row overrides both when it carries target_relpath /
    target_profile_root.
    """
    target_relpath = "system_prompt.md"
    try:
        from hermes_cli.profiles import get_profile_dir as _get_profile_dir
        profile_root = str(_get_profile_dir(profile))
    except Exception:
        log.debug("hermes_cli.profiles unavailable; falling back to Path.home() logic")
        _hermes_home = Path.home() / ".hermes"
        if profile == "default":
            profile_root = str(_hermes_home)
        else:
            profile_root = str(_hermes_home / "profiles" / profile)
    rows = db.list_experiments(profile=profile)
    if rows:
        exp = rows[0]
        target_relpath = exp.get("target_relpath") or target_relpath
        profile_root = exp.get("target_profile_root") or profile_root
    # config.yaml is the declared intent — plugins.karpathy_self_improve.profiles
    # .<profile>.{target_relpath,profile_root}. It wins over the default and any
    # experiment override so a configured-but-never-run profile resolves right.
    try:
        from hermes_cli.config import load_config, cfg_get  # type: ignore[import]
        cfg = load_config()
        cfg_target = cfg_get(
            cfg, "plugins", "karpathy_self_improve", "profiles", profile,
            "target_relpath", default=None,
        )
        cfg_root = cfg_get(
            cfg, "plugins", "karpathy_self_improve", "profiles", profile,
            "profile_root", default=None,
        )
        if cfg_target:
            target_relpath = str(cfg_target)
        if cfg_root:
            profile_root = str(cfg_root)
    except Exception:
        log.debug("karpathy _resolve_target: config.yaml unreadable", exc_info=True)
    return target_relpath, profile_root


@router.get("/profiles/{profile}")
async def get_profile_status(profile: str, _auth: None = Depends(_require_auth)) -> JSONResponse:
    """Full config + status surface for *profile*.

    Never 500s on missing optional data — returns null/0/empty per field. Lets
    the SwitchUI tell a configured profile from an unbootstrapped one.
    """
    try:
        from _db import get_db
        db = get_db()

        paused = db.is_paused(profile)
        target_relpath, profile_root = _resolve_target(db, profile)
        try:
            configured = (Path(profile_root) / target_relpath).exists()
        except Exception:
            configured = False

        proposer_model = None
        judge_model = None
        try:
            from _wiring import resolve_propose_kwargs
            _kw = resolve_propose_kwargs(profile)
            proposer_model = _kw.get("proposer_model")
            judge_model = _kw.get("judge_model")
        except Exception:
            pass

        experiments = db.list_experiments(profile=profile)
        experiment_counts = {
            s: 0 for s in ("proposed", "approved", "live", "verified", "reverted", "rejected")
        }
        live_sessions_target = None
        last_verification_at = None
        for exp in experiments:
            st = exp.get("state")
            if st in experiment_counts:
                experiment_counts[st] += 1
            if live_sessions_target is None and exp.get("live_sessions_target") is not None:
                live_sessions_target = exp.get("live_sessions_target")
            if last_verification_at is None and st == "verified":
                last_verification_at = exp.get("verified_at") or exp.get("updated_at")
        last_proposal_at = experiments[0].get("created_at") if experiments else None
        if live_sessions_target is None:
            try:
                from hermes_cli.config import load_config, cfg_get  # type: ignore[import]
                live_sessions_target = cfg_get(
                    load_config(), "plugins", "karpathy_self_improve", "profiles",
                    profile, "live_sessions_target", default=None,
                )
            except Exception:
                pass

        scenario_counts = {"train": 0, "holdout": 0}
        for s in db.list_scenarios(profile):
            scenario_counts["holdout" if s.get("holdout") else "train"] += 1

        baselines = db.list_baselines(profile)
        latest_baseline_score = baselines[0].get("score") if baselines else None

        metrics = db.list_metrics(profile=profile, limit=1)
        last_collection_at = metrics[0].get("captured_at") if metrics else None

        return JSONResponse({
            "profile": profile,
            "paused": paused,
            "configured": configured,
            "target_relpath": target_relpath,
            "profile_root": profile_root,
            "proposer_model": proposer_model,
            "judge_model": judge_model,
            "live_sessions_target": live_sessions_target,
            "scenario_counts": scenario_counts,
            "experiment_counts": experiment_counts,
            "latest_baseline_score": latest_baseline_score,
            "last_collection_at": last_collection_at,
            "last_proposal_at": last_proposal_at,
            "last_verification_at": last_verification_at,
        })
    except Exception as exc:
        log.exception("karpathy GET /profiles/%s error", profile)
        return JSONResponse({"error": str(exc)}, status_code=500)

@router.post("/profiles/{profile}/pause")
async def pause_profile(profile: str, _auth: None = Depends(_require_auth)) -> JSONResponse:
    try:
        from _db import get_db
        get_db().set_paused(profile, True)
        return JSONResponse({"ok": True, "profile": profile, "paused": True})
    except Exception as exc:
        log.exception("karpathy /profiles/%s/pause error", profile)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/profiles/{profile}/resume")
async def resume_profile(profile: str, _auth: None = Depends(_require_auth)) -> JSONResponse:
    try:
        from _db import get_db
        get_db().set_paused(profile, False)
        return JSONResponse({"ok": True, "profile": profile, "paused": False})
    except Exception as exc:
        log.exception("karpathy /profiles/%s/resume error", profile)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

@router.get("/baselines")
async def list_baselines(profile: Optional[str] = None) -> JSONResponse:
    try:
        from _db import get_db
        db = get_db()
        if profile:
            rows = db.list_baselines(profile)
        else:
            cur = db._conn.execute(
                "SELECT * FROM baselines ORDER BY created_at DESC"
            )
            rows = [dict(r) for r in cur.fetchall()]
        return JSONResponse({"baselines": rows})
    except Exception as exc:
        log.exception("karpathy GET /baselines error")
        return JSONResponse({"error": str(exc)}, status_code=500)
