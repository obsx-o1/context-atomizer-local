"""Fail-open stdin/stdout process contract for UserPromptSubmit and Stop hooks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import BinaryIO, TextIO

from atomizer_local_client.diagnostics import record_capture_error
from atomizer_local_client.hosts.codex.hook_adapter import capture_codex_hook_fail_open
from atomizer_local_client.chat.normalizer import normalize_codex_hook
from atomizer_local_client.managed_access.hook_client import request_managed_context
from atomizer_local_client.runtime.permissions import PermissionStore

_MAX_HOOK_BYTES = 1024 * 1024


def run_hook(
    stdin: BinaryIO,
    stdout: TextIO,
    database_path: Path,
    permission_store: PermissionStore | None = None,
) -> int:
    hook_name: str | None = None
    managed_context: str | None = None
    try:
        raw = stdin.read(_MAX_HOOK_BYTES + 1)
        if len(raw) > _MAX_HOOK_BYTES:
            raise ValueError("hook payload exceeds size limit")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        hook_name = payload.get("hook_event_name")
        if permission_store is None or permission_store.is_enabled("codex"):
            capture_codex_hook_fail_open(payload, database_path)
            if hook_name == "UserPromptSubmit":
                event = normalize_codex_hook(payload)
                if event is not None:
                    managed_context = request_managed_context(event, database_path)
    except BaseException as error:
        record_capture_error(Path(database_path).parent, "codex_hook_input_failed", error)
    if hook_name == "UserPromptSubmit" and managed_context is not None:
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
    elif hook_name == "Stop":
        stdout.write('{"continue":true}\n')
        stdout.flush()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one supported Codex hook event locally")
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
