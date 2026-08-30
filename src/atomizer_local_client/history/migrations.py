"""Ordered application of bundled SQLite schema migrations."""

from __future__ import annotations

import sqlite3
import re
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+$")


def registered_migrations(migration_directory: Path | None = None) -> tuple[Path, ...]:
    """Return the canonical validated migration registry in application order."""
    directory = migration_directory or Path(__file__).with_name("migrations")
    paths = tuple(sorted(Path(directory).glob("*.sql")))
    identifiers: list[str] = []
    numbers: list[int] = []
    for path in paths:
        match = _MIGRATION_NAME.fullmatch(path.stem)
        if match is None:
            raise RuntimeError(f"invalid migration identifier: {path.stem}")
        identifiers.append(path.stem)
        numbers.append(int(match.group(1)))
    if len(identifiers) != len(set(identifiers)) or len(numbers) != len(set(numbers)):
        raise RuntimeError("duplicate migration identifier or sequence number")
    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError("migration sequence must be contiguous from 001")
    return paths


def registered_migration_ids(
    migration_directory: Path | None = None,
) -> tuple[str, ...]:
    return tuple(path.stem for path in registered_migrations(migration_directory))


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for migration_path in registered_migrations():
        version = migration_path.stem
        exists = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
        ).fetchone()
        if exists:
            continue
        safe_version = version.replace("'", "''")
        script = migration_path.read_text(encoding="utf-8")
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + script
            + "\nINSERT INTO schema_migrations(version, applied_at) "
            + f"VALUES ('{safe_version}', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
            + "COMMIT;"
        )
