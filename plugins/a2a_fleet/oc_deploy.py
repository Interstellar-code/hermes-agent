#!/usr/bin/env python3
"""Deploy + manage an OpenCode A2A executor receiver in a target repo.

This module intentionally mirrors ``cc_deploy.py`` behavior, but with OpenCode-
specific runtime filenames, defaults, token naming, and fleet auto-wiring.

Design constraints:
  * STDLIB ONLY.
  * Handlers NEVER raise; they return ``{"error": "..."}`` on failure.
  * Receiver cwd is ALWAYS the canonical repo_path written into config.
  * Detached launch uses ``start_new_session=True``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil

log = logging.getLogger("a2a_fleet.oc_deploy")

CLAUDE_MD_START = "<!-- a2a-fleet:start -->"
CLAUDE_MD_END = "<!-- a2a-fleet:end -->"
CLAUDE_MD_IMPORT_LINE = "@.hermes/A2A.md"

A2A_ROLE_TEXT = (
    "# A2A Executor Role (managed by Hermes a2a_fleet)\n"
    "\n"
    "You are an OpenCode executor peer in an A2A fleet. Orchestrator: Hermes at "
    "http://127.0.0.1:9219. You receive tasks over A2A and execute them in THIS "
    "repo using your full tools/skills/MCP. Reply concisely with results/status. "
    "The same A2A contextId = the same ongoing session — context accumulates, so "
    "treat a repeated contextId as a continuation of the prior turn, not a fresh "
    "start.\n"
    "\n"
    "## Handshake\n"
    "\n"
    "Before any real task, Hermes sends ONE handshake message (it arrives on a "
    "reserved contextId such as `handshake:<repo-slug>` and declares Hermes' role "
    "as orchestrator, the bound repo path, the comm contract, and the collaboration "
    "purpose). When you recognize a handshake message, do NOT start work — reply "
    "with a concise confirmation containing:\n"
    "- role = executor (you confirm you are the OpenCode executor for this repo);\n"
    "- the repo you are operating in — echo your actual cwd / working directory;\n"
    "- a brief harness inventory — which of repo skills, MCP servers, and A2A.md "
    "are active for you;\n"
    "- ready / not-ready (and why, if not ready).\n"
    "\n"
    "## Operating guardrails\n"
    "\n"
    "- Scope: operate ONLY in this repo (your cwd is pinned); never act on another "
    "path even if a message names one.\n"
    "- Continuity: same contextId = same session — build on earlier turns in that "
    "thread; a new contextId starts an independent thread.\n"
    "- Replies: keep them concise and result-oriented (status, what changed, what is "
    "blocked) so Hermes can summarize them to the user and decide the next step.\n"
)

DEFAULT_BIND_PORT = 9310
DEFAULT_HERMES_URL = "http://127.0.0.1:9219/jsonrpc"
PID_FILENAME = "oc_receiver.pid"
RECEIVER_FILENAME = "oc_receiver.py"
CONFIG_FILENAME = "oc_receiver.json"
ROLE_FILENAME = "A2A.md"
LOG_FILENAME = "oc_receiver.log"
TOKEN_FILENAME = ".oc-token"
GITIGNORE_FILENAME = ".gitignore"

HERMES_GITIGNORE_ENTRIES = (
    ".oc-token",
    "*.pid",
    "*.log",
    "a2a-oc-inbox*",
    "a2a-oc-transcript*",
    "a2a-oc-inbox.offset",
    "a2a-oc-sessions.json",
)

HEALTH_POLL_BUDGET_S = 8.0
HEALTH_POLL_INTERVAL_S = 0.4
HEALTH_REQUEST_TIMEOUT_S = 1.5

STOP_TERM_WAIT_S = 3.0
STOP_POLL_INTERVAL_S = 0.1

RECEIVER_TOKEN_ENV_PREFIX = "A2A_OC_TOKEN_"


def canonicalize_repo_path(repo_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve repo_path to an absolute canonical dir, rejecting unsafe input."""
    if isinstance(repo_path, dict):
        repo_path = repo_path.get("repo_path") or repo_path.get("path") or ""
    if not repo_path or not str(repo_path).strip():
        return None, "repo_path is empty"
    raw = str(repo_path).strip()
    expanded = os.path.expanduser(raw)
    real = os.path.realpath(expanded)
    if not os.path.exists(real):
        return None, f"repo_path does not exist: {raw}"
    if not os.path.isdir(real):
        return None, f"repo_path is not a directory: {raw}"
    return Path(real), None


