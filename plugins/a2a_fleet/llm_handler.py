"""LLM-backed inbound A2A handler (Route A — stateless model call).

Route A capability boundary: this is a direct, stateless model call using the
active Hermes profile's configured provider. It delivers real conversational
back-and-forth (reasoning, Q&A, persona replies) but does NOT have access to
the peer's live tools, memory, or MCP. Tool-grounded queries require Route B
(deferred to a later async/Task phase).
"""
from __future__ import annotations

from typing import Any, Dict

from .context_store import append, get_lock, history
from .response_handler import HandlerResult

_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful AI agent participating in an agent-to-agent conversation. "
    "Be concise and direct in your responses."
)


class A2AHandlerError(Exception):
    """Raised by llm_handler when the model call fails or the provider is unavailable."""


async def llm_handler(text: str, context_id: str, cfg: Dict[str, Any]) -> HandlerResult:
    """Call the active profile's LLM and return a HandlerResult.

    Holds the per-context asyncio lock across the full read->build->call->append
    span to ensure causal ordering for overlapping same-context turns.

    Raises A2AHandlerError on provider unavailability or model/network errors.
    """
    llm_cfg = cfg.get("llm") or {}
    max_tokens: int = int(llm_cfg.get("max_tokens", 2048))
    temperature: float = float(llm_cfg.get("temperature", 0.7))

    # Resolve system prompt: explicit string > file > default.
    system_prompt: str
    sp_text = llm_cfg.get("system_prompt")
    if sp_text:
        system_prompt = str(sp_text)
    else:
        sp_file = llm_cfg.get("system_prompt_file")
        if sp_file:
            try:
                from pathlib import Path
                system_prompt = Path(sp_file).read_text(encoding="utf-8")
            except OSError as exc:
                raise A2AHandlerError(
                    f"llm.system_prompt_file could not be read: {sp_file!r}: {exc}"
                ) from exc
        else:
            system_prompt = _DEFAULT_SYSTEM_PROMPT

    ctx_lock = get_lock(context_id)
    async with ctx_lock:
        # Build message list from current history.
        prior_turns = history(context_id)
        messages = [{"role": "system", "content": system_prompt}]
        for turn in prior_turns:
            role = "assistant" if turn["role"] == "assistant" else "user"
            messages.append({"role": role, "content": turn["text"]})
        messages.append({"role": "user", "content": text})

        # Resolve provider client.
        from agent.auxiliary_client import resolve_provider_client  # type: ignore[import]
        client, model = resolve_provider_client("auto", async_mode=True)
        if client is None:
            raise A2AHandlerError("llm provider unavailable (no auth)")

        # Call the model.
        try:
            resp = await client.chat.completions.create(
                model=model or "auto",
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            reply: str = resp.choices[0].message.content or ""
        except Exception as exc:
            raise A2AHandlerError(f"model call failed: {exc}") from exc

        # Persist both turns.
        append(context_id, "user", text)
        append(context_id, "assistant", reply)

    return HandlerResult(text=reply, context_id=context_id)
