"""Bounded deterministic nonce replay protection."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable


class ReplayWindow:
    def __init__(
        self,
        *,
        freshness_seconds: int = 120,
        future_skew_seconds: int = 30,
        max_entries: int = 4096,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if freshness_seconds <= 0 or future_skew_seconds < 0 or max_entries <= 0:
            raise ValueError("replay-window bounds must be positive")
        self.freshness_seconds = freshness_seconds
        self.future_skew_seconds = future_skew_seconds
        self.max_entries = max_entries
        self.clock = clock
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.freshness_seconds
        while self._seen:
            nonce, accepted_at = next(iter(self._seen.items()))
            if accepted_at >= cutoff:
                break
            self._seen.pop(nonce, None)

    def accept(self, nonce: str, timestamp: int) -> bool:
        now = self.clock()
        if timestamp < now - self.freshness_seconds:
            return False
        if timestamp > now + self.future_skew_seconds:
            return False
        with self._lock:
            self._prune(now)
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            while len(self._seen) > self.max_entries:
                self._seen.popitem(last=False)
            return True

    @property
    def size(self) -> int:
        with self._lock:
            self._prune(self.clock())
            return len(self._seen)