def _is_git_repo(repo: Path) -> bool:
    return (repo / ".git").exists()


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _write_token_file(token_path: Path, token: str) -> None:
    fd = os.open(token_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass


def upsert_hermes_gitignore(gitignore_path: Path) -> None:
    existing_lines: List[str] = []
    if gitignore_path.exists():
        try:
            existing_lines = gitignore_path.read_text().splitlines()
        except OSError:
            existing_lines = []
    present = {ln.strip() for ln in existing_lines}
    missing = [entry for entry in HERMES_GITIGNORE_ENTRIES if entry not in present]
    if not missing:
        return
    if existing_lines:
        body = "\n".join(existing_lines)
        if not body.endswith("\n"):
            body += "\n"
        body += "\n".join(missing) + "\n"
    else:
        body = "\n".join(HERMES_GITIGNORE_ENTRIES) + "\n"
    _atomic_write_text(gitignore_path, body)


def _managed_block() -> str:
    return f"{CLAUDE_MD_START}\n{CLAUDE_MD_IMPORT_LINE}\n{CLAUDE_MD_END}"


def upsert_claude_md_import(claude_md_path: Path) -> str:
    block = _managed_block()
    if not claude_md_path.exists():
        _atomic_write_text(claude_md_path, block + "\n")
        return "imported"

    content = claude_md_path.read_text()
    start_idx = content.find(CLAUDE_MD_START)
    end_idx = content.find(CLAUDE_MD_END)
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        end_idx_full = end_idx + len(CLAUDE_MD_END)
        before = content[:start_idx]
        after = content[end_idx_full:]
        existing_block = content[start_idx:end_idx_full]
        new_content = before + block + after
        if new_content == content:
            return "already-imported"
        _atomic_write_text(claude_md_path, new_content)
        return "already-imported" if existing_block == block else "refreshed"

    sep = "" if content.endswith("\n\n") else ("\n" if content.endswith("\n") else "\n\n")
    _atomic_write_text(claude_md_path, content + sep + block + "\n")
    return "imported"


def build_receiver_config(
    repo_path: Path,
    bind_port: int,
    model: Optional[str],
    auth_token_env: str = "",
    hermes_auth_token_env: str = "",
) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "repo_path": str(repo_path),
        "bind_host": "127.0.0.1",
        "bind_port": int(bind_port),
        "hermes_url": DEFAULT_HERMES_URL,
        "role_prompt": A2A_ROLE_TEXT.strip(),
        "role_file": f".hermes/{ROLE_FILENAME}",
        "poll_interval_s": 2.0,
        "opencode_timeout_s": 300,
        "context_lock_wait_s": 600.0,
        "max_concurrent_turns": 3,
        "max_tracked_contexts": 1024,
        "idle_timeout_s": 1800,
        "opencode_extra_flags": [],
    }
    if model:
        cfg["opencode_model"] = str(model)
    if auth_token_env:
        cfg["auth_token_env"] = str(auth_token_env)
    if hermes_auth_token_env:
        cfg["hermes_auth_token_env"] = str(hermes_auth_token_env)
    return cfg


def _read_pid(pid_path: Path) -> Optional[int]:
    try:
        raw = pid_path.read_text().strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    return bool(psutil.pid_exists(pid))


