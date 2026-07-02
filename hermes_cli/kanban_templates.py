"""Kanban board templates — storage, validation, substitution, and instantiation.

Templates live at ``<kanban root>/templates/<slug>/template.yaml``.  The module
is stdlib + PyYAML only; it composes :mod:`hermes_cli.kanban_db` for all DB
work and never touches the DB directly except for the raw column updates that
``create_task`` does not expose (``workflow_template_id``, ``current_step_key``,
``model_override``).

Public API (consumed verbatim by REST, CLI, agent tools, and cron phases):

    SLUG_RE            — compiled regex for slug validation
    MAX_TEMPLATE_BYTES — 64 KiB cap enforced in save_template
    MAX_OPEN_TASKS_CAP — 200 default guardrail for instantiate()

    TemplateError          — base exception (message safe to surface)
    TemplateNotFound       — slug not found on disk
    TemplateValidationError — schema / variable / cycle error
    InstantiationRefused   — open-task guardrail cap hit

    templates_root()       -> Path
    list_templates()       -> list[dict]
    load_template(slug)    -> dict
    save_template(slug, yaml_text) -> dict
    delete_template(slug)  -> None
    validate_template(data) -> dict
    substitute(text, variables) -> str
    instantiate(slug, variables, board_slug, auto_dispatch, tenant) -> dict
    save_board_as_template(board_slug, template_slug, name, reset_status) -> dict
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

import hermes_cli.kanban_db as _kdb

log = logging.getLogger("hermes_cli.kanban_templates")

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

SLUG_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
"""Compiled regex for template and board slugs.  Intentionally stricter than
the board-slug regex (no underscores) to match the template filesystem
convention and the issue spec."""

MAX_TEMPLATE_BYTES: int = 64 * 1024
"""Maximum raw YAML size accepted by :func:`save_template`."""

MAX_OPEN_TASKS_CAP: int = 200
"""Default guardrail: refuse instantiation when the target board already has
more than this many non-done, non-archived tasks."""

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TemplateError(Exception):
    """Base exception for all template errors.  Message is safe to surface."""


class TemplateNotFound(TemplateError):
    """Raised when a template slug does not exist on disk."""


class TemplateValidationError(TemplateError):
    """Raised when a template fails schema or semantic validation."""


class InstantiationRefused(TemplateError):
    """Raised when the open-task guardrail cap is exceeded on the target board."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def templates_root() -> Path:
    """Return ``<kanban root>/templates``, creating it on demand.

    Respects the same root-resolution chain as :mod:`hermes_cli.kanban_db`
    (``HERMES_KANBAN_HOME`` → ``get_default_hermes_root()``).
    """
    root = _kdb.kanban_home() / "kanban" / "templates"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _template_dir(slug: str) -> Path:
    """Return ``<templates root>/<slug>/``.  Caller must validate slug first."""
    return templates_root() / slug


def _template_file(slug: str) -> Path:
    return _template_dir(slug) / "template.yaml"


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------


def _validate_slug(slug: str, label: str = "template slug") -> str:
    """Validate *slug* against :data:`SLUG_RE`.  Returns the slug unchanged.

    Raises :class:`TemplateValidationError` on failure — intentionally NOT
    ``ValueError`` so callers that catch ``TemplateError`` handle it cleanly.
    """
    if not SLUG_RE.match(slug):
        raise TemplateValidationError(
            f"invalid {label} {slug!r}: must match ^[a-z0-9][a-z0-9-]{{0,63}}$"
        )
    return slug


# ---------------------------------------------------------------------------
# list / load / save / delete
# ---------------------------------------------------------------------------


def list_templates() -> list[dict]:
    """Scan templates root; return a list of summary dicts.

    Each entry contains::

        {
          "slug": str,
          "name": str,
          "description": str | None,
          "color": str | None,
          "variables": list[dict],
          "has_recurrence": bool,
          "path": str,          # absolute path to template.yaml
        }

    Invalid YAML files are skipped with a warning.
    """
    root = templates_root()
    results: list[dict] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        slug = child.name
        if not SLUG_RE.match(slug):
            continue
        tf = child / "template.yaml"
        if not tf.exists():
            continue
        try:
            raw = tf.read_text(encoding="utf-8")
            data = yaml.safe_load(raw) or {}
            results.append({
                "slug": slug,
                "name": data.get("name", slug),
                "description": data.get("description"),
                "color": data.get("color"),
                "variables": data.get("variables") or [],
                "has_recurrence": bool(data.get("recurrence")),
                "path": str(tf),
            })
        except Exception as exc:
            log.warning("skipping malformed template %s: %s", slug, exc)
    return results


