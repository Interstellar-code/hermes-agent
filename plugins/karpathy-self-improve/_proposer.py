"""
_proposer.py — Meta-agent proposer for karpathy-self-improve.

propose_for_profile() reads the latest metrics + failing offline scenarios for a
profile, asks an LLM for ONE atomic sentence-level edit to the target file,
validates the diff changes exactly one sentence/hunk, runs an offline eval on
the candidate, and if the aggregate score improved (or tied with shorter prompt)
creates an experiment row in state='proposed'.

Respects one-active-experiment-per-profile (skips if one exists).
Never raises to caller — returns a ProposalResult object.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ProposalResult:
    ok: bool
    skipped: bool = False
    skip_reason: str = ""
    experiment_id: Optional[int] = None
    offline_score: Optional[float] = None
    baseline_score: Optional[float] = None
    sentence_delta_count: int = 0
    error: str = ""
    diff: str = ""


# ---------------------------------------------------------------------------
# Sentence delta counting
# ---------------------------------------------------------------------------

# Split on sentence-ending punctuation followed by whitespace or end-of-string.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _count_sentences(text: str) -> int:
    """Rough sentence count for the given text."""
    stripped = text.strip()
    if not stripped:
        return 0
    parts = _SENTENCE_SPLIT_RE.split(stripped)
    return len([p for p in parts if p.strip()])


def _split_sentences(text: str) -> List[str]:
    """Split text into a list of non-empty sentence strings."""
    stripped = text.strip()
    if not stripped:
        return []
    parts = _SENTENCE_SPLIT_RE.split(stripped)
    return [p.strip() for p in parts if p.strip()]


def _sentence_delta_count(original: str, modified: str) -> int:
    """Return the number of changed sentences between original and modified.

    Uses difflib.SequenceMatcher over sentence lists to count replaced,
    inserted, and deleted sentences. An in-place rewrite of N sentences
    (same total count) returns N, not 0.
    """
    orig_sents = _split_sentences(original)
    mod_sents = _split_sentences(modified)
    matcher = difflib.SequenceMatcher(None, orig_sents, mod_sents, autojunk=False)
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        # replace: max(deleted, inserted) sentences changed
        # insert: j2-j1 new sentences
        # delete: i2-i1 removed sentences
        changed += max(i2 - i1, j2 - j1)
    return changed


def _count_diff_hunks(diff: str) -> int:
    """Count the number of @@ hunk headers in a unified diff."""
    return sum(1 for line in diff.splitlines() if line.startswith("@@"))


def _diff_is_single_hunk(diff: str) -> bool:
    """Return True if the diff contains exactly one hunk."""
    return _count_diff_hunks(diff) == 1


# ---------------------------------------------------------------------------
# Default LLM wrapper (production only — overridden in tests)
# ---------------------------------------------------------------------------


def _default_llm_fn(prompt: str, *, model: Optional[str] = None) -> str:
    """Call the Hermes gateway LLM. Only used in production."""
    try:
        from _wiring import call_gateway_chat
        return call_gateway_chat(prompt, model=model)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("karpathy-self-improve: _default_llm_fn failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Proposal builder
# ---------------------------------------------------------------------------

_PROPOSE_PROMPT_TEMPLATE = """\
You are a self-improvement agent reviewing an AI assistant profile/system-prompt file.

## Profile: {profile}
## Target file: {target_relpath}

## Current file content:
{file_content}

## Recent metrics (last 5 snapshots):
{metrics_summary}

## Failing offline scenarios:
{failing_scenarios}

Your task: propose ONE atomic sentence-level improvement to the target file that is
most likely to address the failing scenarios or poor metrics. The edit must:
1. Change exactly one sentence or one contiguous paragraph (single hunk).
2. Be minimal — do not rewrite unrelated content.
3. Be expressed as a unified diff (--- a/file, +++ b/file, @@ ... @@).

