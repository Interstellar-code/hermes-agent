"""Read-only CLI wrapper around the ``herdr`` binary.

Herdr already implements the newline-delimited JSON socket protocol,
protocol negotiation, timeouts, and the SSH bridge. This module does not
reimplement any of that: it shells out to the ``herdr`` binary, captures its
stdout, and parses the JSON envelope it prints
(``{"id": ..., "result": ...}`` or ``{"id": ..., "error": {"code", "message"}}``).

Remote targets are a single argv prefix (``["--remote", ssh_target]``) —
herdr owns the SSH bridge lifecycle, there is nothing for this module to
manage.

Only read-only verbs are exposed here: ``status``, ``schema``,
``list_agents``, ``get_agent``. Verbs that mutate a live session are Phase 2
work, gated on confirmation tokens that do not exist yet, and are
deliberately absent from this file.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class HerdrError(Exception):
    """Base class for all herdr CLI errors."""

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class HerdrUnavailable(HerdrError):
    """Binary missing, server not running, or the CLI produced no usable output."""


class HerdrProtocolMismatch(HerdrError):
    """The live herdr protocol version does not match the pinned constant."""


class HerdrNotFound(HerdrError):
    """The requested target does not exist (e.g. unknown terminal id)."""


class HerdrTimeout(HerdrError):
    """The herdr subprocess did not finish within the configured timeout."""


# error.code values herdr is known to return, mapped to exception subclasses.
# Anything not listed here raises the HerdrError base with .code set.
_ERROR_CODE_MAP: Dict[str, type] = {
    "agent_not_found": HerdrNotFound,
    "session_not_found": HerdrNotFound,
    "target_not_found": HerdrNotFound,
}


class HerdrClient:
    """Shells out to ``herdr`` and parses its JSON envelope."""

    PROTOCOL_VERSION = 16
    DEFAULT_TIMEOUT = 10.0
    # ponytail: generous cap against a runaway/garbled response; raise if a
    # legitimate verb ever needs more.
    MAX_OUTPUT_BYTES = 10 * 1024 * 1024

    def __init__(
        self,
        binary: Optional[str] = None,
        ssh_target: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.binary = binary or shutil.which("herdr")
        self.ssh_target = ssh_target
        self.timeout = timeout
        self.max_output_bytes = self.MAX_OUTPUT_BYTES

    def _build_argv(self, *args: str) -> List[str]:
        argv: List[str] = [self.binary or "herdr"]
        if self.ssh_target:
            argv += ["--remote", self.ssh_target]
        argv += list(args)
        return argv

    async def _read_capped(self, stream: asyncio.StreamReader) -> bytes:
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > self.max_output_bytes:
                raise HerdrUnavailable(
                    f"herdr output exceeded {self.max_output_bytes} byte cap"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    async def _exec(self, argv: List[str]) -> tuple:
        if not argv[0]:
            raise HerdrUnavailable("herdr binary not found on PATH")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise HerdrUnavailable(f"herdr binary not found: {argv[0]}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.gather(self._read_capped(proc.stdout), self._read_capped(proc.stderr)),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise HerdrTimeout(
                f"herdr call timed out after {self.timeout}s: {' '.join(argv)}"
            ) from exc
        except HerdrUnavailable:
            proc.kill()
            await proc.wait()
            raise

        returncode = await proc.wait()
        logger.info("herdr call: argv=%s exit=%s", argv, returncode)
        logger.debug("herdr call output: stdout=%r stderr=%r", stdout, stderr)
        return returncode, stdout, stderr

    def _error_to_exception(self, error: Dict[str, Any]) -> HerdrError:
        code = error.get("code", "unknown")
        message = error.get("message", "")
        exc_cls = _ERROR_CODE_MAP.get(code, HerdrError)
        return exc_cls(message, code=code)

    async def _call(self, *args: str, enveloped: bool = True) -> Any:
        argv = self._build_argv(*args)
        returncode, stdout, stderr = await self._exec(argv)
        # herdr routes FAILURE envelopes to stderr and exits non-zero, while
        # success envelopes go to stdout (same split as its --help output).
        # Verified live: `herdr agent get term_doesnotexist` exits 1 and writes
        # {"id":...,"error":{"code":"agent_not_found",...}} to stderr with an
        # EMPTY stdout. Parsing stdout only would turn every structured Herdr
        # error into an opaque HerdrUnavailable and lose the error code, so try
        # stderr before giving up.
        payload = None
        for buf in (stdout, stderr):
            if not buf:
                continue
            try:
                payload = json.loads(buf)
                break
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        if payload is None:
            snippet = stderr.decode("utf-8", "replace")[:500] or repr(stdout[:500])
            raise HerdrUnavailable(
                f"herdr exited {returncode} with unparseable output: {snippet}"
            )
        if isinstance(payload, dict) and "error" in payload:
            raise self._error_to_exception(payload["error"])
        if not enveloped:
            return payload
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload

    async def help_text(self, *args: str) -> str:
        """Return raw ``--help`` text for a subcommand. Used only for capability
        probing — never parsed as JSON, never used to infer session state.

        herdr prints ``--help`` output to stderr (verified live), so both
        streams are captured and concatenated.
        """
        argv = self._build_argv(*args)
        _returncode, stdout, stderr = await self._exec(argv)
        return stdout.decode("utf-8", "replace") + stderr.decode("utf-8", "replace")

    # -- read-only verbs -----------------------------------------------------

    async def status(self) -> Dict[str, Any]:
        return await self._call("status", "--json", enveloped=False)

    async def schema(self) -> Dict[str, Any]:
        return await self._call("api", "schema", "--json", enveloped=False)

    async def list_agents(self) -> Dict[str, Any]:
        return await self._call("agent", "list")

    async def get_agent(self, target: str) -> Dict[str, Any]:
        return await self._call("agent", "get", target)

    async def check_protocol(self) -> None:
        """Raise HerdrProtocolMismatch if the live protocol has drifted."""
        schema = await self.schema()
        actual = schema.get("protocol")
        if actual != self.PROTOCOL_VERSION:
            raise HerdrProtocolMismatch(
                f"herdr protocol mismatch: expected {self.PROTOCOL_VERSION}, got {actual}",
                code="protocol_mismatch",
            )


def normalize_agent_record(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a raw ``agent list``/``agent get`` record into a stable dict.

    ``terminal_id`` is the stable handle (the ``agent`` kind label is not
    unique). ``cwd``/``workspace_id`` are workspace identity;
    ``foreground_cwd`` is deliberately never read here — it is a transient
    child-process directory that diverges from ``cwd`` in practice.
    """
    return {
        "terminal_id": raw.get("terminal_id"),
        "pane_id": raw.get("pane_id"),
        "agent_kind": raw.get("agent"),
        "agent_status": raw.get("agent_status"),
        "cwd": raw.get("cwd"),
        "workspace_id": raw.get("workspace_id"),
        "revision": raw.get("revision"),
        "terminal_title_stripped": raw.get("terminal_title_stripped"),
        "focused": raw.get("focused"),
    }
