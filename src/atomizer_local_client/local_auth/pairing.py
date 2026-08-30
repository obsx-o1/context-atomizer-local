"""Explicit one-time extension pairing and persisted paired-secret authority."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol


class SecretStore(Protocol):
    def load(self) -> str: ...
    def rotate(self) -> str: ...
    def remove(self) -> None: ...


class PairingRateLimited(PermissionError):
    pass


class ExtensionPairingAuthority:
    def __init__(
        self,
        secret_store: SecretStore,
        *,
        code_ttl_seconds: int = 300,
        max_attempts: int = 5,
        attempt_window_seconds: int = 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.secret_store = secret_store
        self.code_ttl_seconds = code_ttl_seconds
        self.max_attempts = max_attempts
        self.attempt_window_seconds = attempt_window_seconds
        self.clock = clock
        self._code: str | None = None
        self._expires_at = 0.0
        self._attempts: deque[float] = deque(maxlen=max_attempts)
        self._lock = threading.RLock()
        try:
            self._secret: str | None = secret_store.load()
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            self._secret = None

    def issue_code(self) -> str:
        with self._lock:
            self._code = secrets.token_urlsafe(32)
            self._expires_at = self.clock() + self.code_ttl_seconds
            self._attempts.clear()
            return self._code

    def _prune_attempts(self, now: float) -> None:
        cutoff = now - self.attempt_window_seconds
        while self._attempts and self._attempts[0] < cutoff:
            self._attempts.popleft()

    def pair(self, supplied_code: str) -> str:
        now = self.clock()
        with self._lock:
            self._prune_attempts(now)
            if len(self._attempts) >= self.max_attempts:
                raise PairingRateLimited("pairing attempts are temporarily limited")
            self._attempts.append(now)
            valid = (
                self._code is not None
                and now <= self._expires_at
                and hmac.compare_digest(supplied_code, self._code)
            )
            if not valid:
                if now > self._expires_at:
                    self._code = None
                raise PermissionError("pairing code was rejected")
            self._code = None
            self._expires_at = 0.0
            self._attempts.clear()
            self._secret = self.secret_store.rotate()
            return self._secret

    def secret(self) -> str | None:
        with self._lock:
            return self._secret

    def revoke(self) -> None:
        with self._lock:
            self.secret_store.remove()
            self._secret = None
            self._code = None
            self._expires_at = 0.0
            self._attempts.clear()

    @property
    def paired(self) -> bool:
        return self.secret() is not None
