"""Current Codex hook payload parsing with no persistence implementation knowledge."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from atomizer_local_client.chat.contracts import IngestionReceipt
from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.chat.normalizer import normalize_codex_hook
from atomizer_local_client.diagnostics import record_capture_error


def capture_codex_hook(
    payload: Mapping[str, Any], database_path: Path
) -> IngestionReceipt | None:
    event = normalize_codex_hook(payload)
    if event is None:
        return None
    return ingest_chat_event(database_path, event)


def capture_codex_hook_fail_open(payload: Mapping[str, Any], database_path: Path) -> bool:
    try:
        capture_codex_hook(payload, database_path)
        return True
    except BaseException as error:
        record_capture_error(Path(database_path).parent, "codex_capture_failed", error)
        return False

