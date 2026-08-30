"""Ordered and idempotent message insertion."""

from __future__ import annotations

import hashlib
import sqlite3

from atomizer_local_client.chat.contracts import ChatEvent


def _dedupe_key(event: ChatEvent, host_chat_reference: str | None = None) -> str:
    observation_reference = event.host_turn_reference or event.event_id
    material = "\x1f".join(
        (
            event.host.value,
            host_chat_reference or event.host_chat_reference,
            observation_reference,
            event.role.value,
            event.content,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def write_message(
    connection: sqlite3.Connection, chat_id: str, event: ChatEvent
) -> tuple[str, bool, int]:
    dedupe_key = _dedupe_key(event)
    prior_dedupe_key = (
        _dedupe_key(event, event.rebind_from_host_chat_reference)
        if event.rebind_from_host_chat_reference
        else None
    )
    existing = connection.execute(
        """
        SELECT message_id, chat_id, sequence_number FROM messages
        WHERE dedupe_key = ? OR (? IS NOT NULL AND dedupe_key = ?)
        """,
        (dedupe_key, prior_dedupe_key, prior_dedupe_key),
    ).fetchone()
    if existing:
        if str(existing["chat_id"]) != chat_id:
            raise ValueError("duplicate observation belongs to another chat")
        return str(existing["message_id"]), False, int(existing["sequence_number"])
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_sequence FROM messages WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    sequence_number = int(row["next_sequence"])
    connection.execute(
        """
        INSERT INTO messages(
            message_id, chat_id, host_turn_reference, sequence_number,
            role, content, captured_at, dedupe_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            chat_id,
            event.host_turn_reference,
            sequence_number,
            event.role.value,
            event.content,
            event.captured_at,
            dedupe_key,
        ),
    )
    return event.event_id, True, sequence_number
