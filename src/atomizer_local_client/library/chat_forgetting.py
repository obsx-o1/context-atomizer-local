"""Canonical local Chat forgetting and resurrection-prevention orchestration."""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
from pathlib import Path

from atomizer_local_client.chat.contracts import ChatEvent, utc_now
from atomizer_local_client.chats.identity import (
    bind_chat_identity,
    chat_identity_digests,
    identity_digest,
    tombstone_id as derive_tombstone_id,
)
from atomizer_local_client.claims.repository import ClaimRepository
from atomizer_local_client.contradictions.repository import ContradictionRepository
from atomizer_local_client.entities.repository import EntityRepository
from atomizer_local_client.history.connection import database, transaction
from atomizer_local_client.temporal.repository import TemporalRepository
from atomizer_local_client.verification.repository import VerificationRepository


_DIGEST = re.compile(r"[0-9a-f]{64}")


class ForgetOperationError(RuntimeError):
    """Forgetting did not reach a complete, reportable success state."""


class CaptureBlockedError(ValueError):
    """Capture was denied by durable local resurrection protection."""


@dataclass(frozen=True, slots=True)
class ForgetResult:
    status: str
    tombstone_id: str | None


@dataclass(frozen=True, slots=True)
class BlockedCapture:
    tombstone_id: str
    chat_ids: tuple[str, ...]


