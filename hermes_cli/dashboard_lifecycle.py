"""Cross-process startup guards for the Hermes dashboard."""

from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from hermes_constants import get_hermes_home

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


DASHBOARD_CONFLICT_EXIT_CODE = 98


class DashboardStartupConflict(RuntimeError):
    """A second dashboard or occupied listen address prevents startup."""

    exit_code = DASHBOARD_CONFLICT_EXIT_CODE


def _lock_path() -> Path:
    return get_hermes_home() / "dashboard.lock"


def _mutex_path() -> Path:
    return get_hermes_home() / "dashboard.lock.mutex"


def _try_lock(handle: IO[str]) -> bool:
    try:
        if sys.platform == "win32":
            handle.seek(0)
            if handle.read(1) == "":
                handle.write("\n")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def _unlock(handle: IO[str]) -> None:
    try:
        if sys.platform == "win32":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_record(path: Path, host: str, port: int) -> None:
    record = {
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "argv": sys.argv,
    }
    path.write_text(json.dumps(record), encoding="utf-8")


def _format_lock_owner(record: dict[str, Any] | None) -> str:
    if not record:
        return ""
    pid = record.get("pid")
    host = record.get("host")
    port = record.get("port")
    details = []
    if pid is not None:
        details.append(f"PID {pid}")
    if host and port is not None:
        details.append(f"{host}:{port}")
    return f" ({', '.join(details)})" if details else ""


def _port_owner(host: str, port: int) -> str:
    """Return best-effort PID/cmdline details for a listening socket."""
    try:
        import psutil
    except ImportError:
        psutil = None

    if psutil is not None:
        try:
            wildcard = host in {"0.0.0.0", "::", ""}
            for conn in psutil.net_connections(kind="inet"):
                if conn.status != psutil.CONN_LISTEN or not conn.laddr:
                    continue
                if int(conn.laddr.port) != int(port):
                    continue
                if not wildcard and conn.laddr.ip not in {host, "0.0.0.0", "::"}:
                    continue
                if conn.pid is None:
                    return ""
                try:
                    process = psutil.Process(conn.pid)
                    command = " ".join(process.cmdline()).strip()
                except (psutil.Error, OSError):
                    command = ""
                suffix = f": {command}" if command else ""
                return f" PID {conn.pid}{suffix}"
        except (psutil.Error, OSError):
            pass

    try:
        result = subprocess.run(
            [
                "lsof",
                "-nP",
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-Fpc",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        pid = None
        command = None
        for line in result.stdout.splitlines():
            if line.startswith("p") and pid is None:
                pid = line[1:]
            elif line.startswith("c") and command is None:
                command = line[1:]
        if pid:
            command = _process_command(pid) or command
            suffix = f": {command}" if command else ""
            return f" PID {pid}{suffix}"
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _process_command(pid: str) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _assert_port_available(host: str, port: int) -> None:
    if port == 0:
        return

    try:
        addresses = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except socket.gaierror as exc:
        raise DashboardStartupConflict(
            f"Dashboard cannot resolve listen host {host!r}: {exc}"
        ) from exc

    for family, socktype, proto, _, address in addresses:
        probe = socket.socket(family, socktype, proto)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(address)
        except OSError as exc:
            owner = _port_owner(host, port)
            raise DashboardStartupConflict(
                f"Dashboard address {host}:{port} is already in use."
                f"{owner} Stop the existing service or choose --port <port>."
            ) from exc
        finally:
            probe.close()


@dataclass
class DashboardStartupLease:
    """Held for the dashboard process lifetime."""

    path: Path
    _handle: IO[str]
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        _unlock(self._handle)
        self._handle.close()
        try:
            atexit.unregister(self.release)
        except Exception:
            pass


def acquire_dashboard_startup_guard(
    host: str,
    port: int,
) -> DashboardStartupLease:
    """Acquire the profile lock and reject an already-bound listen address."""
    path = _lock_path()
    mutex = _mutex_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(mutex, "a+", encoding="utf-8")
    if not _try_lock(handle):
        owner = _format_lock_owner(_read_record(path))
        handle.close()
        raise DashboardStartupConflict(
            f"A dashboard for this Hermes profile is already starting or running"
            f"{owner}."
        )

    try:
        _write_record(path, host, port)
        _assert_port_available(host, port)
    except Exception:
        _unlock(handle)
        handle.close()
        raise

    lease = DashboardStartupLease(path=path, _handle=handle)
    atexit.register(lease.release)
    return lease
