from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from atomizer_local_client.history.migrations import apply_migrations
from release.sqlite_logical_snapshot import snapshot_database, write_snapshot


class SqliteLogicalSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "history.sqlite3"
        with closing(sqlite3.connect(self.database_path, isolation_level=None)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            apply_migrations(connection)
            connection.execute(
                """INSERT INTO projects(
                       project_id,host,host_project_reference,display_name,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?)""",
                ("project-1", "chatgpt_web", "project-ref", "Project", "t1", "t2"),
            )
            connection.execute(
                """INSERT INTO chats(
                       chat_id,project_id,host,host_chat_reference,display_title,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                ("chat-1", "project-1", "chatgpt_web", "chat-ref", "Chat", "t1", "t2"),
            )
            connection.execute(
                """INSERT INTO messages(
                       message_id,chat_id,host_turn_reference,sequence_number,role,content,
                       captured_at,dedupe_key
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                ("message-1", "chat-1", "turn-1", 1, "user", "preserve me", "t3", "dedupe-1"),
            )
            connection.execute(
                """INSERT INTO documents(
                       document_id,project_id,display_name,document_type,local_source_reference,
                       text_content,updated_at,local_source_key,content_sha256,file_size,
                       modified_time_ns,file_identity,previous_content_sha256,superseded_at,revision
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "document-1", "project-1", "Document", "text", "C:/fixture/document.txt",
                    "document content", "t4", "c:/fixture/document.txt", "content-sha", 16,
                    123, "fixture-id", None, None, 1,
                ),
            )
            connection.execute(
                """INSERT INTO elected_sources(
                       source_id,project_id,source_kind,display_name,local_source_reference,
                       local_source_key,created_at,updated_at,last_synced_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    "source-1", "project-1", "FILE", "Document", "C:/fixture/document.txt",
                    "c:/fixture/document.txt", "t4", "t4", "t4",
                ),
            )
            connection.execute(
                "INSERT INTO document_source_memberships(source_id,document_id) VALUES (?,?)",
                ("source-1", "document-1"),
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fingerprint(self) -> str:
        return str(snapshot_database(self.database_path)["logical_fingerprint"])

    def test_snapshot_detects_deleted_message(self) -> None:
        before = self.fingerprint()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("DELETE FROM messages WHERE message_id='message-1'")
            connection.commit()
        self.assertNotEqual(self.fingerprint(), before)

    def test_snapshot_detects_changed_message_content(self) -> None:
        before = self.fingerprint()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE messages SET content='changed' WHERE message_id='message-1'"
            )
            connection.commit()
        self.assertNotEqual(self.fingerprint(), before)

    def test_snapshot_detects_deleted_source_authorization_and_document(self) -> None:
        before = self.fingerprint()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM elected_sources WHERE source_id='source-1'")
            connection.execute("DELETE FROM documents WHERE document_id='document-1'")
            connection.commit()
        self.assertNotEqual(self.fingerprint(), before)

    def test_wal_checkpoint_can_change_file_hash_without_logical_change(self) -> None:
        connection = sqlite3.connect(self.database_path, isolation_level=None)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute(
                "UPDATE messages SET content='WAL-preserved' WHERE message_id='message-1'"
            )
            logical_before = self.fingerprint()
            physical_before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        finally:
            connection.close()
        physical_after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        logical_after = self.fingerprint()
        self.assertNotEqual(physical_after, physical_before)
        self.assertEqual(logical_after, logical_before)

    def test_large_snapshot_uses_complete_file_instead_of_bounded_diagnostics(self) -> None:
        snapshot = snapshot_database(self.database_path)
        canonical = json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.assertGreater(len(canonical), 4096)
        old_bounded_transport = canonical[:4096] + "\n[diagnostic truncated]"
        with self.assertRaises(json.JSONDecodeError):
            json.loads(old_bounded_transport)

        output = Path(self.temporary.name) / "logical-snapshot.json"
        helper = Path(__file__).parents[1] / "release" / "sqlite_logical_snapshot.py"
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(helper),
                str(self.database_path),
                "--output",
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(output.is_file())
        self.assertGreater(output.stat().st_size, 4096)
        parsed = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(parsed["logical_fingerprint"], snapshot["logical_fingerprint"])
        diagnostics = result.stdout + result.stderr
        self.assertLessEqual(len(result.stdout.strip()), 4096)
        self.assertLessEqual(len(result.stderr.strip()), 4096)
        self.assertNotIn("preserve me", diagnostics)
        self.assertNotIn(canonical, diagnostics)

    def test_atomic_output_failure_leaves_no_partial_snapshot(self) -> None:
        output = Path(self.temporary.name) / "failed-snapshot.json"
        snapshot = snapshot_database(self.database_path)
        with mock.patch(
            "release.sqlite_logical_snapshot.os.replace",
            side_effect=OSError("synthetic replace failure"),
        ):
            with self.assertRaises(OSError):
                write_snapshot(output, snapshot)
        self.assertFalse(output.exists())
        self.assertEqual(
            list(output.parent.glob(f".{output.name}.*.tmp")),
            [],
        )

    @unittest.skipUnless(sys.platform == "win32", "requires Windows PowerShell 5.1")
    def test_powershell_diagnostics_remain_bounded_while_file_is_complete(self) -> None:
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        output = Path(self.temporary.name) / "powershell-logical-snapshot.json"
        helper = Path(__file__).parents[1] / "release" / "sqlite_logical_snapshot.py"
        process_helper = Path(__file__).parents[1] / "release" / "windows_process.ps1"
        command = r"""
$ErrorActionPreference = 'Stop'
. $env:ATOMIZER_TEST_PROCESS_HELPER
$arguments = '"{0}" "{1}" --output "{2}"' -f $env:ATOMIZER_TEST_SNAPSHOT_HELPER,$env:ATOMIZER_TEST_DATABASE,$env:ATOMIZER_TEST_OUTPUT
$result = Invoke-BoundedProcess -FilePath $env:ATOMIZER_TEST_PYTHON -ArgumentList $arguments -TimeoutSeconds 30 -ReportFailure
$snapshotText = Get-Content -LiteralPath $env:ATOMIZER_TEST_OUTPUT -Raw -Encoding UTF8
$snapshot = $snapshotText | ConvertFrom-Json
[ordered]@{
    exit_code = [int]$result.ExitCode
    output_exists = Test-Path -LiteralPath $env:ATOMIZER_TEST_OUTPUT -PathType Leaf
    byte_length = (Get-Item -LiteralPath $env:ATOMIZER_TEST_OUTPUT).Length
    stdout_length = $result.StandardOutput.Length
    stderr_length = $result.StandardError.Length
    diagnostic_contains_content = (($result.StandardOutput + $result.StandardError) -like '*preserve me*')
    logical_fingerprint = $snapshot.logical_fingerprint
} | ConvertTo-Json -Compress
"""
        environment = dict(os.environ)
        environment.update(
            {
                "ATOMIZER_TEST_PROCESS_HELPER": str(process_helper),
                "ATOMIZER_TEST_SNAPSHOT_HELPER": str(helper),
                "ATOMIZER_TEST_DATABASE": str(self.database_path),
                "ATOMIZER_TEST_OUTPUT": str(output),
                "ATOMIZER_TEST_PYTHON": sys.executable,
            }
        )
        result = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=40,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout.splitlines()[-1])
        expected = snapshot_database(self.database_path)
        self.assertEqual(receipt["exit_code"], 0)
        self.assertTrue(receipt["output_exists"])
        self.assertGreater(receipt["byte_length"], 4096)
        self.assertLessEqual(receipt["stdout_length"], 4096)
        self.assertLessEqual(receipt["stderr_length"], 4096)
        self.assertFalse(receipt["diagnostic_contains_content"])
        self.assertNotIn("preserve me", result.stdout + result.stderr)
        self.assertEqual(
            receipt["logical_fingerprint"], expected["logical_fingerprint"]
        )


if __name__ == "__main__":
    unittest.main()