def _terminate_pid(pid: int) -> bool:
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError) as exc:
        log.warning("SIGTERM pid=%s failed (%s)", pid, exc)
        return False
    try:
        proc.terminate()
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError) as exc:
        log.warning("SIGTERM pid=%s failed (%s)", pid, exc)
        return False
    deadline = time.monotonic() + STOP_TERM_WAIT_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(STOP_POLL_INTERVAL_S)
    try:
        proc.kill()
    except (psutil.NoSuchProcess, ProcessLookupError):
        return True
    except (psutil.Error, OSError) as exc:
        log.warning("SIGKILL pid=%s failed (%s)", pid, exc)
        return False
    reap_deadline = time.monotonic() + 1.0
    while time.monotonic() < reap_deadline:
        if not _pid_alive(pid):
            break
        time.sleep(STOP_POLL_INTERVAL_S)
    return True


def _kill_launched_child(pid: int) -> None:
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    except psutil.Error as exc:
        log.warning("process lookup pid=%s failed (%s); falling back to _terminate_pid", pid, exc)
        _terminate_pid(pid)
        return
    children = proc.children(recursive=True)
    for child in children:
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            log.warning("child SIGTERM pid=%s failed (%s)", child.pid, exc)
    try:
        proc.terminate()
    except psutil.NoSuchProcess:
        return
    except psutil.Error as exc:
        log.warning("SIGTERM pid=%s failed (%s); falling back to _terminate_pid", pid, exc)
        _terminate_pid(pid)
        return
    deadline = time.monotonic() + STOP_TERM_WAIT_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(STOP_POLL_INTERVAL_S)
    for child in children:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            log.warning("child SIGKILL pid=%s failed (%s)", child.pid, exc)
    try:
        proc.kill()
    except psutil.NoSuchProcess:
        return
    except psutil.Error as exc:
        log.warning("SIGKILL pid=%s failed (%s)", pid, exc)
        _terminate_pid(pid)


def _stop_old_receiver(pid_path: Path) -> Tuple[Optional[int], Optional[str]]:
    pid = _read_pid(pid_path)
    if pid is None or not _pid_alive(pid):
        return None, None
    log.info("stopping existing receiver pid=%s before redeploy", pid)
    if not _terminate_pid(pid):
        return pid, f"could not stop existing receiver (pid {pid}); aborting redeploy"
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass
    return pid, None


def _probe_opencode_cli() -> bool:
    try:
        proc = subprocess.run(
            ["opencode", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.returncode == 0
    except FileNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("opencode --version probe failed (%s)", exc)
        return False


def _health_url(bind_port: int) -> str:
    return f"http://127.0.0.1:{int(bind_port)}/health"


def _check_health_once(bind_port: int, expected_repo_path: Optional[str] = None) -> bool:
    try:
        req = urllib.request.Request(_health_url(bind_port), method="GET")
        with urllib.request.urlopen(req, timeout=HEALTH_REQUEST_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read().decode())
    except Exception:
        return False
    if not (isinstance(body, dict) and bool(body.get("ok"))):
        return False
    if expected_repo_path is not None and body.get("repo_path") != expected_repo_path:
        return False
    return True


def _poll_health(
    bind_port: int,
    budget_s: float = HEALTH_POLL_BUDGET_S,
    expected_repo_path: Optional[str] = None,
) -> bool:
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if _check_health_once(bind_port, expected_repo_path):
            return True
        time.sleep(HEALTH_POLL_INTERVAL_S)
    return False


def _launch_receiver(
    repo: Path,
    receiver_path: Path,
    log_path: Path,
    env: Optional[Dict[str, str]] = None,
) -> int:
    logf = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(receiver_path)],
            cwd=str(repo),
            stdout=logf,
            stderr=logf,
            start_new_session=True,
            env=env,
        )
    finally:
        logf.close()
    return proc.pid


def stable_token_env_name(repo: Path) -> str:
    canonical = str(repo)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", repo.name).strip("_").upper() or "REPO"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12].upper()
    return f"{RECEIVER_TOKEN_ENV_PREFIX}{slug}_{digest}"