def load_template(slug: str) -> dict:
    """Load, parse, and validate a template by *slug*.

    Returns the normalised template dict.

    Raises:
        TemplateNotFound:        slug directory or file absent.
        TemplateValidationError: YAML invalid or schema check fails.
    """
    _validate_slug(slug)
    tf = _template_file(slug)
    if not tf.exists():
        raise TemplateNotFound(f"template {slug!r} not found")
    try:
        raw = tf.read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateNotFound(f"template {slug!r} not readable: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise TemplateValidationError(f"template {slug!r} contains invalid YAML: {exc}") from exc
    return validate_template(data)


def _validate_recurrence_cron(slug: str, recurrence: Any) -> None:
    """Validate ``recurrence.cron`` with croniter when the recurrence is enabled.

    Only validates when ``enabled`` is truthy — disabled recurrence does not
    need a parseable expression (it may be a placeholder).

    Raises :class:`TemplateValidationError` for an invalid/unparseable expression.
    """
    if not isinstance(recurrence, dict):
        return
    if not recurrence.get("enabled"):
        return
    cron_expr = recurrence.get("cron")
    if not cron_expr:
        raise TemplateValidationError(
            f"template {slug!r}: recurrence.enabled is true but recurrence.cron is absent"
        )
    try:
        from croniter import croniter as _croniter
        _croniter(str(cron_expr))
    except ImportError:
        raise TemplateValidationError(
            "recurrence.cron validation requires the 'croniter' package; "
            "install it with: pip install croniter"
        )
    except Exception as exc:
        raise TemplateValidationError(
            f"template {slug!r}: invalid recurrence.cron {cron_expr!r}: {exc}"
        ) from exc


def save_template(slug: str, yaml_text: str) -> dict:
    """Validate *slug* and *yaml_text*, then atomically write to disk.

    Enforces:
    * :data:`SLUG_RE` on slug.
    * :data:`MAX_TEMPLATE_BYTES` size cap (before parsing).
    * ``yaml.safe_load`` only.
    * Full schema validation via :func:`validate_template`.
    * ``recurrence.cron`` validated with croniter when ``enabled`` is true.

    Writes to ``templates/<slug>/template.yaml`` atomically (write-then-rename).

    Returns the parsed and normalised template dict.

    Raises :class:`TemplateValidationError` for any validation failure.
    """
    _validate_slug(slug)
    if isinstance(yaml_text, (bytes, bytearray)):
        yaml_text = yaml_text.decode("utf-8")
    if len(yaml_text.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise TemplateValidationError(
            f"template YAML exceeds {MAX_TEMPLATE_BYTES} bytes "
            f"({len(yaml_text.encode('utf-8'))} bytes supplied)"
        )
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise TemplateValidationError(f"invalid YAML: {exc}") from exc
    if data is None:
        raise TemplateValidationError("template YAML is empty")
    normalised = validate_template(data)

    # Validate recurrence.cron before writing — bad cron expressions are
    # rejected at save time, not at scheduler dispatch time.
    _validate_recurrence_cron(slug, normalised.get("recurrence"))

    target_dir = _template_dir(slug)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "template.yaml"
    tmp_file = target_dir / "template.yaml.tmp"
    try:
        tmp_file.write_text(yaml_text, encoding="utf-8")
        tmp_file.replace(target_file)
    except OSError as exc:
        tmp_file.unlink(missing_ok=True)
        raise TemplateValidationError(f"could not write template {slug!r}: {exc}") from exc
    log.info("saved template %r to %s", slug, target_file)
    sync_recurrence(slug, normalised)
    return normalised


def delete_template(slug: str) -> None:
    """Delete template *slug* from disk.

    Raises :class:`TemplateNotFound` when the slug does not exist.
    Raises :class:`TemplateValidationError` for an invalid slug.
    """
    _validate_slug(slug)
    td = _template_dir(slug)
    if not td.exists():
        raise TemplateNotFound(f"template {slug!r} not found")
    tf = td / "template.yaml"
    tf.unlink(missing_ok=True)
    try:
        td.rmdir()
    except OSError:
        # Directory not empty (e.g. extra files); leave it, only remove yaml.
        log.debug("template directory %s not empty after yaml removal; left in place", td)
    log.info("deleted template %r", slug)
    _remove_recurrence_job(slug)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_VALID_TASK_STATUSES = {"todo", "ready"}
_VALID_PRIORITIES = {"low", "normal", "high", "urgent", "critical"}
_PRIORITY_INT = {"low": -1, "normal": 0, "high": 1, "urgent": 2, "critical": 3}


def validate_template(data: dict) -> dict:
    """Validate and normalise a parsed template dict.

    Checks:
    * ``schema: 1`` present.
    * ``tasks`` non-empty list.
    * Task ``key`` values unique across the template.
    * ``links`` reference existing keys.
    * No cycles in links (BFS reachability).
    * ``variables`` well-formed (each has ``key``; ``required`` defaults False).

    Returns the normalised dict (in-place modifications; same object returned).

    Raises :class:`TemplateValidationError` for any violation.
    """
    if not isinstance(data, dict):
        raise TemplateValidationError("template must be a YAML mapping")

    if data.get("schema") != 1:
        raise TemplateValidationError(
            f"unsupported schema version {data.get('schema')!r}; expected 1"
        )

    tasks_raw = data.get("tasks")
    if not tasks_raw or not isinstance(tasks_raw, list):
        raise TemplateValidationError("template must have a non-empty 'tasks' list")

    # Validate and normalise tasks
    seen_keys: set[str] = set()
    for i, task in enumerate(tasks_raw):
        if not isinstance(task, dict):
            raise TemplateValidationError(f"tasks[{i}] must be a mapping")
        key = task.get("key")
        if not key or not isinstance(key, str):
            raise TemplateValidationError(f"tasks[{i}] missing required string 'key'")
        if not key.strip():
            raise TemplateValidationError(f"tasks[{i}] has empty 'key'")
        key = key.strip()
        task["key"] = key
        if key in seen_keys:
            raise TemplateValidationError(f"duplicate task key {key!r}")
        seen_keys.add(key)

        title = task.get("title")
        if not title or not isinstance(title, str) or not title.strip():
            raise TemplateValidationError(f"task {key!r} missing required 'title'")

        # Normalise status
        status = task.get("status", "todo")
        if status not in _VALID_TASK_STATUSES:
            raise TemplateValidationError(
                f"task {key!r} status {status!r} must be one of {sorted(_VALID_TASK_STATUSES)}"
            )
        task["status"] = status

        # Normalise priority to int
        prio = task.get("priority", 0)
        if isinstance(prio, str):
            if prio not in _PRIORITY_INT:
                raise TemplateValidationError(
                    f"task {key!r} priority {prio!r} unknown; use one of "
                    f"{sorted(_PRIORITY_INT)} or an integer"
                )
            task["priority"] = _PRIORITY_INT[prio]
        elif isinstance(prio, int):
            task["priority"] = prio
        else:
            raise TemplateValidationError(
                f"task {key!r} priority must be a string label or integer, got {prio!r}"
            )

        # Validate optional positive-integer fields
        for _field in ("max_runtime_seconds", "goal_max_turns"):
            v = task.get(_field)
            if v is None:
                continue
            if isinstance(v, bool):
                raise TemplateValidationError(
                    f"task {key!r} {_field} must be a positive integer, got {v!r}"
                )
            if isinstance(v, int):
                if v <= 0:
                    raise TemplateValidationError(
                        f"task {key!r} {_field} must be a positive integer, got {v!r}"
                    )
                task[_field] = v
            elif isinstance(v, str) and v.isdigit():
                coerced = int(v)
                if coerced <= 0:
                    raise TemplateValidationError(
                        f"task {key!r} {_field} must be a positive integer, got {v!r}"
                    )
                task[_field] = coerced
            else:
                raise TemplateValidationError(
                    f"task {key!r} {_field} must be a positive integer, got {v!r}"
                )

        # Validate optional scheduled_at (epoch int, relative offset, or
        # {{placeholder}}). Resolution happens at instantiation.
        _validate_scheduled_at_format(key, task.get("scheduled_at"))

    # Validate links
    links_raw = data.get("links") or []
    if not isinstance(links_raw, list):
        raise TemplateValidationError("'links' must be a list")
    for i, lnk in enumerate(links_raw):
        if not isinstance(lnk, (list, tuple)) or len(lnk) != 2:
            raise TemplateValidationError(
                f"links[{i}] must be a two-element list [parent_key, child_key]"
            )
        parent_key, child_key = str(lnk[0]), str(lnk[1])
        if parent_key not in seen_keys:
            raise TemplateValidationError(
                f"links[{i}] parent key {parent_key!r} not found in tasks"
            )
        if child_key not in seen_keys:
            raise TemplateValidationError(
                f"links[{i}] child key {child_key!r} not found in tasks"
            )
        if parent_key == child_key:
            raise TemplateValidationError(
                f"links[{i}] self-link on task {parent_key!r}"
            )

    # Cycle detection via DFS on the link graph
    adj: dict[str, list[str]] = {k: [] for k in seen_keys}
    for lnk in links_raw:
        parent_key, child_key = str(lnk[0]), str(lnk[1])
        adj[parent_key].append(child_key)

    def _has_cycle(start: str) -> bool:
        visited: set[str] = set()
        stack: set[str] = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            stack.add(node)
            for nb in adj.get(node, []):
                if nb not in visited:
                    if dfs(nb):
                        return True
                elif nb in stack:
                    return True
            stack.discard(node)
            return False

        return dfs(start)

    for key in seen_keys:
        if _has_cycle(key):
            raise TemplateValidationError(
                f"template links contain a cycle reachable from task {key!r}"
            )

    # Validate variables
    variables_raw = data.get("variables") or []
    if not isinstance(variables_raw, list):
        raise TemplateValidationError("'variables' must be a list")
    seen_var_keys: set[str] = set()
    for i, var in enumerate(variables_raw):
        if not isinstance(var, dict):
            raise TemplateValidationError(f"variables[{i}] must be a mapping")
        vkey = var.get("key")
        if not vkey or not isinstance(vkey, str):
            raise TemplateValidationError(f"variables[{i}] missing required string 'key'")
        vkey = vkey.strip()
        var["key"] = vkey
        if vkey in seen_var_keys:
            raise TemplateValidationError(f"duplicate variable key {vkey!r}")
        seen_var_keys.add(vkey)
        # Normalise required flag
        var.setdefault("required", False)

    return data


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def substitute(text: str, variables: dict[str, Any]) -> str:
    """Replace ``{{key}}`` placeholders in *text* with values from *variables*.

    Unknown placeholders (keys not present in *variables*) are left intact.
    Builtin keys (``date``, ``instance_id``) are expected to be pre-injected
    by the caller (see :func:`instantiate`).

    This is plain ``str.replace`` — never ``eval``, never shell expansion.
    Only string values are substituted; non-string values are str()-coerced.
    """

    def _replace(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in variables:
            return str(variables[key])
        return m.group(0)  # leave unknown placeholders intact

    return _PLACEHOLDER_RE.sub(_replace, text)


# ---------------------------------------------------------------------------
# scheduled_at — deferred-dispatch start time (template-side)
# ---------------------------------------------------------------------------
#
# A task may carry an optional ``scheduled_at`` that defers its dispatch
# (see kanban_db: the claim/dispatch path skips tasks until the time is due).
# In a template it accepts three forms:
#
#   * a relative offset string ``+<n><unit>`` (``s``/``m``/``h``/``d``/``w``),
#     e.g. ``+2h``, ``+30m``, ``+1d`` — resolved to ``now + delta`` at
#     instantiation, so a saved template stays portable across runs;
#   * an absolute unix-epoch integer (or all-digit string) — passed through;
#   * a ``{{var}}`` placeholder — substituted first, then resolved as above.
#
# Absolute live timestamps are deliberately NOT captured by
# save_board_as_template (a fixed wall-clock time is stale on the next
# instantiation — the same non-portable treatment as workspace fields).

_REL_SCHED_RE = re.compile(r"^\+(\d+)([smhdw])$")
_SCHED_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _validate_scheduled_at_format(key: str, value: Any) -> None:
    """Validate a template task's ``scheduled_at`` *format* only.

    Does not resolve relative offsets (that happens at instantiation, when
    "now" is known). Strings containing ``{{placeholders}}`` are accepted
    verbatim and re-validated after substitution.

    Raises :class:`TemplateValidationError` for malformed values.
    """
    if value is None:
        return
    if isinstance(value, bool):
        raise TemplateValidationError(
            f"task {key!r} scheduled_at must be a positive epoch int or "
            f"relative offset like '+2h', got {value!r}"
        )
    if isinstance(value, int):
        if value <= 0:
            raise TemplateValidationError(
                f"task {key!r} scheduled_at must be a positive integer, got {value!r}"
            )
        return
    if isinstance(value, str):
        v = value.strip()
        if _PLACEHOLDER_RE.search(v):
            return  # deferred to instantiation, re-validated after substitute()
        if _REL_SCHED_RE.match(v):
            return
        if v.isdigit() and int(v) > 0:
            return
        raise TemplateValidationError(
            f"task {key!r} scheduled_at {value!r} is invalid; use a positive "
            f"epoch integer or a relative offset like '+2h' / '+30m' / '+1d'"
        )
    raise TemplateValidationError(
        f"task {key!r} scheduled_at must be a positive epoch int or relative "
        f"offset like '+2h', got {value!r}"
    )


def _resolve_scheduled_at(value: Any, now_epoch: int) -> Optional[int]:
    """Resolve a (post-substitution) ``scheduled_at`` value to a unix epoch.

    * ``None`` -> ``None`` (no scheduling constraint).
    * relative ``+<n><unit>`` -> ``now_epoch + delta``.
    * absolute int or all-digit string -> that integer.

    Raises :class:`TemplateValidationError` for malformed values (mirrors
    :func:`_validate_scheduled_at_format`, but after placeholders are gone).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise TemplateValidationError(
            f"scheduled_at must be a positive epoch int or relative offset, got {value!r}"
        )
    if isinstance(value, int):
        if value <= 0:
            raise TemplateValidationError(
                f"scheduled_at must be a positive integer, got {value!r}"
            )
        return value
    if isinstance(value, str):
        v = value.strip()
        m = _REL_SCHED_RE.match(v)
        if m:
            return now_epoch + int(m.group(1)) * _SCHED_UNIT_SECONDS[m.group(2)]
        if v.isdigit() and int(v) > 0:
            return int(v)
        raise TemplateValidationError(
            f"scheduled_at {value!r} is invalid after substitution; use a "
            f"positive epoch integer or a relative offset like '+2h'"
        )
    raise TemplateValidationError(
        f"scheduled_at must be a positive epoch int or relative offset, got {value!r}"
    )


# ---------------------------------------------------------------------------
# Instance-ID generation
# ---------------------------------------------------------------------------

def _new_instance_id() -> str:
    """Return a short unique instance id: ``YYYYMMDDHHmmss`` + 4 hex chars."""
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
    import os as _os
    suffix = _os.urandom(2).hex()  # 4 hex chars
    return f"{ts}{suffix}"


# ---------------------------------------------------------------------------
# Board slug uniquification
# ---------------------------------------------------------------------------

def _uniquify_board_slug(slug: str) -> str:
    """Return *slug* if the board does not exist, else append -2, -3, … until free."""
    if not _kdb.board_exists(slug):
        return slug
    for n in range(2, 1000):
        candidate = f"{slug}-{n}"
        # Validate candidate against board slug rules (trimmed to 64 chars)
        if len(candidate) > 64:
            break
        if not _kdb.board_exists(candidate):
            return candidate
    raise TemplateValidationError(
        f"could not find a free board slug starting with {slug!r}"
    )


# ---------------------------------------------------------------------------
# instantiate()
# ---------------------------------------------------------------------------

def instantiate(
    slug: str,
    variables: Optional[dict[str, Any]] = None,
    board_slug: Optional[str] = None,
    auto_dispatch: bool = False,
    tenant: Optional[str] = None,
    _cap: int = MAX_OPEN_TASKS_CAP,  # internal param for testing guardrail
) -> dict:
    """Instantiate template *slug* onto a Kanban board.

    Steps
    -----
    1. Load and validate the template.
    2. Merge variables: defaults → caller → builtins (``date``, ``instance_id``).
       Missing required variables → :class:`TemplateValidationError`.
    3. Resolve (or create) the target board.
    4. Guardrail: count non-done/non-archived tasks; > *_cap* → :class:`InstantiationRefused`.
    5. For each template task: call :func:`hermes_cli.kanban_db.create_task`
       with ``idempotency_key``, then patch ``workflow_template_id``,
       ``current_step_key``, and ``model_override`` via direct UPDATE.
    6. Wire links via :func:`hermes_cli.kanban_db.link_tasks`.
    7. Emit ``task_events`` kind ``template_instantiated`` on the first task.

    Parameters
    ----------
    slug:
        Template slug to instantiate.
    variables:
        Caller-supplied variable values (merged over template defaults).
    board_slug:
        Explicit target board.  When *None*, the template's ``board.slug``
        field (after substitution) is used; if the template has no board
        section, raises :class:`TemplateValidationError`.
    auto_dispatch:
        When *False* (default), all seeded task statuses are forced to
        ``todo`` regardless of template task status or parent state.
        When *True*, tasks with no parents that have template status ``ready``
        are left as ``ready`` (dispatcher picks them up on its next tick).
    tenant:
        Optional tenant tag passed to ``create_task``.

    Returns
    -------
    dict with keys: ``board_slug``, ``instance_id``, ``task_ids`` (key→id),
    ``created`` (count), ``skipped`` (count, idempotency dedupes).
    """
    # 1. Load
    tmpl = load_template(slug)

    # 2. Merge variables
    merged: dict[str, Any] = {}
    for var in (tmpl.get("variables") or []):
        if "default" in var:
            merged[var["key"]] = var["default"]
    if variables:
        merged.update(variables)

    # Builtins
    instance_id = _new_instance_id()
    merged.setdefault("date", datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"))
    merged["instance_id"] = instance_id

    # Check required
    for var in (tmpl.get("variables") or []):
        if var.get("required") and var["key"] not in merged:
            raise TemplateValidationError(
                f"required variable {var['key']!r} not supplied for template {slug!r}"
            )

    # 3. Resolve target board slug
    if board_slug is None:
        board_def = tmpl.get("board") or {}
        raw_slug = board_def.get("slug") or board_def.get("name")
        if not raw_slug:
            raise TemplateValidationError(
                f"template {slug!r} has no board.slug; supply board_slug explicitly"
            )
        raw_slug = substitute(str(raw_slug), merged).strip().lower()
        # Sanitize: replace any char not in [a-z0-9-] with a hyphen,
        # collapse runs, strip leading/trailing hyphens.
        raw_slug = re.sub(r"[^a-z0-9-]+", "-", raw_slug).strip("-")
        raw_slug = re.sub(r"-{2,}", "-", raw_slug)
        raw_slug = raw_slug[:64]
        # Validate sanitized slug
        if not raw_slug or not SLUG_RE.match(raw_slug):
            raise TemplateValidationError(
                f"substituted board slug (sanitized: {raw_slug!r}) is invalid; "
                "must match ^[a-z0-9][a-z0-9-]{0,63}$"
            )
        board_slug = _uniquify_board_slug(raw_slug)
    else:
        _validate_slug(board_slug, label="board_slug")

    # Create board if it does not exist
    board_name: Optional[str] = None
    board_def = tmpl.get("board") or {}
    if board_def.get("name"):
        board_name = substitute(str(board_def["name"]), merged)
    if not _kdb.board_exists(board_slug):
        _kdb.create_board(
            board_slug,
            name=board_name or board_slug,
            color=tmpl.get("color"),
        )
        log.info("created board %r for template instance %s@%s", board_slug, slug, instance_id)

    # 4. Guardrail
    conn = _kdb.connect(board=board_slug)
    try:
        open_count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done', 'archived')"
        ).fetchone()[0]
        if open_count > _cap:
            raise InstantiationRefused(
                f"board {board_slug!r} has {open_count} open tasks "
                f"(cap {_cap}); refusing instantiation of template {slug!r}"
            )

        tasks_spec = tmpl["tasks"]
        links_spec = tmpl.get("links") or []

        workflow_template_id = f"{slug}@{instance_id}"

        # 5. Create tasks (within one logical board-level transaction per task
        #    — create_task manages its own write_txn internally; we group the
        #    post-INSERT UPDATEs immediately after each create_task call so
        #    the board DB stays consistent even on partial failure).
        task_ids: dict[str, str] = {}  # key -> db task id
        created = 0
        skipped = 0

        for task_spec in tasks_spec:
            key = task_spec["key"]
            raw_title = task_spec["title"]
            raw_body = task_spec.get("body") or ""
            title = substitute(raw_title, merged)
            body = substitute(raw_body, merged) if raw_body else None

            # assignee and skills are identifiers — no substitution
            assignee = task_spec.get("assignee") or None
            skills_list = task_spec.get("skills") or None
            priority: int = task_spec.get("priority", 0)
            max_retries = task_spec.get("max_retries") or None
            goal_mode: bool = bool(task_spec.get("goal_mode", False))
            model_override: Optional[str] = task_spec.get("model_override") or None
            max_runtime_seconds = task_spec.get("max_runtime_seconds")
            goal_max_turns = task_spec.get("goal_max_turns")

            # scheduled_at: substitute {{vars}} first, then resolve a
            # relative offset (+2h) against "now" or pass an absolute epoch.
            raw_scheduled = task_spec.get("scheduled_at")
            if isinstance(raw_scheduled, str):
                raw_scheduled = substitute(raw_scheduled, merged)
            scheduled_at = _resolve_scheduled_at(raw_scheduled, int(time.time()))

            idempotency_key = f"{slug}:{instance_id}:{key}"

            # create_task determines status from parents; we override to todo
            # after creation when auto_dispatch=False (or when the task has
            # parents — it will already be todo from create_task's logic).
            task_id = _kdb.create_task(
                conn,
                title=title,
                body=body,
                assignee=assignee,
                skills=skills_list,
                priority=priority,
                max_retries=int(max_retries) if max_retries is not None else None,
                goal_mode=goal_mode,
                max_runtime_seconds=int(max_runtime_seconds) if max_runtime_seconds is not None else None,
                goal_max_turns=int(goal_max_turns) if goal_max_turns is not None else None,
                scheduled_at=scheduled_at,
                idempotency_key=idempotency_key,
                tenant=tenant,
                board=board_slug,
            )

            # Detect idempotency deduplication: if a task with this key already
            # existed, create_task returns the existing id.
            already_existed = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ? AND id != ? "
                "AND status != 'archived'",
                (idempotency_key, task_id),
            ).fetchone()
            # Simpler: check whether the task was just created by verifying
            # created_at is very recent (within 2s).
            row_created_at = conn.execute(
                "SELECT created_at FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            just_created = (
                row_created_at is not None
                and int(time.time()) - row_created_at[0] < 2
            )

            if task_id in task_ids.values():
                # create_task returned a duplicate id within this run — skip
                skipped += 1
            elif not just_created:
                skipped += 1
            else:
                created += 1

            task_ids[key] = task_id

            # Patch template-specific columns not exposed by create_task
            with _kdb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET workflow_template_id = ?, current_step_key = ? "
                    "WHERE id = ?",
                    (workflow_template_id, key, task_id),
                )
                if model_override:
                    conn.execute(
                        "UPDATE tasks SET model_override = ? WHERE id = ?",
                        (model_override, task_id),
                    )
                # auto_dispatch=False: force todo regardless of template status
                # or natural ready/todo assignment from create_task.
                if not auto_dispatch:
                    conn.execute(
                        "UPDATE tasks SET status = 'todo' "
                        "WHERE id = ? AND status IN ('ready', 'running')",
                        (task_id,),
                    )
                else:
                    # auto_dispatch=True: honour template task status for tasks
                    # without parents; tasks with parents are already todo/ready
                    # from create_task's logic.
                    desired_status = task_spec.get("status", "todo")
                    if desired_status == "ready":
                        conn.execute(
                            "UPDATE tasks SET status = 'ready' "
                            "WHERE id = ? AND status = 'todo' "
                            "AND NOT EXISTS ("
                            "  SELECT 1 FROM task_links WHERE child_id = ?"
                            ")",
                            (task_id, task_id),
                        )

        # 6. Wire links (parent → child)
        # NOTE: create_task commits each task individually (write_txn).
        # On link failure we must clean up the already-committed tasks so
        # no partial/edge-missing board is left behind.
        for lnk in links_spec:
            parent_key, child_key = str(lnk[0]), str(lnk[1])
            parent_id = task_ids.get(parent_key)
            child_id = task_ids.get(child_key)
            if parent_id and child_id:
                try:
                    _kdb.link_tasks(conn, parent_id, child_id)
                except Exception as exc:
                    # Roll back by archiving every task we just created so
                    # the board is left in a consistent (empty) state.
                    created_ids = list(task_ids.values())
                    if created_ids:
                        with _kdb.write_txn(conn):
                            placeholders = ",".join("?" * len(created_ids))
                            conn.execute(
                                f"UPDATE tasks SET status = 'archived' "
                                f"WHERE id IN ({placeholders})",
                                created_ids,
                            )
                    raise TemplateError(
                        f"failed to wire link {parent_key}->{child_key}: {exc}"
                    ) from exc

        # 7. Emit template_instantiated event on the first task
        if task_ids:
            first_task_id = task_ids[tasks_spec[0]["key"]]
            with _kdb.write_txn(conn):
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, NULL, ?, ?, ?)",
                    (
                        first_task_id,
                        "template_instantiated",
                        json.dumps({
                            "template": slug,
                            "instance_id": instance_id,
                            "task_count": len(task_ids),
                        }, ensure_ascii=False),
                        int(time.time()),
                    ),
                )

    finally:
        conn.close()

    log.info(
        "instantiated template %r as %s on board %r: %d created, %d skipped",
        slug, instance_id, board_slug, created, skipped,
    )
    return {
        "board_slug": board_slug,
        "instance_id": instance_id,
        "task_ids": task_ids,
        "created": created,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# save_board_as_template()
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Convert *text* to a lowercase kebab-case slug (best-effort)."""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    s = s[:64] or "task"
    return s


def save_board_as_template(
    board_slug: str,
    template_slug: str,
    name: Optional[str] = None,
    reset_status: bool = True,
) -> dict:
    """Snapshot a live board as a reusable template.

    Reads all non-archived tasks and their links from the board DB.
    Strips runtime fields (pids, runs, heartbeats, results, claim_lock,
    workspace_path, created_at, etc.).  Generates template-local ``key``
    values from slugified task titles (uniquified with -2/-3 suffixes).

    Status handling (``reset_status``):

    * ``True`` (default): every task status → ``todo``.
    * ``False``: preserves only the authoring distinction — live status
      ``ready`` → ``ready``; every other live status (``running``,
      ``blocked``, ``done``, etc.) → ``todo``.  This keeps the template
      valid (only ``todo``/``ready`` are legal template statuses) while
      capturing which root tasks were dispatch-ready.

    Writes ``template.yaml`` atomically and returns the parsed template dict.

    Raises :class:`TemplateValidationError` for invalid slugs.
    """
    _validate_slug(board_slug, label="board_slug")
    _validate_slug(template_slug)

    conn = _kdb.connect(board=board_slug)
    try:
        rows = conn.execute(
            "SELECT id, title, body, assignee, priority, skills, "
            "max_retries, goal_mode, goal_max_turns, max_runtime_seconds, "
            "model_override, status "
            "FROM tasks WHERE status != 'archived' "
            "ORDER BY created_at ASC"
        ).fetchall()

        # Build key mapping: task_id -> template key
        used_keys: set[str] = set()
        id_to_key: dict[str, str] = {}
        tasks_out: list[dict] = []

        for row in rows:
            base = _slugify(row["title"] or "task")
            if not base or not SLUG_RE.match(base[0] + "x"):
                base = "task"
            # Ensure key starts with alphanumeric
            if not base[0].isalnum():
                base = "t" + base
            # Uniquify
            candidate = base
            n = 2
            while candidate in used_keys:
                candidate = f"{base}-{n}"
                n += 1
            used_keys.add(candidate)
            id_to_key[row["id"]] = candidate

            skills_raw = row["skills"]
            try:
                skills_parsed = json.loads(skills_raw) if skills_raw else None
            except (json.JSONDecodeError, TypeError):
                skills_parsed = None

            task_entry: dict[str, Any] = {
                "key": candidate,
                "title": row["title"] or "Untitled",
            }
            if row["body"]:
                task_entry["body"] = row["body"]
            if row["assignee"]:
                task_entry["assignee"] = row["assignee"]
            if skills_parsed:
                task_entry["skills"] = skills_parsed
            prio = row["priority"] or 0
            if prio != 0:
                task_entry["priority"] = prio
            if row["max_retries"] is not None:
                task_entry["max_retries"] = row["max_retries"]
            if row["goal_mode"]:
                task_entry["goal_mode"] = True
            if row["model_override"]:
                task_entry["model_override"] = row["model_override"]
            if row["max_runtime_seconds"] is not None:
                task_entry["max_runtime_seconds"] = row["max_runtime_seconds"]
            if row["goal_max_turns"] is not None:
                task_entry["goal_max_turns"] = row["goal_max_turns"]
            # scheduled_at is intentionally NOT captured: a live task's value
            # is an absolute epoch that would be stale (in the past) on the
            # next instantiation. Authors express deferral portably by adding
            # a relative offset (e.g. scheduled_at: "+2h") to the template YAML.
            task_entry["status"] = "todo" if reset_status else ("ready" if row["status"] == "ready" else "todo")
            tasks_out.append(task_entry)

        # Fetch links
        if rows:
            all_ids = [r["id"] for r in rows]
            link_rows = conn.execute(
                "SELECT parent_id, child_id FROM task_links "
                "WHERE parent_id IN ({ph}) AND child_id IN ({ph}) "
                "ORDER BY parent_id, child_id".format(
                    ph=",".join("?" * len(all_ids))
                ),
                all_ids + all_ids,
            ).fetchall()
        else:
            link_rows = []

        links_out: list[list[str]] = []
        for lr in link_rows:
            pk = id_to_key.get(lr["parent_id"])
            ck = id_to_key.get(lr["child_id"])
            if pk and ck:
                links_out.append([pk, ck])

    finally:
        conn.close()

    board_meta = _kdb.read_board_metadata(board_slug)
    display_name = name or board_meta.get("name") or board_slug

    tmpl_dict: dict[str, Any] = {
        "schema": 1,
        "slug": template_slug,
        "name": display_name,
        "tasks": tasks_out,
    }
    if board_meta.get("color"):
        tmpl_dict["color"] = board_meta["color"]
    if board_meta.get("description"):
        tmpl_dict["description"] = board_meta["description"]
    if links_out:
        tmpl_dict["links"] = links_out
    tmpl_dict["on_instantiate"] = {"auto_dispatch": False}

    yaml_text = yaml.dump(tmpl_dict, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return save_template(template_slug, yaml_text)


# ---------------------------------------------------------------------------
# Recurrence sync helpers (appended — other agents do not touch this section)
# ---------------------------------------------------------------------------


def _recurrence_job_id(slug: str) -> str:
    """Return the deterministic cron job ID for a template's recurrence job."""
    return f"kanban-template-{slug}"


def _remove_recurrence_job(slug: str) -> None:
    """Remove the recurrence cron job for *slug* if it exists.

    No-ops silently when the cron subsystem is unavailable or the job does
    not exist.  Called by :func:`delete_template`.
    """
    try:
        from cron.jobs import remove_job as _remove_job
    except ImportError:
        log.debug("cron subsystem unavailable; skipping recurrence job removal for %r", slug)
        return
    try:
        job_id = _recurrence_job_id(slug)
        _remove_job(job_id)
        log.debug("removed recurrence cron job %r for template %r", job_id, slug)
    except Exception as exc:
        log.debug("could not remove recurrence job for template %r: %s", slug, exc)


def sync_recurrence(slug: str, template: dict) -> None:
    """Upsert or remove the cron job that drives recurrence for *template*.

    Behaviour:
    * ``recurrence.cron`` present and ``recurrence.enabled`` truthy
      → upsert job id ``kanban-template-{slug}`` with the given cron schedule,
        type ``kanban_board_from_template``, and payload derived from *template*.
    * ``recurrence`` absent, disabled, or ``recurrence.cron`` missing
      → remove the job id if it exists (no-op when absent).

    Guards:
    * Imports :mod:`cron.jobs` lazily — if the module is unavailable (e.g.
      the module is used in a standalone context), logs a debug message and
      returns without raising.
    * Any exception from the cron subsystem is caught, logged at WARNING
      level, and swallowed so a cron-side failure never breaks template saves.

    Called automatically by :func:`save_template` (one-line addition at its
    end) and :func:`delete_template` (via :func:`_remove_recurrence_job`).
    """
    recurrence = template.get("recurrence") if isinstance(template, dict) else None

    # Determine whether recurrence should be active.
    if not isinstance(recurrence, dict) or not recurrence.get("enabled"):
        _remove_recurrence_job(slug)
        return

    cron_expr = recurrence.get("cron")
    if not cron_expr:
        _remove_recurrence_job(slug)
        return

    try:
        from cron.jobs import upsert_kanban_template_job as _upsert
    except ImportError:
        log.debug("cron subsystem unavailable; skipping recurrence sync for template %r", slug)
        return

    job_id = _recurrence_job_id(slug)

    # Payload: pull variables + auto_dispatch from template if present.
    on_inst = template.get("on_instantiate") or {}
    variables: dict = {}
    if isinstance(on_inst, dict) and on_inst.get("variables"):
        variables = dict(on_inst["variables"])
    auto_dispatch: bool = bool(
        (isinstance(on_inst, dict) and on_inst.get("auto_dispatch"))
    )

    try:
        _upsert(
            job_id=job_id,
            schedule_expr=str(cron_expr),
            template_slug=slug,
            variables=variables if variables else None,
            auto_dispatch=auto_dispatch,
        )
        log.info(
            "synced recurrence cron job %r for template %r (schedule: %s)",
            job_id, slug, cron_expr,
        )
    except Exception as exc:
        log.warning(
            "could not sync recurrence cron job for template %r: %s", slug, exc
        )
