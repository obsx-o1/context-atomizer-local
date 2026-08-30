from __future__ import annotations

import io
import json

from test_support import TemporaryDatabaseTest

from atomizer_local_client.history.connection import database
from atomizer_local_client.chat.normalizer import normalize_codex_hook
from atomizer_local_client.hosts.codex.hook_adapter import (
    capture_codex_hook,
    capture_codex_hook_fail_open,
)
from atomizer_local_client.hosts.codex.hook_entrypoint import run_hook


def user_prompt(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "session_id": "session-123",
        "turn_id": "turn-123",
        "cwd": r"C:\Customers\PrivateProject",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "capture this supported prompt",
    }
    value.update(overrides)
    return value


def stop(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "session_id": "session-123",
        "turn_id": "turn-123",
        "cwd": r"C:\Customers\PrivateProject",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": "capture this assistant response",
    }
    value.update(overrides)
    return value


class CodexHookTests(TemporaryDatabaseTest):
    def test_supported_hook_has_no_trustworthy_chat_title(self) -> None:
        event = normalize_codex_hook(
            user_prompt(chat_display_name="Untrusted injected title")
        )
        self.assertIsNotNone(event)
        self.assertIsNone(event.chat_display_name)

    def test_user_prompt_submit_and_stop_store_one_message_each(self) -> None:
        user_receipt = capture_codex_hook(user_prompt(), self.database_path)
        assistant_receipt = capture_codex_hook(stop(), self.database_path)
        self.assertIsNotNone(user_receipt)
        self.assertIsNotNone(assistant_receipt)
        self.assertEqual(user_receipt.chat_id, assistant_receipt.chat_id)
        with database(self.database_path) as connection:
            rows = connection.execute(
                "SELECT host_turn_reference, role FROM messages ORDER BY sequence_number"
            ).fetchall()
        self.assertEqual([(row["host_turn_reference"], row["role"]) for row in rows], [("turn-123", "user"), ("turn-123", "assistant")])

    def test_session_id_is_stable_chat_identity_and_duplicate_is_idempotent(self) -> None:
        first = capture_codex_hook(user_prompt(), self.database_path)
        duplicate = capture_codex_hook(user_prompt(), self.database_path)
        self.assertEqual(first.chat_id, duplicate.chat_id)
        self.assertFalse(duplicate.inserted)

    def test_cwd_is_hashed_locally_and_raw_path_is_not_persisted(self) -> None:
        receipt = capture_codex_hook(user_prompt(), self.database_path)
        with database(self.database_path) as connection:
            row = connection.execute(
                "SELECT host_project_reference, display_name FROM projects WHERE project_id = ?",
                (receipt.project_id,),
            ).fetchone()
        self.assertTrue(row["host_project_reference"].startswith("workspace:"))
        self.assertNotIn("Customers", row["host_project_reference"])
        self.assertEqual(row["display_name"], "PrivateProject")

    def test_transcript_path_is_not_required(self) -> None:
        payload = user_prompt()
        self.assertNotIn("transcript_path", payload)
        self.assertTrue(capture_codex_hook(payload, self.database_path).inserted)

    def test_user_prompt_stdout_is_empty_and_content_is_not_echoed(self) -> None:
        stdin = io.BytesIO(json.dumps(user_prompt()).encode("utf-8"))
        stdout = io.StringIO()
        self.assertEqual(run_hook(stdin, stdout, self.database_path), 0)
        self.assertEqual(stdout.getvalue(), "")

    def test_stop_outputs_valid_non_continuing_json(self) -> None:
        stdin = io.BytesIO(json.dumps(stop()).encode("utf-8"))
        stdout = io.StringIO()
        self.assertEqual(run_hook(stdin, stdout, self.database_path), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"continue": True})
        self.assertNotIn("decision", stdout.getvalue())

    def test_stop_without_visible_assistant_message_is_safe(self) -> None:
        stdin = io.BytesIO(json.dumps(stop(last_assistant_message=None)).encode("utf-8"))
        stdout = io.StringIO()
        self.assertEqual(run_hook(stdin, stdout, self.database_path), 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"continue": True})

    def test_malformed_input_and_storage_failure_fail_open(self) -> None:
        malformed_out = io.StringIO()
        self.assertEqual(run_hook(io.BytesIO(b"not-json"), malformed_out, self.database_path), 0)
        self.assertEqual(malformed_out.getvalue(), "")
        directory_database = self.root / "database-is-directory"
        directory_database.mkdir()
        storage_out = io.StringIO()
        self.assertEqual(
            run_hook(io.BytesIO(json.dumps(user_prompt()).encode("utf-8")), storage_out, directory_database),
            0,
        )
        self.assertEqual(storage_out.getvalue(), "")

    def test_fts_failure_rolls_back_message_and_capture_remains_fail_open(self) -> None:
        with database(self.database_path) as connection:
            connection.execute("DROP TABLE lexical_entries_fts")
        self.assertFalse(capture_codex_hook_fail_open(user_prompt(), self.database_path))
        with database(self.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"]
        self.assertEqual(count, 0)
        error_log = (self.database_path.parent / "capture-errors.log").read_text(encoding="utf-8")
        self.assertNotIn("capture this supported prompt", error_log)


if __name__ == "__main__":
    import unittest

    unittest.main()
