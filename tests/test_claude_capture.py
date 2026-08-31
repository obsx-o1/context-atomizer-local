from __future__ import annotations

import io
import json

from test_support import TemporaryDatabaseTest

from atomizer_local_client.history.connection import database
from atomizer_local_client.hosts.claude_code.hook_adapter import (
    capture_claude_hook,
    normalize_claude_hook,
)
from atomizer_local_client.hosts.claude_code.hook_entrypoint import (
    MAX_HOOK_BYTES,
    run_hook,
)
from atomizer_local_client.runtime.permissions import PermissionStore


def prompt(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "session_id": "claude-session",
        "prompt_id": "prompt-1",
        "cwd": r"C:\Customers\ClaudeProject",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "capture Claude prompt",
    }
    value.update(overrides)
    return value


def stop(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "session_id": "claude-session",
        "prompt_id": "prompt-1",
        "cwd": r"C:\Customers\ClaudeProject",
        "hook_event_name": "Stop",
        "last_assistant_message": "capture Claude response",
    }
    value.update(overrides)
    return value


class ClaudeCaptureTests(TemporaryDatabaseTest):
    def test_direct_fields_normalize_and_ingest(self) -> None:
        user = normalize_claude_hook(prompt(transcript_path="ignored.jsonl"))
        self.assertEqual(user.content, "capture Claude prompt")
        self.assertEqual(user.host.value, "claude_code")
        first = capture_claude_hook(prompt(), self.database_path)
        second = capture_claude_hook(stop(), self.database_path)
        self.assertEqual(first.chat_id, second.chat_id)
        with database(self.database_path) as connection:
            roles = [row["role"] for row in connection.execute(
                "SELECT role FROM messages ORDER BY sequence_number"
            )]
        self.assertEqual(roles, ["user", "assistant"])

    def test_duplicate_and_missing_assistant_are_safe(self) -> None:
        first = capture_claude_hook(prompt(), self.database_path)
        duplicate = capture_claude_hook(prompt(), self.database_path)
        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertIsNone(capture_claude_hook(stop(last_assistant_message=None), self.database_path))

    def test_malformed_size_and_disabled_permission_fail_open_with_empty_stdout(self) -> None:
        permissions = PermissionStore(self.root / "permissions.json")
        for raw in (b"not-json", b"x" * (MAX_HOOK_BYTES + 1)):
            stdout = io.StringIO()
            self.assertEqual(run_hook(io.BytesIO(raw), stdout, self.database_path, permissions), 0)
            self.assertEqual(stdout.getvalue(), "")
        stdout = io.StringIO()
        run_hook(io.BytesIO(json.dumps(prompt()).encode()), stdout, self.database_path, permissions)
        with database(self.database_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_invalid_event_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_claude_hook(prompt(hook_event_name="PreToolUse"))
