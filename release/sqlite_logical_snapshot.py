"""Canonical logical snapshots for disposable installer preservation checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any


SNAPSHOT_FORMAT = "context-atomizer-sqlite-logical-snapshot-v1"

AUTHORITATIVE_TABLES = (
    "schema_migrations",
    "projects",
    "chats",
    "messages",
    "documents",
    "elected_sources",
    "document_source_memberships",
    "document_revision_history",
)

DERIVED_TABLES = (
    "lexical_entries",
    "semantic_units",
    "embedding_records",
    "entities",
    "entity_aliases",
    "entity_mentions",
    "claims",
    "claim_evidence",
    "claim_equivalence_decisions",
    "temporal_evidence_state",
    "contradiction_relations",
    "claim_verification_state",
)


class SnapshotError(RuntimeError):
    """Raised when a database cannot satisfy the preservation contract."""


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _canonical_value(value: object) -> dict[str, object]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bytes):
        return {
            "type": "blob",
            "length": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "real", "value": value.hex()}
    if isinstance(value, str):
        return {"type": "text", "value": value}
    raise SnapshotError(f"unsupported SQLite value type: {type(value).__name__}")


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _application_tables(connection: sqlite3.Connection) -> set[str]:
    names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
        )
    }
    return {
        name
        for name in names
        if not name.startswith("sqlite_") and not name.startswith("lexical_entries_fts")
    }


def _table_snapshot(connection: sqlite3.Connection, table: str) -> dict[str, object]:
    quoted = _quote_identifier(table)
    columns = [
        {
            "position": int(row[0]),
            "name": str(row[1]),
            "declared_type": str(row[2]),
            "not_null": bool(row[3]),
            "primary_key_position": int(row[5]),
        }
        for row in connection.execute(f"PRAGMA table_info({quoted})")
    ]
    if not columns:
        raise SnapshotError(f"required table is missing: {table}")
    primary_key = [
        column["name"]
        for column in sorted(
            (column for column in columns if column["primary_key_position"]),
            key=lambda column: int(column["primary_key_position"]),
        )
    ]
    order_columns = primary_key or [column["name"] for column in columns]
    order_by = ", ".join(_quote_identifier(str(name)) for name in order_columns)
    rows = [
        [_canonical_value(value) for value in row]
        for row in connection.execute(f"SELECT * FROM {quoted} ORDER BY {order_by}")
    ]
    return {
        "columns": columns,
        "order_by": order_columns,
        "rows": rows,
    }


def _scalar(connection: sqlite3.Connection, query: str) -> int:
    return int(connection.execute(query).fetchone()[0])


def _checks(connection: sqlite3.Connection) -> dict[str, Any]:
    integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
    foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
    lexical_rows = [
        (int(row[0]), str(row[1]))
        for row in connection.execute("SELECT rowid, content FROM lexical_entries ORDER BY rowid")
    ]
    fts_rows = [
        (int(row[0]), str(row[1]))
        for row in connection.execute("SELECT rowid, content FROM lexical_entries_fts ORDER BY rowid")
    ]
    derived = {
        "lexical_without_semantic": _scalar(
            connection,
            """SELECT COUNT(*) FROM lexical_entries l
               LEFT JOIN semantic_units s
                 ON s.source_id=l.source_id
                AND s.source_type=CASE l.corpus_type
                    WHEN 'CHAT_HISTORY' THEN 'chat_message'
                    ELSE 'elected_document' END
               WHERE s.semantic_unit_id IS NULL""",
        ),
        "semantic_without_lexical": _scalar(
            connection,
            """SELECT COUNT(*) FROM semantic_units s
               LEFT JOIN lexical_entries l
                 ON l.source_id=s.source_id
                AND l.corpus_type=CASE s.source_type
                    WHEN 'chat_message' THEN 'CHAT_HISTORY'
                    ELSE 'ELECTED_DOCUMENT' END
               WHERE l.lexical_entry_id IS NULL""",
        ),
        "semantic_without_embedding": _scalar(
            connection,
            """SELECT COUNT(*) FROM semantic_units s
               LEFT JOIN embedding_records e ON e.semantic_unit_id=s.semantic_unit_id
               WHERE e.semantic_unit_id IS NULL""",
        ),
        "failed_or_invalidated_embeddings": _scalar(
            connection,
            "SELECT COUNT(*) FROM embedding_records WHERE state IN ('failed','invalidated')",
        ),
        "mentions_without_entity_or_unit": _scalar(
            connection,
            """SELECT COUNT(*) FROM entity_mentions m
               LEFT JOIN entities e ON e.entity_id=m.entity_id
               LEFT JOIN semantic_units s ON s.semantic_unit_id=m.semantic_unit_id
               WHERE e.entity_id IS NULL OR s.semantic_unit_id IS NULL""",
        ),
        "evidence_without_claim_or_unit": _scalar(
            connection,
            """SELECT COUNT(*) FROM claim_evidence e
               LEFT JOIN claims c ON c.claim_id=e.claim_id
               LEFT JOIN semantic_units s ON s.semantic_unit_id=e.semantic_unit_id
               WHERE c.claim_id IS NULL OR s.semantic_unit_id IS NULL""",
        ),
        "evidence_without_temporal_state": _scalar(
            connection,
            """SELECT COUNT(*) FROM claim_evidence e
               LEFT JOIN temporal_evidence_state t ON t.evidence_id=e.evidence_id
               WHERE t.evidence_id IS NULL""",
        ),
        "claims_without_verification_state": _scalar(
            connection,
            """SELECT COUNT(*) FROM claims c
               LEFT JOIN claim_verification_state v ON v.claim_id=c.claim_id
               WHERE v.claim_id IS NULL""",
        ),
    }
    checks = {
        "integrity_check": integrity,
        "quick_check": quick,
        "foreign_key_violations": foreign_keys,
        "fts_matches_lexical_projection": lexical_rows == fts_rows,
        "derived_relationships": derived,
    }
    if integrity != ["ok"]:
        raise SnapshotError(f"integrity_check failed: {integrity}")
    if quick != ["ok"]:
        raise SnapshotError(f"quick_check failed: {quick}")
    if foreign_keys:
        raise SnapshotError(f"foreign_key_check failed: {foreign_keys}")
    if lexical_rows != fts_rows:
        raise SnapshotError("FTS content does not match the lexical projection")
    failed = {name: count for name, count in derived.items() if count}
    if failed:
        raise SnapshotError(f"derived relationship checks failed: {failed}")
    return checks


def snapshot_database(database_path: Path) -> dict[str, object]:
    """Return canonical logical state without mutating the supplied database."""
    path = Path(database_path).resolve()
    if not path.is_file():
        raise SnapshotError(f"database does not exist: {path}")
    connection = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        actual = _application_tables(connection)
        expected = set(AUTHORITATIVE_TABLES) | set(DERIVED_TABLES)
        if actual != expected:
            raise SnapshotError(
                f"unexpected application table inventory: missing={sorted(expected - actual)}, "
                f"extra={sorted(actual - expected)}"
            )
        authoritative = {
            table: _table_snapshot(connection, table) for table in AUTHORITATIVE_TABLES
        }
        derived = {table: _table_snapshot(connection, table) for table in DERIVED_TABLES}
        checks = _checks(connection)
    finally:
        connection.close()
    logical = {
        "format": SNAPSHOT_FORMAT,
        "authoritative_tables": authoritative,
        "derived_tables": derived,
    }
    return {
        **logical,
        "authoritative_fingerprint": _fingerprint(authoritative),
        "derived_fingerprint": _fingerprint(derived),
        "logical_fingerprint": _fingerprint(logical),
        "checks": checks,
    }


def write_snapshot(
    output_path: Path, snapshot: dict[str, object], *, pretty: bool = False
) -> int:
    """Atomically write a complete UTF-8 snapshot and return its byte length."""
    output = Path(output_path).resolve()
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    encoded = (
        json.dumps(
            snapshot,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return len(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pretty", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.database.resolve() == arguments.output.resolve():
            raise SnapshotError("output path must differ from the database path")
        snapshot = snapshot_database(arguments.database)
        byte_length = write_snapshot(arguments.output, snapshot, pretty=arguments.pretty)
    except (OSError, sqlite3.Error, SnapshotError) as error:
        print(f"logical snapshot failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "logical_fingerprint": snapshot["logical_fingerprint"],
                "snapshot_bytes": byte_length,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