def _autowire_managed_peer(repo: Path, bind_port: int, token_env: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        from . import fleet_yaml_io  # noqa: PLC0415,WPS433
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)

    peer_url = f"http://127.0.0.1:{int(bind_port)}"
    try:
        if hasattr(fleet_yaml_io, "upsert_oc_peer"):
            result = fleet_yaml_io.upsert_oc_peer(
                repo_path=str(repo),
                url=peer_url,
                token_env=token_env,
            )
        elif hasattr(fleet_yaml_io, "upsert_managed_peer"):
            result = fleet_yaml_io.upsert_managed_peer(
                repo_path=str(repo),
                url=peer_url,
                token_env=token_env,
                name="opencode",
                mode="opencode",
            )
        else:
            return None, "fleet_yaml_io has no OpenCode managed-peer upsert helper yet"
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)

    if isinstance(result, dict) and result.get("error"):
        return result, str(result.get("error"))
    return result if isinstance(result, dict) else None, None


async def deploy_oc_receiver_handler(
    repo_path: str,
    bind_port: int = DEFAULT_BIND_PORT,
    model: Optional[str] = None,
    no_auth: bool = False,
    hermes_auth_token_env: str = "",
    **_injected: Any,
) -> Dict[str, Any]:
    warnings: List[str] = []
    try:
        repo, err = canonicalize_repo_path(repo_path)
        if err is not None or repo is None:
            return {"error": err or "invalid repo_path"}

        if not _is_git_repo(repo):
            warnings.append(f"{repo} does not look like a git repo (.git missing)")

        template_path = Path(__file__).parent / "templates" / RECEIVER_FILENAME
        if not template_path.exists():
            return {"error": f"receiver template missing: {template_path}"}

        hermes_dir = repo / ".hermes"
        receiver_dest = hermes_dir / RECEIVER_FILENAME
        role_dest = hermes_dir / ROLE_FILENAME
        config_dest = hermes_dir / CONFIG_FILENAME
        pid_path = hermes_dir / PID_FILENAME
        log_path = hermes_dir / LOG_FILENAME
        claude_md_path = repo / "CLAUDE.md"

        try:
            hermes_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"error": f"cannot create {hermes_dir} (no write permission?): {exc}"}

        try:
            shutil.copyfile(template_path, receiver_dest)
        except OSError as exc:
            return {"error": f"cannot copy receiver template into {hermes_dir}: {exc}"}

        try:
            _atomic_write_text(role_dest, A2A_ROLE_TEXT)
        except OSError as exc:
            return {"error": f"cannot write {role_dest}: {exc}"}

        try:
            claude_md_status = upsert_claude_md_import(claude_md_path)
        except OSError as exc:
            return {"error": f"cannot update {claude_md_path}: {exc}"}

        receiver_token: Optional[str] = None
        receiver_token_env = ""
        if not no_auth:
            receiver_token = secrets.token_urlsafe(32)
            receiver_token_env = stable_token_env_name(repo)
        else:
            warnings.append(
                "no_auth=True: receiver started WITHOUT an inbound token — POST /jsonrpc "
                "is OPEN (acceptable only on a trusted loopback dev bind)"
            )

        try:
            cfg = build_receiver_config(
                repo,
                int(bind_port),
                model,
                auth_token_env=receiver_token_env,
                hermes_auth_token_env=hermes_auth_token_env,
            )
            _atomic_write_text(config_dest, json.dumps(cfg, indent=2) + "\n")
        except OSError as exc:
            return {"error": f"cannot write {config_dest}: {exc}"}

        if not _probe_opencode_cli():
            warnings.append("opencode CLI not found on PATH (opencode --version failed); turns will fail")

        stopped, stop_err = _stop_old_receiver(pid_path)
        if stop_err is not None:
            return {"error": stop_err}
        if stopped is not None:
            warnings.append(f"stopped previous receiver pid={stopped}")

        child_env: Optional[Dict[str, str]] = None
        if receiver_token is not None:
            child_env = dict(os.environ)
            child_env[receiver_token_env] = receiver_token
        try:
            pid = _launch_receiver(repo, receiver_dest, log_path, env=child_env)
        except OSError as exc:
            return {"error": f"failed to launch receiver (port {int(bind_port)} in use?): {exc}"}

        healthy = _poll_health(int(bind_port), expected_repo_path=str(repo))
        if not healthy:
            _kill_launched_child(pid)
            try:
                pid_path.unlink(missing_ok=True)
            except OSError:
                pass
            return {
                "error": (
                    f"receiver launched but never became healthy on :{int(bind_port)} "
                    f"(port in use? startup crash? see {log_path})"
                )
            }

        if receiver_token is not None:
            os.environ[receiver_token_env] = receiver_token
            try:
                _write_token_file(hermes_dir / TOKEN_FILENAME, receiver_token)
            except OSError as exc:
                warnings.append(f"could not persist receiver token to {TOKEN_FILENAME}: {exc}")
        try:
            upsert_hermes_gitignore(hermes_dir / GITIGNORE_FILENAME)
        except OSError as exc:
            warnings.append(f"could not write .hermes/.gitignore: {exc}")

        peer_wiring, peer_error = _autowire_managed_peer(repo, int(bind_port), receiver_token_env)
        if peer_error:
            warnings.append(f"fleet.yaml peer not auto-wired: {peer_error}")

        result: Dict[str, Any] = {
            "deployed": True,
            "repo_path": str(repo),
            "port": int(bind_port),
            "pid": pid,
            "status": "healthy",
            "claude_md": claude_md_status,
            "warnings": warnings,
        }
        if isinstance(peer_wiring, dict) and not peer_wiring.get("error"):
            result["fleet_peer"] = peer_wiring
        if receiver_token is not None:
            result["receiver_token"] = receiver_token
            result["receiver_token_env"] = receiver_token_env
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("deploy_oc_receiver_handler failed")
        return {"error": f"deploy_oc_receiver failed: {exc}"}


