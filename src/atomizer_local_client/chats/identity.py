"""Content-free deterministic bindings for canonical host Chat identities."""

from __future__ import annotations

import hashlib
import sqlite3


_IDENTITY_DOMAIN = "context-atomizer-local/chat-source-identity/v1"
_TOMBSTONE_DOMAIN = "context-atomizer-local/chat-forget-tombstone/v1"


class ChatIdentityConflictError(RuntimeError):
    """A deterministic identity is bound to incompatible local state."""


def identity_digest(host: str, host_chat_reference: str) -> str:
    material = "\x1f".join((_IDENTITY_DOMAIN, str(host), host_chat_reference))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def tombstone_id(chat_id: str) -> str:
    material = "\x1f".join((_TOMBSTONE_DOMAIN, chat_id))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def bind_chat_identity(
    connection: sqlite3.Connection,
    *,
    chat_id: str,
    host: str,
    host_chat_reference: str,
    created_at: str,
) -> str:
    digest = identity_digest(host, host_chat_reference)
    connection.execute(
        """
        INSERT INTO chat_identity_aliases(identity_digest, chat_id, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(identity_digest) DO NOTHING
        """,
        (digest, chat_id, created_at),
    )
    bound = connection.execute(
        "SELECT chat_id FROM chat_identity_aliases WHERE identity_digest = ?",
        (digest,),
    ).fetchone()
    if bound is None or str(bound["chat_id"]) != chat_id:
        raise ChatIdentityConflictError("Chat identity is already bound to different local state")
    return digest


def ensure_current_chat_aliases(connection: sqlite3.Connection) -> None:
    """Backfill a digest for each current Chat without retaining raw aliases."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_identity_aliases'"
    ).fetchone()
    if exists is None:
        return
    for row in connection.execute(
        """
        SELECT chats.chat_id, chats.host, chats.host_chat_reference, chats.created_at
        FROM chats
        LEFT JOIN chat_identity_aliases
          ON chat_identity_aliases.chat_id = chats.chat_id
        WHERE chat_identity_aliases.chat_id IS NULL
        ORDER BY chats.chat_id
        """
    ).fetchall():
        bind_chat_identity(
            connection,
            chat_id=str(row["chat_id"]),
            host=str(row["host"]),
            host_chat_reference=str(row["host_chat_reference"]),
            created_at=str(row["created_at"]),
        )


def current_chat_aliases_complete(connection: sqlite3.Connection) -> bool:
    """Report whether migration backfill can be skipped without taking a write lock."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_identity_aliases'"
    ).fetchone()
    if exists is None:
        return True
    missing = connection.execute(
        """
        SELECT 1
        FROM chats
        LEFT JOIN chat_identity_aliases
          ON chat_identity_aliases.chat_id = chats.chat_id
        WHERE chat_identity_aliases.chat_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    return missing is None


def chat_identity_digests(connection: sqlite3.Connection, chat_id: str) -> tuple[str, ...]:
    return tuple(
        str(row["identity_digest"])
        for row in connection.execute(
            "SELECT identity_digest FROM chat_identity_aliases "
            "WHERE chat_id = ? ORDER BY identity_digest",
            (chat_id,),
        ).fetchall()
    )


__all__ = [
    "ChatIdentityConflictError",
    "bind_chat_identity",
    "chat_identity_digests",
    "current_chat_aliases_complete",
    "ensure_current_chat_aliases",
    "identity_digest",
    "tombstone_id",
]
