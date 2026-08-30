"""Small value contracts shared across local-client boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping


class Host(StrEnum):
    CODEX = "codex"
    CHATGPT_WEB = "chatgpt_web"


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class CorpusType(StrEnum):
    CHAT_HISTORY = "CHAT_HISTORY"
    ELECTED_DOCUMENT = "ELECTED_DOCUMENT"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


@dataclass(frozen=True, slots=True)
class ChatEvent:
    event_id: str
    host: Host
    host_project_reference: str | None
    host_chat_reference: str
    host_turn_reference: str | None
    role: Role
    content: str
    captured_at: str
    project_display_name: str | None = None
    chat_display_name: str | None = None
    rebind_from_host_chat_reference: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.event_id, "event_id")
        _required_text(self.host_chat_reference, "host_chat_reference")
        _required_text(self.content, "content")
        if self.rebind_from_host_chat_reference is not None:
            source = _required_text(
                self.rebind_from_host_chat_reference,
                "rebind_from_host_chat_reference",
            )
            if self.host != Host.CHATGPT_WEB:
                raise ValueError("only ChatGPT-web chats can be provisionally rebound")
            if self.role != Role.USER or self.host_turn_reference is None:
                raise ValueError("only an identified user submission can rebind a provisional chat")
            if not source.startswith("provisional:new-chat:"):
                raise ValueError("rebind source must be a new-chat provisional reference")
            if self.host_chat_reference.startswith(("route:", "provisional:")):
                raise ValueError("rebind target must be a stable host chat reference")
            if source == self.host_chat_reference:
                raise ValueError("rebind source and target must differ")
        try:
            datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("captured_at must be an ISO-8601 timestamp") from exc

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ChatEvent":
        return cls(
            event_id=_required_text(value.get("event_id"), "event_id"),
            host=Host(_required_text(value.get("host"), "host")),
            host_project_reference=_optional_text(
                value.get("host_project_reference"), "host_project_reference"
            ),
            host_chat_reference=_required_text(
                value.get("host_chat_reference"), "host_chat_reference"
            ),
            host_turn_reference=_optional_text(
                value.get("host_turn_reference"), "host_turn_reference"
            ),
            role=Role(_required_text(value.get("role"), "role")),
            content=_required_text(value.get("content"), "content"),
            captured_at=_required_text(value.get("captured_at"), "captured_at"),
            project_display_name=_optional_text(
                value.get("project_display_name"), "project_display_name"
            ),
            chat_display_name=_optional_text(
                value.get("chat_display_name"), "chat_display_name"
            ),
            rebind_from_host_chat_reference=_optional_text(
                value.get("rebind_from_host_chat_reference"),
                "rebind_from_host_chat_reference",
            ),
        )


@dataclass(frozen=True, slots=True)
class IngestionReceipt:
    project_id: str
    chat_id: str
    message_id: str
    inserted: bool
    sequence_number: int


@dataclass(frozen=True, slots=True)
class LexicalCandidate:
    corpus_type: CorpusType
    source_id: str
    project_id: str
    chat_id: str | None
    role: str | None
    content: str
    score: float
