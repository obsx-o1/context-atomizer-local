"""Bounded automatic reconciliation for explicitly authorized local sources."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atomizer_local_client.chat.contracts import utc_now
from atomizer_local_client.history.connection import database
from atomizer_local_client.library.document_registry import (
    SourceSyncResult,
    sync_elected_source,
)


@dataclass(frozen=True, slots=True)
class SourceMaintenanceError:
    source_id: str
    error_class: str


@dataclass(frozen=True, slots=True)
class SourceMaintenanceCycle:
    started_at: str
    completed_at: str
    source_count: int
    results: tuple[SourceSyncResult, ...]
    errors: tuple[SourceMaintenanceError, ...]


def reconcile_authorized_sources(database_path: Path) -> SourceMaintenanceCycle:
    """Reconcile every authorization once, isolating failures by source."""
    started_at = utc_now()
    with database(database_path) as connection:
        rows = connection.execute(
            "SELECT source_id FROM elected_sources ORDER BY source_id"
        ).fetchall()
    results: list[SourceSyncResult] = []
    errors: list[SourceMaintenanceError] = []
    for row in rows:
        source_id = str(row["source_id"])
        try:
            results.append(sync_elected_source(database_path, source_id))
        except KeyError:
            # A concurrent revocation is a successful stop boundary, not a cycle failure.
            continue
        except Exception as error:  # Keep one bad authorized source from stopping later ones.
            errors.append(
                SourceMaintenanceError(
                    source_id=source_id,
                    error_class=type(error).__name__[:128],
                )
            )
    return SourceMaintenanceCycle(
        started_at=started_at,
        completed_at=utc_now(),
        source_count=len(rows),
        results=tuple(results),
        errors=tuple(errors),
    )


class AutomaticSourceMaintainer:
    """One non-overlapping local scan loop owned by the Library runtime."""

    def __init__(
        self,
        database_path: Path,
        *,
        interval_seconds: float = 2.0,
        operation_lock: Any | None = None,
    ) -> None:
        if not 0.1 <= interval_seconds <= 3600.0:
            raise ValueError("automatic source interval must be between 0.1 and 3600 seconds")
        self.database_path = Path(database_path)
        self.interval_seconds = float(interval_seconds)
        self._stop_event = threading.Event()
        self.operation_lock = operation_lock or threading.RLock()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_cycle: SourceMaintenanceCycle | None = None

    @property
    def last_cycle(self) -> SourceMaintenanceCycle | None:
        with self._state_lock:
            return self._last_cycle

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop_event.is_set()

    @property
    def health_state(self) -> str:
        if not self.is_running:
            return "paused"
        cycle = self.last_cycle
        if cycle is not None and cycle.errors:
            return "error"
        return "running"

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="context-atomizer-source-maintenance",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self.operation_lock:
                    cycle = reconcile_authorized_sources(self.database_path)
            except Exception as error:
                now = utc_now()
                cycle = SourceMaintenanceCycle(
                    started_at=now,
                    completed_at=now,
                    source_count=0,
                    results=(),
                    errors=(
                        SourceMaintenanceError(
                            source_id="<cycle>",
                            error_class=type(error).__name__[:128],
                        ),
                    ),
                )
            with self._state_lock:
                self._last_cycle = cycle
            self._stop_event.wait(self.interval_seconds)
