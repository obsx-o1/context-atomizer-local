"""Host-specific raw capture normalization into ChatEvent only."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from atomizer_local_client.chat.contracts import ChatEvent, Host, Role, utc_now


def _identifier(*parts: str) -> str:
    material = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value.strip()


def _codex_workspace(cwd: Any) -> tuple[str | None, str | None]:
    if not isinstance(cwd, str) or not cwd.strip():
        return None, None
    local_path = Path(cwd).expanduser().resolve(strict=False)
    local_identity = str(local_path).replace("\\", "/").casefold()
    return f"workspace:{_identifier(local_identity)}", local_path.name or "Codex workspace"


def normalize_codex_hook(payload: Mapping[str, Any]) -> ChatEvent | None:
    hook_name = _text(payload.get("hook_event_name"), "hook_event_name")
    if hook_name not in {"UserPromptSubmit", "Stop"}:
        raise ValueError("unsupported Codex hook event")
    session_id = _text(payload.get("session_id"), "session_id")
    turn_id = _text(payload.get("turn_id"), "turn_id")
    if hook_name == "UserPromptSubmit":
        content = _text(payload.get("prompt"), "prompt")
        role = Role.USER
    else:
        assistant_message = payload.get("last_assistant_message")
        if assistant_message is None:
            return None
        content = _text(assistant_message, "last_assistant_message")
        role = Role.ASSISTANT
    project_reference, project_name = _codex_workspace(payload.get("cwd"))
    event_id = _identifier(Host.CODEX.value, session_id, turn_id, role.value, content)
    return ChatEvent(
        event_id=event_id,
        host=Host.CODEX,
        host_project_reference=project_reference,
        host_chat_reference=session_id,
        host_turn_reference=turn_id,
        role=role,
        content=content,
        captured_at=str(payload.get("captured_at") or utc_now()),
        project_display_name=project_name,
        # Current supported Codex hook payloads expose stable session identity,
        # but no trustworthy human session title. Keep the title as presentation fallback.
        chat_display_name=None,
    )


def normalize_chatgpt_web(payload: Mapping[str, Any]) -> ChatEvent:
    normalized = dict(payload)
    normalized["host"] = Host.CHATGPT_WEB.value
    if not normalized.get("captured_at"):
        normalized["captured_at"] = utc_now()
    if not normalized.get("event_id"):
        normalized["event_id"] = _identifier(
            Host.CHATGPT_WEB.value,
            _text(normalized.get("host_chat_reference"), "host_chat_reference"),
            str(normalized.get("host_turn_reference") or ""),
            _text(normalized.get("role"), "role"),
            _text(normalized.get("content"), "content"),
        )
    return ChatEvent.from_mapping(normalized)


def normalize_claude_web(payload: Mapping[str, Any]) -> ChatEvent:
    normalized = dict(payload)
    normalized["host"] = Host.CLAUDE_WEB.value
    if not normalized.get("captured_at"):
        normalized["captured_at"] = utc_now()
    if not normalized.get("event_id"):
        normalized["event_id"] = _identifier(
            Host.CLAUDE_WEB.value,
            _text(normalized.get("host_chat_reference"), "host_chat_reference"),
            str(normalized.get("host_turn_reference") or ""),
            _text(normalized.get("role"), "role"),
            _text(normalized.get("content"), "content"),
        )
    return ChatEvent.from_mapping(normalized)


def normalize_host_event(payload: Mapping[str, Any]) -> ChatEvent | None:
    if payload.get("hook_event_name"):
        return normalize_codex_hook(payload)
    if payload.get("host") == Host.CHATGPT_WEB.value:
        return normalize_chatgpt_web(payload)
    if payload.get("host") == Host.CLAUDE_WEB.value:
        return normalize_claude_web(payload)
    raise ValueError("unknown host capture event")
