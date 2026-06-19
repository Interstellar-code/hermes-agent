from __future__ import annotations

import secrets
import time


class ConfirmTokenManager:
    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl_seconds = ttl_seconds
        self._tokens: dict[str, dict] = {}

    def generate(self, action: str, target: str, payload: dict) -> str:
        token = secrets.token_urlsafe(12)
        self._tokens[token] = {
            "action": action,
            "target": target,
            "payload": payload,
            "expires_at": time.time() + self.ttl_seconds,
        }
        return token

    def verify(self, token: str, action: str, target: str) -> tuple[bool, str | None]:
        if not token:
            return False, "confirm_token is required"
        item = self._tokens.get(token)
        if not item:
            return False, "Unknown confirm_token"
        if item["expires_at"] < time.time():
            self._tokens.pop(token, None)
            return False, "confirm_token expired"
        if item["action"] != action or item["target"] != target:
            return False, "confirm_token does not match this action"
        self._tokens.pop(token, None)
        return True, None
