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


# Herdr error codes that ``agent prompt`` returns BEFORE it writes anything to
# the pane (``src/app/api/agents.rs:62``). ``agent_prompt_failed`` belongs here
# too: it is raised by the failed ``try_send_bytes`` itself, so the text was
# never queued and the Enter was never scheduled.
#
# The distinction is the whole point. These prove no mutation occurred, so the
# caller can say so plainly. Anything else — a transport death, a timeout, an
# unrecognised code — leaves the outcome genuinely unknown, and must be
# reported as unknown rather than assumed either way.
# Herdr's --wait verdicts. agent_prompt_stalled means the text went in but no
# state change followed, i.e. the Enter did not take and a draft is sitting in
# the composer. That is a DIFFERENT outcome from "we do not know".
PROMPT_NOT_SUBMITTED = frozenset({"agent_prompt_stalled"})

PROMPT_REJECTED_BEFORE_SUBMISSION = frozenset(
    {
        "empty_agent_prompt",
        "agent_not_found",
        "agent_not_ready",
        "agent_prompt_failed",
    }
)


class HerdrClient:
    """Shells out to ``herdr`` and parses its JSON envelope."""

    # Pinned against the INSTALLED binary, never against a source checkout.
    # Re-verified 2026-07-30 on herdr 0.7.5 (protocol 17): PaneInfo fields,
    # the AgentStatus enum, the error envelope, and revision's placement on
    # pane_output_changed only are all unchanged from 16, so nothing this
    # client parses moved.
    #
    # Bumping this is a deliberate act, not a version-tracking chore: a pin
    # that drifts ahead of reality is how a verb got wrapped that the binary
    # did not have. /tmp/herdr currently reads PROTOCOL_VERSION = 18 — that is
    # a source checkout ahead of the shipped build, and is NOT evidence.
    PROTOCOL_VERSION = 17
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
        # execve() cannot carry a NUL inside an argument; create_subprocess_exec
        # raises a bare ValueError("embedded null byte") that reads as an
        # internal fault rather than bad input. Reject it as a Herdr error so
        # every verb — including the mutating ones Phase 2 adds — reports it as
        # what it is.
        for arg in argv:
            if "\x00" in arg:
                raise HerdrError(
                    "herdr argument contains an embedded null byte",
                    code="invalid_argument",
                )
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

    # -- action verbs (Phase 2, confirmation-gated at the tool layer) ---------

    async def submit_prompt(
        self,
        target: str,
        text: str,
        *,
        until: Optional[List[str]] = None,
        verify_ms: Optional[int] = 15000,
    ) -> Dict[str, Any]:
        """Submit a prompt to one agent session via ``herdr agent prompt``.

        This is the ONLY mutating verb wrapped anywhere in this client.

        It replaces the earlier ``agent send`` wrapper, which inserted literal
        text and stopped — leaving the prompt sitting unsubmitted in the CLI
        composer while the tool reported success. ``agent prompt`` is Herdr's
        purpose-built verb for this and does the whole job atomically
        (``src/app/api/agents.rs:62``)::

            let (text, enter) = encode_api_submission_parts(runtime, &text);
            runtime.try_send_bytes(text);
            runtime.send_bytes_after(enter, AGENT_PROMPT_SUBMIT_DELAY);

        Deliberately NOT composed from a literal-text write plus a separate
        ENTER keystroke verb, even though Herdr exposes both. Three reasons,
        all of which make the composed version worse rather than merely longer:

        * the submission encoding is per-runtime (bracketed paste and
          agent-specific quirks); hand-rolling it re-implements
          ``encode_api_submission_parts`` badly;
        * Herdr enforces preconditions we cannot see — ``effective_known_agent``,
          ``managed_agent_launch_pending``, and ``runtime_hosts_agent``, which
          refuse when the agent is no longer the pane's foreground process;
        * the delay between text and Enter is a real race that Herdr already
          gets right.

        It is also the *narrower* surface: there is no ``keys`` array anywhere
        in this call, so no arbitrary-key injection route exists to be abused.
        The keystroke-level verbs stay permanently unwrapped.

        Success means Herdr accepted the prompt and scheduled the Enter — the
        submission is asynchronous, so a success response is an acknowledgement,
        not proof the agent has acted. Pass ``until`` for observed evidence.

        Herdr attaches no request ID and offers no dedup, so this is not
        idempotent and never can be. Callers must not retry an unknown outcome.
        """
        argv = ["agent", "prompt", target, text]
        if verify_ms:
            # --wait is not a convenience here, it is the proof. Without it
            # Herdr acks as soon as the text is queued and schedules the Enter
            # afterwards, so a success response says nothing about whether the
            # prompt was actually submitted. Verified live 2026-07-30: the ack
            # returned success while the text sat unsubmitted at the composer
            # and the agent stayed idle.
            #
            # With --wait, Herdr requires an observed state change after
            # submission and returns agent_prompt_stalled when none happens —
            # which is precisely "the Enter did not take".
            argv.append("--wait")
            for status in until or ():
                argv += ["--until", status]
            argv += ["--timeout", str(int(verify_ms))]
        return await self._call(*argv)

    async def wait_agent_status(
        self, target: str, status: str, timeout_ms: int
    ) -> Dict[str, Any]:
        """Block until the session reaches ``status`` or the timeout expires.

        Uses the agent-scoped ``agent wait``, addressed by ``terminal_id``.
        Herdr 0.7.5 removed the top-level ``wait`` command this previously
        called ("unknown command: wait"); ``agent wait`` now accepts ``done``,
        which was the only reason the top-level form was preferred.

        A ``done`` signal is the sole admissible completion evidence: silence,
        an idle prompt, or a quiet pane are never completion.
        """
        return await self._call(
            "agent", "wait", target, "--until", status,
            "--timeout", str(int(timeout_ms)),
        )

    async def supports_submit(self) -> bool:
        """True if the INSTALLED binary has the agent-scoped submit verb.

        Probed by invoking ``agent prompt`` with no arguments, which prints its
        usage line and exits 2 without touching a session. A binary that does
        not know the subcommand prints the agent command list instead.

        Deliberately not probed via ``--help`` substring like the other verbs:
        ``agent prompt`` is absent from Herdr's hand-written agent help even in
        versions that implement it, so a help-based probe reports it missing on
        a binary that has it.

        This check exists because reading Herdr's source is NOT evidence about
        the binary on this machine. ``agent prompt`` landed in 0.7.5; against
        an installed 0.7.4 the wrapper invoked a subcommand that did not exist
        and the failure only surfaced at submission time, on a real session.
        """
        try:
            usage = await self.help_text("agent", "prompt")
        except HerdrError:
            return False
        return "usage: herdr agent prompt" in usage

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
