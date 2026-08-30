"""Deterministic election and synchronization for local text/Markdown sources."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from atomizer_local_client.chat.contracts import utc_now
from atomizer_local_client.history.connection import database, transaction
from atomizer_local_client.lexical.indexer import index_document, remove_document_index

_DOCUMENT_NAMESPACE = uuid.UUID("78d3404f-1c43-44c8-a3bc-d1e96d9070e1")
_SOURCE_NAMESPACE = uuid.UUID("c1e68975-aa02-4bbb-80e2-23884333cae9")
_DOCUMENT_TYPES = {".txt": "text", ".md": "markdown", ".markdown": "markdown"}
_MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
_MAX_HISTORICAL_REVISIONS = 10


class SourceKind(StrEnum):
    FILE = "FILE"
    DIRECTORY = "DIRECTORY"


@dataclass(frozen=True, slots=True)
class SourceSyncResult:
    source_id: str
    scanned: int
    added: int
    updated: int
    unchanged: int
    moved: int
    removed: int


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    path: Path
    path_key: str
    document_type: str
    content: str
    content_sha256: str
    file_size: int
    modified_time_ns: int
    file_identity: str | None


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path)).casefold()


def _file_identity(stat_result: os.stat_result) -> str | None:
    if stat_result.st_ino == 0:
        return None
    return f"{stat_result.st_dev}:{stat_result.st_ino}"


def _snapshot_file(source_path: Path) -> _FileSnapshot:
    resolved = Path(source_path).resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("elected source must be a regular file")
    document_type = _DOCUMENT_TYPES.get(resolved.suffix.casefold())
    if document_type is None:
        raise ValueError("only .txt, .md, and .markdown documents are supported")
    before = resolved.stat()
    if before.st_size > _MAX_DOCUMENT_BYTES:
        raise ValueError("elected document exceeds the 5 MiB V1 limit")
    raw = resolved.read_bytes()
    after = resolved.stat()
    before_identity = _file_identity(before)
    after_identity = _file_identity(after)
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before_identity != after_identity
        or len(raw) != after.st_size
    ):
        raise RuntimeError("elected document changed while it was being read")
    return _FileSnapshot(
        path=resolved,
        path_key=_path_key(resolved),
        document_type=document_type,
        content=raw.decode("utf-8"),
        content_sha256=hashlib.sha256(raw).hexdigest(),
        file_size=after.st_size,
        modified_time_ns=after.st_mtime_ns,
        file_identity=after_identity,
    )


def _directory_candidates(root: Path) -> list[Path]:
    candidates: list[Path] = []

    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, directory_names, file_names in os.walk(
        root, topdown=True, onerror=raise_walk_error, followlinks=False
    ):
        current_path = Path(current)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not (current_path / name).is_symlink()
        )
        for name in sorted(file_names):
            candidate = current_path / name
            if candidate.is_symlink():
                continue
            if candidate.suffix.casefold() not in _DOCUMENT_TYPES:
                continue
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root):
                continue
            candidates.append(resolved)
    return candidates


def _snapshots_for_source(source_kind: SourceKind, source_path: Path) -> list[_FileSnapshot]:
    if not source_path.exists():
        return []
    if source_kind == SourceKind.FILE:
        return [_snapshot_file(source_path)]
    if not source_path.is_dir():
        raise ValueError("elected directory source is no longer a directory")
    return [_snapshot_file(path) for path in _directory_candidates(source_path)]


def _source_id(project_id: str, source_kind: SourceKind, source_key: str) -> str:
    return str(uuid.uuid5(_SOURCE_NAMESPACE, f"{project_id}\x1f{source_kind.value}\x1f{source_key}"))


def _document_id(project_id: str, source_key: str) -> str:
    return str(uuid.uuid5(_DOCUMENT_NAMESPACE, f"{project_id}\x1f{source_key}"))


def _require_project(connection: sqlite3.Connection, project_id: str) -> None:
    if connection.execute(
        "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone() is None:
        raise KeyError(project_id)


def _elect_source(
    database_path: Path,
    project_id: str,
    source_path: Path,
    source_kind: SourceKind,
) -> str:
    resolved = Path(source_path).expanduser().resolve(strict=True)
    if source_kind == SourceKind.FILE and not resolved.is_file():
        raise ValueError("elected file source must be a regular file")
    if source_kind == SourceKind.DIRECTORY and not resolved.is_dir():
        raise ValueError("elected directory source must be a directory")
    if source_kind == SourceKind.FILE and resolved.suffix.casefold() not in _DOCUMENT_TYPES:
        raise ValueError("only .txt, .md, and .markdown documents are supported")
    source_key = _path_key(resolved)
    proposed_source_id = _source_id(project_id, source_kind, source_key)
    now = utc_now()
    with database(database_path) as connection:
        with transaction(connection):
            _require_project(connection, project_id)
            existing = connection.execute(
                "SELECT source_id, source_kind FROM elected_sources "
                "WHERE project_id = ? AND local_source_key = ?",
                (project_id, source_key),
            ).fetchone()
            if existing is not None and existing["source_kind"] != source_kind.value:
                raise ValueError("the same path cannot be elected as both a file and directory")
            source_id = str(existing["source_id"]) if existing is not None else proposed_source_id
            connection.execute(
                """
                INSERT INTO elected_sources(
                    source_id, project_id, source_kind, display_name,
                    local_source_reference, local_source_key, created_at, updated_at, last_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(project_id, local_source_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    local_source_reference = excluded.local_source_reference,
                    updated_at = excluded.updated_at
                """,
                (
                    source_id,
                    project_id,
                    source_kind.value,
                    resolved.name,
                    str(resolved),
                    source_key,
                    now,
                    now,
                ),
            )
    return source_id


def elect_file_source(database_path: Path, project_id: str, source_path: Path) -> SourceSyncResult:
    source_id = _elect_source(database_path, project_id, source_path, SourceKind.FILE)
    return sync_elected_source(database_path, source_id)


def elect_directory(database_path: Path, project_id: str, source_path: Path) -> SourceSyncResult:
    source_id = _elect_source(database_path, project_id, source_path, SourceKind.DIRECTORY)
    return sync_elected_source(database_path, source_id)


def authorize_file_source(
    database_path: Path, project_id: str, source_path: Path
) -> SourceSyncResult:
    """Authorize one supported file and perform its initial reconciliation."""
    return elect_file_source(database_path, project_id, source_path)


def authorize_directory(
    database_path: Path, project_id: str, source_path: Path
) -> SourceSyncResult:
    """Authorize a directory scope and perform its initial recursive reconciliation."""
    return elect_directory(database_path, project_id, source_path)


def elect_document(database_path: Path, project_id: str, source_path: Path) -> str:
    """Backward-compatible single-file election returning its document ID."""
    result = elect_file_source(database_path, project_id, source_path)
    with database(database_path) as connection:
        row = connection.execute(
            "SELECT document_id FROM document_source_memberships WHERE source_id = ?",
            (result.source_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("elected file did not produce a document")
    return str(row["document_id"])


def _cleanup_orphan_documents(
    connection: sqlite3.Connection, candidate_document_ids: set[str]
) -> int:
    removed = 0
    for document_id in sorted(candidate_document_ids):
        membership = connection.execute(
            "SELECT 1 FROM document_source_memberships WHERE document_id = ? LIMIT 1",
            (document_id,),
        ).fetchone()
        if membership is not None:
            continue
        remove_document_index(connection, document_id)
        removed += connection.execute(
            "DELETE FROM documents WHERE document_id = ?", (document_id,)
        ).rowcount
    return removed


def _prune_document_revision_history(
    connection: sqlite3.Connection, document_id: str
) -> tuple[int, ...]:
    """Keep the newest bounded historical revisions using stable revision order."""
    rows = connection.execute(
        "SELECT revision FROM document_revision_history "
        "WHERE document_id = ? ORDER BY revision DESC",
        (document_id,),
    ).fetchall()
    pruned = tuple(
        sorted(int(row["revision"]) for row in rows[_MAX_HISTORICAL_REVISIONS:])
    )
    for revision in pruned:
        connection.execute(
            "DELETE FROM document_revision_history "
            "WHERE document_id = ? AND revision = ?",
            (document_id, revision),
        )
    return pruned


def sync_elected_source(database_path: Path, source_id: str) -> SourceSyncResult:
    with database(database_path) as connection:
        source = connection.execute(
            "SELECT source_id, project_id, source_kind, local_source_reference "
            "FROM elected_sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
    if source is None:
        raise KeyError(source_id)
    project_id = str(source["project_id"])
    source_kind = SourceKind(str(source["source_kind"]))
    snapshots = _snapshots_for_source(
        source_kind, Path(str(source["local_source_reference"]))
    )
    current_path_keys = {snapshot.path_key for snapshot in snapshots}
    if len(current_path_keys) != len(snapshots):
        raise RuntimeError("elected source produced duplicate canonical paths")

    added = updated = unchanged = moved = 0
    now = utc_now()
    with database(database_path) as connection:
        with transaction(connection):
            if connection.execute(
                "SELECT 1 FROM elected_sources WHERE source_id = ?", (source_id,)
            ).fetchone() is None:
                raise KeyError(source_id)
            project_documents = connection.execute(
                "SELECT * FROM documents WHERE project_id = ? ORDER BY document_id",
                (project_id,),
            ).fetchall()
            documents_by_key = {
                str(row["local_source_key"]): row
                for row in project_documents
                if row["local_source_key"] is not None
            }
            source_documents = connection.execute(
                """
                SELECT d.* FROM documents d
                JOIN document_source_memberships m ON m.document_id = d.document_id
                WHERE m.source_id = ? ORDER BY d.document_id
                """,
                (source_id,),
            ).fetchall()
            source_document_ids = {str(row["document_id"]) for row in source_documents}
            seen_document_ids: set[str] = set()

            for snapshot in snapshots:
                existing = documents_by_key.get(snapshot.path_key)
                was_moved = False
                if existing is None and snapshot.file_identity is not None:
                    move_candidates = [
                        row
                        for row in source_documents
                        if row["file_identity"] == snapshot.file_identity
                        and str(row["local_source_key"]) not in current_path_keys
                        and str(row["document_id"]) not in seen_document_ids
                    ]
                    if len(move_candidates) == 1:
                        existing = move_candidates[0]
                        was_moved = True

                if existing is None:
                    document_id = _document_id(project_id, snapshot.path_key)
                    connection.execute(
                        """
                        INSERT INTO documents(
                            document_id, project_id, display_name, document_type,
                            local_source_reference, text_content, updated_at,
                            local_source_key, content_sha256, file_size,
                            modified_time_ns, file_identity, previous_content_sha256,
                            superseded_at, revision
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1)
                        """,
                        (
                            document_id,
                            project_id,
                            snapshot.path.name,
                            snapshot.document_type,
                            str(snapshot.path),
                            snapshot.content,
                            now,
                            snapshot.path_key,
                            snapshot.content_sha256,
                            snapshot.file_size,
                            snapshot.modified_time_ns,
                            snapshot.file_identity,
                        ),
                    )
                    index_document(
                        connection,
                        document_id=document_id,
                        project_id=project_id,
                        content=snapshot.content,
                        updated_at=now,
                    )
                    documents_by_key[snapshot.path_key] = connection.execute(
                        "SELECT * FROM documents WHERE document_id = ?", (document_id,)
                    ).fetchone()
                    added += 1
                else:
                    document_id = str(existing["document_id"])
                    content_changed = existing["content_sha256"] != snapshot.content_sha256
                    metadata_changed = (
                        existing["display_name"] != snapshot.path.name
                        or existing["document_type"] != snapshot.document_type
                        or existing["local_source_reference"] != str(snapshot.path)
                        or existing["local_source_key"] != snapshot.path_key
                        or existing["file_size"] != snapshot.file_size
                        or existing["modified_time_ns"] != snapshot.modified_time_ns
                        or existing["file_identity"] != snapshot.file_identity
                    )
                    lexical_exists = connection.execute(
                        "SELECT 1 FROM lexical_entries WHERE corpus_type = 'ELECTED_DOCUMENT' "
                        "AND source_id = ?",
                        (document_id,),
                    ).fetchone() is not None
                    if content_changed or metadata_changed:
                        old_key = str(existing["local_source_key"])
                        connection.execute(
                            """
                            UPDATE documents SET
                                display_name = ?, document_type = ?, local_source_reference = ?,
                                text_content = ?, updated_at = ?, local_source_key = ?,
                                content_sha256 = ?, file_size = ?, modified_time_ns = ?,
                                file_identity = ?,
                                previous_content_sha256 = CASE
                                    WHEN ? THEN content_sha256 ELSE previous_content_sha256 END,
                                superseded_at = CASE
                                    WHEN ? THEN ? ELSE superseded_at END,
                                revision = revision + ?
                            WHERE document_id = ?
                            """,
                            (
                                snapshot.path.name,
                                snapshot.document_type,
                                str(snapshot.path),
                                snapshot.content,
                                now,
                                snapshot.path_key,
                                snapshot.content_sha256,
                                snapshot.file_size,
                                snapshot.modified_time_ns,
                                snapshot.file_identity,
                                content_changed,
                                content_changed,
                                now,
                                int(content_changed),
                                document_id,
                            ),
                        )
                        documents_by_key.pop(old_key, None)
                        documents_by_key[snapshot.path_key] = connection.execute(
                            "SELECT * FROM documents WHERE document_id = ?", (document_id,)
                        ).fetchone()
                        updated += 1
                    else:
                        unchanged += 1
                    _prune_document_revision_history(connection, document_id)
                    if content_changed or not lexical_exists:
                        index_document(
                            connection,
                            document_id=document_id,
                            project_id=project_id,
                            content=snapshot.content,
                            updated_at=now,
                        )
                    if was_moved:
                        moved += 1

                connection.execute(
                    "INSERT OR IGNORE INTO document_source_memberships(source_id, document_id) "
                    "VALUES (?, ?)",
                    (source_id, document_id),
                )
                seen_document_ids.add(document_id)

            stale_document_ids = source_document_ids - seen_document_ids
            for document_id in sorted(stale_document_ids):
                connection.execute(
                    "DELETE FROM document_source_memberships WHERE source_id = ? AND document_id = ?",
                    (source_id, document_id),
                )
            removed = _cleanup_orphan_documents(connection, stale_document_ids)
            connection.execute(
                "UPDATE elected_sources SET updated_at = ?, last_synced_at = ? WHERE source_id = ?",
                (now, now, source_id),
            )

    return SourceSyncResult(
        source_id=source_id,
        scanned=len(snapshots),
        added=added,
        updated=updated,
        unchanged=unchanged,
        moved=moved,
        removed=removed,
    )


def sync_all_elected_sources(
    database_path: Path, project_id: str | None = None
) -> list[SourceSyncResult]:
    with database(database_path) as connection:
        if project_id is None:
            rows = connection.execute(
                "SELECT source_id FROM elected_sources ORDER BY source_id"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT source_id FROM elected_sources WHERE project_id = ? ORDER BY source_id",
                (project_id,),
            ).fetchall()
    return [sync_elected_source(database_path, str(row["source_id"])) for row in rows]


def unelect_source(database_path: Path, source_id: str) -> bool:
    with database(database_path) as connection:
        with transaction(connection):
            member_rows = connection.execute(
                "SELECT document_id FROM document_source_memberships WHERE source_id = ?",
                (source_id,),
            ).fetchall()
            candidate_document_ids = {str(row["document_id"]) for row in member_rows}
            deleted = connection.execute(
                "DELETE FROM elected_sources WHERE source_id = ?", (source_id,)
            ).rowcount
            if deleted == 0:
                return False
            _cleanup_orphan_documents(connection, candidate_document_ids)
            return True


def revoke_source_authorization(database_path: Path, source_id: str) -> bool:
    """Revoke an authorization without touching any physical file or directory."""
    return unelect_source(database_path, source_id)


def unelect_document(database_path: Path, document_id: str) -> bool:
    """Revoke a single-file election; directory-owned files require source revocation."""
    with database(database_path) as connection:
        source_rows = connection.execute(
            """
            SELECT s.source_id, s.source_kind
            FROM elected_sources s
            JOIN document_source_memberships m ON m.source_id = s.source_id
            WHERE m.document_id = ? ORDER BY s.source_id
            """,
            (document_id,),
        ).fetchall()
    if not source_rows:
        return False
    if any(row["source_kind"] != SourceKind.FILE.value for row in source_rows):
        raise ValueError("directory-owned documents must be revoked by elected source")
    removed = False
    for row in source_rows:
        removed = unelect_source(database_path, str(row["source_id"])) or removed
    return removed
