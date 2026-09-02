"""Fail-open stdin process contract for documented Claude Code capture hooks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import BinaryIO, TextIO

from atomizer_local_client.diagnostics import record_capture_error
from atomizer_local_client.hosts.claude_code.hook_adapter import (
    capture_claude_hook_fail_open,
    normalize_claude_hook,
)
from atomizer_local_client.managed_access.hook_client import request_managed_context
from atomizer_local_client.runtime.permissions import PermissionStore


MAX_HOOK_BYTES = 1024 * 1024


def run_hook(
    stdin: BinaryIO,
    stdout: TextIO,
    database_path: Path,
    permission_store: PermissionStore | None = None,
) -> int:
    """Capture first, then return only separately managed native context."""

    managed_context: str | None = None
    try:
        raw = stdin.read(MAX_HOOK_BYTES + 1)
        if len(raw) > MAX_HOOK_BYTES:
            raise ValueError("hook payload exceeds size limit")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        if permission_store is None or permission_store.is_enabled("claude_code"):
            capture_claude_hook_fail_open(payload, database_path)
            if payload.get("hook_event_name") == "UserPromptSubmit":
                event = normalize_claude_hook(payload)
                if event is not None:
                    managed_context = request_managed_context(event, database_path)
    except BaseException as error:
        record_capture_error(Path(database_path).parent, "claude_hook_input_failed", error)
    if managed_context is not None:
        stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": managed_context,
                    }
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        stdout.flush()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one supported Claude Code hook locally")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--permissions", type=Path)
    arguments = parser.parse_args()
    permission_path = arguments.permissions or arguments.database.parent / "permissions.json"
    return run_hook(
        sys.stdin.buffer,
        sys.stdout,
        arguments.database,
        PermissionStore(permission_path),
    )


if __name__ == "__main__":
    raise SystemExit(main())