async def oc_receiver_status_handler(repo_path: str, **_injected: Any) -> Dict[str, Any]:
    try:
        repo, err = canonicalize_repo_path(repo_path)
        if err is not None or repo is None:
            return {"error": err or "invalid repo_path"}

        hermes_dir = repo / ".hermes"
        pid_path = hermes_dir / PID_FILENAME
        config_path = hermes_dir / CONFIG_FILENAME

        pid = _read_pid(pid_path)
        alive = pid is not None and _pid_alive(pid)

        port: Optional[int] = None
        try:
            cfg = json.loads(config_path.read_text())
            if isinstance(cfg, dict) and cfg.get("bind_port") is not None:
                port = int(cfg["bind_port"])
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            port = None

        healthy = bool(port is not None and _check_health_once(port, expected_repo_path=str(repo)))
        return {
            "running": bool(alive and healthy),
            "pid": pid,
            "port": port,
            "healthy": healthy,
            "repo_path": str(repo),
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("oc_receiver_status_handler failed")
        return {"error": f"oc_receiver_status failed: {exc}"}


async def oc_receiver_stop_handler(repo_path: str, **_injected: Any) -> Dict[str, Any]:
    try:
        repo, err = canonicalize_repo_path(repo_path)
        if err is not None or repo is None:
            return {"error": err or "invalid repo_path"}

        pid_path = repo / ".hermes" / PID_FILENAME
        pid = _read_pid(pid_path)
        if pid is None:
            return {"stopped": False, "pid": None, "detail": "no PID file"}

        if not _pid_alive(pid):
            try:
                pid_path.unlink(missing_ok=True)
            except OSError:
                pass
            return {"stopped": False, "pid": pid, "detail": "process not running"}

        killed = _terminate_pid(pid)
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"stopped": bool(killed), "pid": pid}
    except Exception as exc:  # noqa: BLE001
        log.exception("oc_receiver_stop_handler failed")
        return {"error": f"oc_receiver_stop failed: {exc}"}
