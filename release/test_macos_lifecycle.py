from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path


_PROBE_VALUE = "context-atomizer-macos-library-preservation-v1"


def _manager(executable: Path, command: str) -> dict[str, object]:
    result = subprocess.run(
        [str(executable), command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("manager response must be an object")
    return payload


def _write_probe(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS macos_lifecycle_probe (value TEXT NOT NULL)"
        )
        connection.execute("DELETE FROM macos_lifecycle_probe")
        connection.execute(
            "INSERT INTO macos_lifecycle_probe (value) VALUES (?)", (_PROBE_VALUE,)
        )
        connection.commit()


def _verify_probe(database: Path) -> None:
    try:
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute(
                "SELECT value FROM macos_lifecycle_probe"
            ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError("macOS Library preservation probe is missing") from exc
    if row != (_PROBE_VALUE,):
        raise RuntimeError("macOS Library preservation probe changed")


def validate_lifecycle(manager: Path, database: Path) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("packaged macOS lifecycle requires a native macOS host")
    status = _manager(manager, "status")
    if status.get("installed") is not True or status.get("running") is not True:
        raise RuntimeError("installed macOS runtime is not healthy")
    _write_probe(database)
    restarted = _manager(manager, "restart")
    if restarted.get("running") is not True:
        raise RuntimeError("macOS runtime restart failed")
    updated = _manager(manager, "update")
    if updated.get("updated") is not True or updated.get("running") is not True:
        raise RuntimeError("macOS runtime update failed")
    if updated.get("database_preserved") is not True:
        raise RuntimeError("macOS update did not preserve the Library")
    _verify_probe(database)
    with closing(sqlite3.connect(database)) as connection:
        migrations = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
    if migrations < 7:
        raise RuntimeError("macOS Library did not apply the existing migrations")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager", type=Path)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--verify-library-only", action="store_true")
    arguments = parser.parse_args()
    database = arguments.database.resolve()
    if arguments.verify_library_only:
        _verify_probe(database)
    else:
        if arguments.manager is None:
            parser.error("--manager is required unless --verify-library-only is used")
        validate_lifecycle(arguments.manager.resolve(), database)
    print("MACOS_PACKAGED_LIFECYCLE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