def _required_text(value: str, name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return normalized


def _validated_tombstone_id(value: str) -> str:
    normalized = _required_text(value, "tombstone_id", maximum=64)
    if _DIGEST.fullmatch(normalized) is None:
        raise ValueError("tombstone_id must be a lowercase SHA-256 digest")
    return normalized


def _insert_tombstone(
    connection: sqlite3.Connection,
    *,
    identity: str,
    group: str,
    host: str,
    forgotten_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO chat_forget_tombstones(
            identity_digest, tombstone_id, host, forgotten_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(identity_digest) DO NOTHING
        """,
        (identity, group, host, forgotten_at),
    )
    stored = connection.execute(
        "SELECT tombstone_id FROM chat_forget_tombstones WHERE identity_digest = ?",
        (identity,),
    ).fetchone()
    if stored is None or str(stored["tombstone_id"]) != group:
        raise ForgetOperationError("Chat resurrection protection conflicts with existing local state")


def _reconcile_surviving_relational_state(connection: sqlite3.Connection) -> None:
    """Rebuild shared projections so only surviving provenance remains authoritative."""
    EntityRepository(connection).rebuild()
    ClaimRepository(connection).rebuild()
    temporal = TemporalRepository(connection)
    temporal.rebuild()
    _, disputed_claims = ContradictionRepository(connection).rebuild()
    temporal.mark_disputed(disputed_claims)
    VerificationRepository(connection).rebuild()


def _purge_chats(connection: sqlite3.Connection, chat_ids: tuple[str, ...]) -> None:
    if not chat_ids:
        return
    connection.execute("PRAGMA secure_delete = ON")
    project_ids: set[str] = set()
    message_ids: set[str] = set()
    for chat_id in chat_ids:
        row = connection.execute(
            "SELECT project_id FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            continue
        project_ids.add(str(row["project_id"]))
        message_ids.update(
            str(item["message_id"])
            for item in connection.execute(
                "SELECT message_id FROM messages WHERE chat_id = ?", (chat_id,)
            ).fetchall()
        )
        connection.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))

    _reconcile_surviving_relational_state(connection)
    for project_id in sorted(project_ids):
        connection.execute(
            """
            DELETE FROM projects WHERE project_id = ?
              AND NOT EXISTS (SELECT 1 FROM chats WHERE project_id = ?)
              AND NOT EXISTS (SELECT 1 FROM documents WHERE project_id = ?)
              AND NOT EXISTS (SELECT 1 FROM elected_sources WHERE project_id = ?)
            """,
            (project_id, project_id, project_id, project_id),
        )

    marks = ",".join("?" for _ in chat_ids)
    if connection.execute(
        f"SELECT 1 FROM chats WHERE chat_id IN ({marks}) LIMIT 1", chat_ids
    ).fetchone() is not None:
        raise ForgetOperationError("Authoritative Chat deletion did not complete")
    if connection.execute(
        f"SELECT 1 FROM lexical_entries WHERE chat_id IN ({marks}) LIMIT 1", chat_ids
    ).fetchone() is not None:
        raise ForgetOperationError("Chat retrieval cleanup did not complete")
    if connection.execute(
        f"SELECT 1 FROM semantic_units WHERE chat_id IN ({marks}) LIMIT 1", chat_ids
    ).fetchone() is not None:
        raise ForgetOperationError("Chat derived-state cleanup did not complete")
    if message_ids:
        message_marks = ",".join("?" for _ in message_ids)
        if connection.execute(
            f"SELECT 1 FROM claim_evidence WHERE source_id IN ({message_marks}) LIMIT 1",
            tuple(sorted(message_ids)),
        ).fetchone() is not None:
            raise ForgetOperationError("Chat evidence cleanup did not complete")


def _event_identity_references(event: ChatEvent) -> tuple[str, ...]:
    values = [event.host_chat_reference]
    if event.rebind_from_host_chat_reference is not None:
        values.append(event.rebind_from_host_chat_reference)
    return tuple(dict.fromkeys(values))


def stage_blocked_capture(
    connection: sqlite3.Connection, event: ChatEvent
) -> BlockedCapture | None:
    """Extend a deny group only across aliases proven by the capture event."""
    references = _event_identity_references(event)
    identities = tuple(identity_digest(event.host.value, value) for value in references)
    marks = ",".join("?" for _ in identities)
    rows = connection.execute(
        f"""
        SELECT identity_digest, tombstone_id, forgotten_at
        FROM chat_forget_tombstones
        WHERE identity_digest IN ({marks})
        ORDER BY tombstone_id, identity_digest
        """,
        identities,
    ).fetchall()
    if not rows:
        return None
    groups = {str(row["tombstone_id"]) for row in rows}
    if len(groups) != 1:
        raise CaptureBlockedError("Capture was blocked by conflicting local forget state")
    group = next(iter(groups))
    forgotten_at = min(str(row["forgotten_at"]) for row in rows)
    protected_identities = {str(row["identity_digest"]) for row in rows}
    chat_ids: set[str] = set()
    for reference, identity in zip(references, identities, strict=True):
        existing = tuple(
            str(row["chat_id"])
            for row in connection.execute(
                "SELECT chat_id FROM chats WHERE host = ? AND host_chat_reference = ?",
                (event.host.value, reference),
            ).fetchall()
        )
        if identity in protected_identities:
            chat_ids.update(existing)
            continue

        exact = tuple(
            str(row["chat_id"])
            for row in connection.execute(
                """
                SELECT chats.chat_id
                FROM chats
                JOIN messages ON messages.chat_id = chats.chat_id
                WHERE chats.host = ? AND chats.host_chat_reference = ?
                  AND messages.host_turn_reference IS ?
                  AND messages.role = ? AND messages.content = ?
                ORDER BY chats.chat_id
                """,
                (
                    event.host.value,
                    reference,
                    event.host_turn_reference,
                    event.role.value,
                    event.content,
                ),
            ).fetchall()
        )
        is_new_stable_target = (
            event.rebind_from_host_chat_reference is not None
            and reference == event.host_chat_reference
            and not existing
        )
        if not exact and not is_new_stable_target:
            continue
        _insert_tombstone(
            connection,
            identity=identity,
            group=group,
            host=event.host.value,
            forgotten_at=forgotten_at,
        )
        chat_ids.update(exact)
    return BlockedCapture(group, tuple(sorted(chat_ids)))


def purge_blocked_capture(
    connection: sqlite3.Connection, blocked: BlockedCapture
) -> None:
    _purge_chats(connection, blocked.chat_ids)


def register_event_identities(
    connection: sqlite3.Connection, chat_id: str, event: ChatEvent
) -> None:
    for reference in _event_identity_references(event):
        bind_chat_identity(
            connection,
            chat_id=chat_id,
            host=event.host.value,
            host_chat_reference=reference,
            created_at=event.captured_at,
        )


def forget_chat(database_path: Path, chat_id: str) -> ForgetResult:
    stable_chat_id = _required_text(chat_id, "chat_id")
    group = derive_tombstone_id(stable_chat_id)
    with database(database_path) as connection:
        try:
            with transaction(connection):
                chat = connection.execute(
                    "SELECT host, host_chat_reference, created_at FROM chats WHERE chat_id = ?",
                    (stable_chat_id,),
                ).fetchone()
                if chat is None:
                    protected = connection.execute(
                        "SELECT 1 FROM chat_forget_tombstones WHERE tombstone_id = ? LIMIT 1",
                        (group,),
                    ).fetchone()
                    status = "already_forgotten" if protected is not None else "not_found"
                else:
                    host = str(chat["host"])
                    bind_chat_identity(
                        connection,
                        chat_id=stable_chat_id,
                        host=host,
                        host_chat_reference=str(chat["host_chat_reference"]),
                        created_at=str(chat["created_at"]),
                    )
                    identities = chat_identity_digests(connection, stable_chat_id)
                    forgotten_at = utc_now()
                    for identity in identities:
                        _insert_tombstone(
                            connection,
                            identity=identity,
                            group=group,
                            host=host,
                            forgotten_at=forgotten_at,
                        )
                    status = "protected"
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise ForgetOperationError(
                "The Chat could not be protected against recapture; no deletion was reported"
            ) from error

        if status != "protected":
            return ForgetResult(status, group if status == "already_forgotten" else None)

        try:
            with transaction(connection):
                _purge_chats(connection, (stable_chat_id,))
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise ForgetOperationError(
                "The Chat remains blocked from recapture, but local deletion did not complete; retry forgetting"
            ) from error
    return ForgetResult("forgotten", group)


def list_forget_tombstones(database_path: Path) -> list[dict[str, object]]:
    with database(database_path) as connection:
        rows = connection.execute(
            """
            SELECT tombstone_id, min(host) AS host, min(forgotten_at) AS forgotten_at,
                   count(*) AS identity_count
            FROM chat_forget_tombstones
            GROUP BY tombstone_id
            ORDER BY forgotten_at DESC, tombstone_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def allow_recapture(database_path: Path, tombstone_id: str) -> bool:
    group = _validated_tombstone_id(tombstone_id)
    with database(database_path) as connection:
        with transaction(connection):
            removed = connection.execute(
                "DELETE FROM chat_forget_tombstones WHERE tombstone_id = ?", (group,)
            ).rowcount
    return removed > 0


__all__ = [
    "BlockedCapture",
    "CaptureBlockedError",
    "ForgetOperationError",
    "ForgetResult",
    "allow_recapture",
    "forget_chat",
    "list_forget_tombstones",
    "purge_blocked_capture",
    "register_event_identities",
    "stage_blocked_capture",
]
