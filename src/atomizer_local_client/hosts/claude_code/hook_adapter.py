"""Normalize documented Claude Code hook payloads into existing chat events."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from atomizer_local_client.chat.contracts import ChatEvent, Host, IngestionReceipt, Role, utc_now
from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.diagnostics import record_capture_error


SUPPORTED_CLAUDE_HOOK_EVENTS = ("UserPromptSubmit", "Stop")


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value.strip()


def _identifier(*parts: str) -> str:
    material = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _workspace(cwd: Any) -> tuple[str | None, str | None]:
    if not isinstance(cwd, str) or not cwd.strip():
        return None, None
    local_path = Path(cwd).expanduser().resolve(strict=False)
    local_identity = str(local_path).replace("\\", "/").casefold()
    return (
        f"workspace:{_identifier(local_identity)}",
        local_path.name or "Claude Code workspace",
    )


def normalize_claude_hook(payload: Mapping[str, Any]) -> ChatEvent | None:
    """Use direct documented event fields; never parse Claude transcript storage."""

    hook_name = _text(payload.get("hook_event_name"), "hook_event_name")
    if hook_name not in SUPPORTED_CLAUDE_HOOK_EVENTS:
        raise ValueError("unsupported Claude hook event")
    session_id = _text(payload.get("session_id"), "session_id")
    if hook_name == "UserPromptSubmit":
        content = _text(payload.get("prompt"), "prompt")
        role = Role.USER
    else:
        assistant_message = payload.get("last_assistant_message")
        if assistant_message is None:
            return None
        content = _text(assistant_message, "last_assistant_message")
        role = Role.ASSISTANT

    prompt_id = payload.get("prompt_id")
    if prompt_id is not None:
        prompt_id = _text(prompt_id, "prompt_id")
    turn_reference = prompt_id or f"{hook_name}:{_identifier(content)}"
    project_reference, project_name = _workspace(payload.get("cwd"))
    return ChatEvent(
        event_id=_identifier(
            Host.CLAUDE_CODE.value,
            session_id,
            turn_reference,
            role.value,
            content,
        ),
        host=Host.CLAUDE_CODE,
        host_project_reference=project_reference,
        host_chat_reference=session_id,
        host_turn_reference=turn_reference,
        role=role,
        content=content,
        captured_at=str(payload.get("captured_at") or utc_now()),
        project_display_name=project_name,
        chat_display_name=None,
    )


def capture_claude_hook(
    payload: Mapping[str, Any], database_path: Path
) -> IngestionReceipt | None:
    event = normalize_claude_hook(payload)
    if event is None:
        return None
    return ingest_chat_event(database_path, event)


def capture_claude_hook_fail_open(payload: Mapping[str, Any], database_path: Path) -> bool:
    try:
        capture_claude_hook(payload, database_path)
        return True
    except BaseException as error:
        record_capture_error(Path(database_path).parent, "claude_capture_failed", error)
        return False