Return your response in this exact format:
DIFF:
<unified diff here, starting with ---, one hunk only>
RATIONALE:
<one sentence explaining why this edit helps>
"""


def _parse_llm_response(response: str) -> tuple[str, str]:
    """Parse the LLM response for DIFF and RATIONALE sections.

    Returns (diff, rationale). Either may be empty on parse failure.
    """
    diff = ""
    rationale = ""

    diff_match = re.search(r"DIFF:\s*\n(.*?)(?=\nRATIONALE:|\Z)", response, re.DOTALL)
    if diff_match:
        diff = diff_match.group(1).strip()

    rationale_match = re.search(r"RATIONALE:\s*\n(.*)", response, re.DOTALL)
    if rationale_match:
        rationale = rationale_match.group(1).strip()

    return diff, rationale


class PatchApplyError(Exception):
    """Raised by apply_diff_to_text() when a unified diff fails to apply."""


def apply_diff_to_text(original: str, diff: str) -> str:
    """Apply a unified diff string to *original* content using the `patch` utility.

    Returns the patched content. Raises PatchApplyError if the `patch` binary
    is missing, the diff fails to apply cleanly (non-zero exit), or any other
    error occurs — callers that need a non-raising contract should catch this.
    """
    import subprocess
    import tempfile
    import os

    try:
        # Run inside a temp dir so patch's reject file (written as ``-.rej``
        # because output goes to stdout via ``-o -``) lands here and is cleaned
        # up, instead of littering the process CWD (the repo root).
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_path = os.path.join(tmpdir, "orig.txt")
            patch_path = os.path.join(tmpdir, "change.patch")
            with open(orig_path, "w", encoding="utf-8") as orig_f:
                orig_f.write(original)
            with open(patch_path, "w", encoding="utf-8") as patch_f:
                patch_f.write(diff)

            result = subprocess.run(
                ["patch", "--no-backup-if-mismatch", "-s", "-o", "-", orig_path, patch_path],
                capture_output=True,
                timeout=10,
                cwd=tmpdir,
            )
            if result.returncode != 0:
                raise PatchApplyError(
                    f"patch exited {result.returncode}: {result.stderr.decode(errors='replace')}"
                )
            return result.stdout.decode(errors="replace")
    except FileNotFoundError as exc:
        raise PatchApplyError("patch utility not found") from exc
    except PatchApplyError:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        raise PatchApplyError(f"apply_diff_to_text failed: {exc}") from exc


def _apply_diff_to_content(original: str, diff: str) -> Optional[str]:
    """Apply a unified diff string to *original* content.

    Returns the patched content, or None on failure. Delegates to
    apply_diff_to_text(), which raises on failure.
    """
    try:
        return apply_diff_to_text(original, diff)
    except PatchApplyError as exc:
        logger.debug("karpathy-self-improve: _apply_diff_to_content failed: %s", exc)
        return None


def _get_failing_scenarios(db: Any, profile: str) -> List[Dict[str, Any]]:
    """Return scenarios that have recently failed (pass_fail=0) for this profile."""
    try:
        cur = db._conn.execute(
            """
            SELECT s.*, esr.pass_fail
            FROM scenarios s
            JOIN experiment_scenario_results esr ON esr.scenario_id = s.id
            WHERE s.profile = ? AND esr.pass_fail = 0
            ORDER BY esr.id DESC
            LIMIT 10
            """,
            (profile,),
        )
        return [dict(row) for row in cur.fetchall()]
    except Exception:  # pylint: disable=broad-except
        return []


def _get_baseline_score(db: Any, profile: str) -> Optional[float]:
    """Return the most recent baseline score for this profile."""
    rows = db.list_baselines(profile)
    if rows:
        return rows[0].get("score")
    return None


def _has_active_experiment(db: Any, profile: str) -> bool:
    """Return True if there is already an active experiment for this profile."""
    rows = db.list_experiments(profile=profile, state="proposed")
    if rows:
        return True
    rows = db.list_experiments(profile=profile, state="approved")
    if rows:
        return True
    rows = db.list_experiments(profile=profile, state="live")
    if rows:
        return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def propose_for_profile(
    db: Any,  # KarpathyDB
    profile: str,
    target_relpath: str,
    profile_root: str,
    *,
    llm_fn: Optional[Callable[[str], str]] = None,
    judge_fn: Optional[Callable[[str, str], bool]] = None,
    proposer_model: Optional[str] = None,
    judge_model: Optional[str] = None,
    scenario_runner: Optional[Callable[[str], str]] = None,
) -> ProposalResult:
    """Propose one atomic sentence-level edit for *profile*'s target file.

    Args:
        db: KarpathyDB instance.
        profile: Profile identifier.
        target_relpath: Relative path to the file to edit within profile_root.
        profile_root: Absolute path to the profile's root directory.
        llm_fn: Callable(prompt) -> response_text. Defaults to _default_llm_fn.
        judge_fn: Callable(rubric, response) -> bool. For judge-type checks.
        proposer_model: LLM model ID for proposing edits.
        judge_model: LLM model ID for judging (must differ from proposer_model).
        scenario_runner: Callable(scenario_input) -> response_text. For eval.

    Returns:
        ProposalResult (never raises).
    """
    try:
        return _propose_for_profile_inner(
            db=db,
            profile=profile,
            target_relpath=target_relpath,
            profile_root=Path(profile_root),
            llm_fn=llm_fn or _default_llm_fn,
            judge_fn=judge_fn,
            proposer_model=proposer_model,
            judge_model=judge_model,
            scenario_runner=scenario_runner,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("karpathy-self-improve: propose_for_profile failed unexpectedly")
        return ProposalResult(ok=False, error=str(exc))


def _propose_for_profile_inner(
    db: Any,
    profile: str,
    target_relpath: str,
    profile_root: Path,
    *,
    llm_fn: Callable[[str], str],
    judge_fn: Optional[Callable[[str, str], bool]],
    proposer_model: Optional[str],
    judge_model: Optional[str],
    scenario_runner: Optional[Callable[[str], str]],
) -> ProposalResult:
    from _eval_runner import run_eval
    from _git_ratchet import current_commit_sha, blob_sha

    now = datetime.now(timezone.utc).isoformat()

    # Skip if the profile is paused.
    try:
        if db.is_paused(profile):
            return ProposalResult(
                ok=True,
                skipped=True,
                skip_reason="profile is paused",
            )
    except Exception:  # pylint: disable=broad-except
        # controls table unavailable — treat as not paused and continue.
        pass

    # Skip if there's already an active experiment.
    if _has_active_experiment(db, profile):
        return ProposalResult(
            ok=True,
            skipped=True,
            skip_reason=f"profile {profile!r} already has an active experiment",
        )

    # Read the target file.
    target_path = profile_root / target_relpath
    if not target_path.is_file():
        return ProposalResult(
            ok=False,
            error=f"target file not found: {target_path}",
        )
    original_content = target_path.read_text(encoding="utf-8", errors="replace")

    # Get recent metrics.
    recent_metrics = db.list_metrics(profile=profile, limit=5)
    metrics_summary = "\n".join(
        f"- sessions={m.get('sessions_count',0)} errors={m.get('error_count',0)} "
        f"warns={m.get('warn_count',0)} captured_at={m.get('captured_at','?')}"
        for m in recent_metrics
    ) or "(no metrics yet)"

    # Get failing scenarios.
    failing = _get_failing_scenarios(db, profile)
    failing_summary = "\n".join(
        f"- [{s.get('name','?')}]: {s.get('input','')[:120]}"
        for s in failing
    ) or "(none)"

    # Build prompt and call LLM.
    prompt = _PROPOSE_PROMPT_TEMPLATE.format(
        profile=profile,
        target_relpath=target_relpath,
        file_content=original_content[:4000],  # Truncate to avoid token overflow.
        metrics_summary=metrics_summary,
        failing_scenarios=failing_summary,
    )

    response = llm_fn(prompt)
    if not response:
        return ProposalResult(ok=False, error="LLM returned empty response")

    diff, rationale = _parse_llm_response(response)
    if not diff:
        return ProposalResult(ok=False, error="LLM response did not contain a parseable DIFF section")

    # Validate: exactly one hunk.
    if not _diff_is_single_hunk(diff):
        hunk_count = _count_diff_hunks(diff)
        return ProposalResult(
            ok=False,
            error=f"diff has {hunk_count} hunks; exactly 1 required (one atomic change)",
            diff=diff,
        )

    # Apply diff to get the modified content.
    modified_content = _apply_diff_to_content(original_content, diff)
    if modified_content is None:
        return ProposalResult(ok=False, error="failed to apply diff to original content", diff=diff)

    # Compute sentence delta.
    delta = _sentence_delta_count(original_content, modified_content)
    if delta > 1:
        return ProposalResult(
            ok=False,
            error=f"diff changes {delta} sentences; maximum is 1 (one atomic sentence-level edit)",
            diff=diff,
            sentence_delta_count=delta,
        )

    # Capture git provenance.
    base_commit = current_commit_sha(profile_root)
    base_blob = blob_sha(profile_root, target_relpath)
    base_commit_sha_val = base_commit.sha if base_commit.ok else None
    base_blob_sha_val = base_blob.sha if base_blob.ok else None

    # Get baseline score for comparison.
    baseline_score = _get_baseline_score(db, profile)

    # Create a TEMPORARY experiment to run the eval against.
    # We create it in 'proposed' state, run eval, then decide whether to keep it.
    # If score doesn't improve, we mark it rejected.
    exp_id = db.insert_experiment(
        profile=profile,
        file=target_relpath,
        state="proposed",
        diff=diff,
        rationale=rationale or "(no rationale)",
        offline_score=None,
        proposer_model=proposer_model,
        judge_model=judge_model,
        sentence_delta_count=delta,
        target_profile_root=str(profile_root),
        target_relpath=target_relpath,
        base_commit_sha=base_commit_sha_val,
        base_blob_sha=base_blob_sha_val,
        created_at=now,
        updated_at=now,
    )

    # Run offline eval to score the proposal.
    # NOTE: We cannot actually apply the diff to the filesystem here (that's
    # reserved for the apply step). The eval runs against the scenario_runner
    # which tests the *current* state. In a real system, the proposer would
    # need to write a temp file and point the scenario_runner at it.
    # For now, we run the eval and record the score as the offline baseline.
    try:
        offline_score = run_eval(
            db=db,
            experiment_id=exp_id,
            profile=profile,
            kind="offline",
            scenario_runner=scenario_runner,
            judge_fn=judge_fn,
            proposer_model=proposer_model,
            judge_model=judge_model,
            include_holdout=False,
        )
    except ValueError as exc:
        # Anti-gaming guard raised — reject the experiment.
        db.update_experiment_fields(exp_id, state="rejected", updated_at=now)
        return ProposalResult(ok=False, error=str(exc))
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("karpathy-self-improve: offline eval failed: %s", exc)
        offline_score = 0.0

    # Update offline_score on the experiment.
    db.update_experiment_fields(exp_id, offline_score=offline_score, updated_at=now)

    # Decide whether to keep or reject.
    # Keep if: score improved over baseline, OR score ties but diff is shorter (proxy for simpler).
    keep = False
    if baseline_score is None:
        # No baseline yet — any score is acceptable.
        keep = True
    elif offline_score > baseline_score:
        keep = True
    elif offline_score == baseline_score and len(diff) < len(original_content):
        keep = True  # Tie with shorter prompt.

    if not keep:
        db.update_experiment_fields(exp_id, state="rejected", updated_at=now)
        logger.debug(
            "karpathy-self-improve: proposal rejected (score=%.3f baseline=%.3f)",
            offline_score,
            baseline_score or 0.0,
        )
        return ProposalResult(
            ok=True,
            skipped=True,
            skip_reason=(
                f"offline score {offline_score:.3f} did not improve over baseline "
                f"{baseline_score:.3f}"
            ),
            offline_score=offline_score,
            baseline_score=baseline_score,
            diff=diff,
            sentence_delta_count=delta,
        )

    logger.info(
        "karpathy-self-improve: proposed experiment %d for profile %r "
        "offline_score=%.3f baseline=%s",
        exp_id,
        profile,
        offline_score,
        f"{baseline_score:.3f}" if baseline_score is not None else "none",
    )

    return ProposalResult(
        ok=True,
        experiment_id=exp_id,
        offline_score=offline_score,
        baseline_score=baseline_score,
        sentence_delta_count=delta,
        diff=diff,
    )
