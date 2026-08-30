"""Bounded background scheduling and content-free derived-state health."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from atomizer_local_client.derived_state.contracts import DerivedStateCycle
from atomizer_local_client.derived_state.cycle import run_derived_state_cycle
from atomizer_local_client.derived_state.detector import inspect_derived_state
from atomizer_local_client.history.connection import database
from atomizer_local_client.semantic.contracts import EmbeddingBackend
from atomizer_local_client.semantic.embeddings import LocalFeatureHashEmbeddingBackend


class AutomaticDerivedStateMaintainer:
    """Run only when authoritative or persisted derived state is detectably stale."""

    def __init__(
        self,
        database_path: Path,
        *,
        interval_seconds: float = 2.0,
        operation_lock: Any | None = None,
        backend: EmbeddingBackend | None = None,
    ) -> None:
        if not 0.1 <= interval_seconds <= 3600.0:
            raise ValueError("derived-state interval must be between 0.1 and 3600 seconds")
        self.database_path = Path(database_path)
        self.interval_seconds = float(interval_seconds)
        self.operation_lock = operation_lock or threading.RLock()
        self.backend = backend or LocalFeatureHashEmbeddingBackend()
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = "idle"
        self._last_cycle: DerivedStateCycle | None = None
        self._last_successful_cycle: str | None = None
        self._last_error_class: str | None = None
        self._pending_count: int | None = None
        self._convergence_state = "pending"
        self._source_signature: str | None = None
        self._projection_signature: str | None = None

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop_event.is_set()

    @property
    def last_cycle(self) -> DerivedStateCycle | None:
        with self._state_lock:
            return self._last_cycle

    def health_snapshot(self) -> dict[str, object]:
        with self._state_lock:
            cycle = self._last_cycle
            return {
                "state": self._state if self.is_running else "paused",
                "running": self.is_running,
                "last_successful_cycle": self._last_successful_cycle,
                "pending_count": self._pending_count,
                "units_indexed": None if cycle is None else cycle.semantic_unit_count,
                "units_failed": None if cycle is None else cycle.embeddings_failed,
                "backend_version": self.backend.version,
                "last_error_class": self._last_error_class,
                "convergence_state": self._convergence_state,
            }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="context-atomizer-derived-state-maintenance",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def _record_error(self, error: BaseException) -> None:
        with self._state_lock:
            self._state = "error"
            self._last_error_class = type(error).__name__[:128]
            self._convergence_state = "behind"

    def _run_once(self) -> None:
        with database(self.database_path) as connection:
            inspection = inspect_derived_state(
                connection,
                self.backend,
                last_source_signature=self._source_signature,
                last_projection_signature=self._projection_signature,
            )
        with self._state_lock:
            self._pending_count = inspection.pending_count
        if not inspection.requires_cycle:
            with self._state_lock:
                self._state = "idle"
                self._last_error_class = None
                self._convergence_state = "converged"
            return

        with self._state_lock:
            self._state = "running"
            self._convergence_state = "backfilling"
        cycle = run_derived_state_cycle(self.database_path, self.backend)
        with self._state_lock:
            self._last_cycle = cycle
            self._pending_count = cycle.embeddings_failed
            if cycle.converged:
                self._source_signature = cycle.source_signature
                self._projection_signature = cycle.projection_signature
                self._last_successful_cycle = cycle.completed_at
                self._last_error_class = None
                self._state = "idle"
                self._convergence_state = "converged"
            else:
                self._last_error_class = "EmbeddingFailure"
                self._state = "error"
                self._convergence_state = "behind"

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self.operation_lock:
                    self._run_once()
            except BaseException as error:
                self._record_error(error)
            self._stop_event.wait(self.interval_seconds)
