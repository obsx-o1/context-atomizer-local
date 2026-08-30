"""One-time Library launch capabilities and runtime-scoped browser sessions."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable


class LibrarySessionAuthority:
    cookie_name = "atomizer_library_session"

    def __init__(
        self,
        *,
        launch_ttl_seconds: int = 60,
        session_ttl_seconds: int = 8 * 60 * 60,
        max_launches: int = 16,
        max_sessions: int = 16,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.launch_ttl_seconds = launch_ttl_seconds
        self.session_ttl_seconds = session_ttl_seconds
        self.max_launches = max_launches
        self.max_sessions = max_sessions
        self.clock = clock
        self._launches: OrderedDict[str, float] = OrderedDict()
        self._sessions: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.RLock()

    @staticmethod
    def _trim(store: OrderedDict[str, float], limit: int) -> None:
        while len(store) > limit:
            store.popitem(last=False)

    @staticmethod
    def _prune(store: OrderedDict[str, float], now: float) -> None:
        for token, expires_at in tuple(store.items()):
            if expires_at >= now:
                continue
            store.pop(token, None)

    def issue_launch(self) -> str:
        now = self.clock()
        with self._lock:
            self._prune(self._launches, now)
            token = secrets.token_urlsafe(32)
            self._launches[token] = now + self.launch_ttl_seconds
            self._trim(self._launches, self.max_launches)
            return token

    def consume_launch(self, supplied: str) -> str | None:
        now = self.clock()
        with self._lock:
            self._prune(self._launches, now)
            matched: str | None = None
            for candidate in self._launches:
                if hmac.compare_digest(supplied, candidate):
                    matched = candidate
                    break
            if matched is None:
                return None
            self._launches.pop(matched, None)
            session = secrets.token_urlsafe(32)
            self._sessions[session] = now + self.session_ttl_seconds
            self._trim(self._sessions, self.max_sessions)
            return session

    def authenticated(self, supplied: str) -> bool:
        now = self.clock()
        with self._lock:
            self._prune(self._sessions, now)
            return any(hmac.compare_digest(supplied, candidate) for candidate in self._sessions)

    def cookie_header(self, session: str) -> str:
        return (
            f"{self.cookie_name}={session}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={self.session_ttl_seconds}"
        )
