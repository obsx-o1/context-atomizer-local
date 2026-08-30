from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from test_support import SOURCE_ROOT, TemporaryDatabaseTest, chat_event

from atomizer_local_client.bridge.local_ingress import LocalIngressServer
from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.history.connection import database
from atomizer_local_client.hosts.codex.hook_entrypoint import run_hook
from atomizer_local_client.library.document_reader import list_documents, list_elected_sources
from atomizer_local_client.library.document_registry import (
    authorize_directory,
    authorize_file_source,
    revoke_source_authorization,
)
from atomizer_local_client.local_auth.contracts import capture_request_material, sign_hex
from atomizer_local_client.local_auth.library_session import LibrarySessionAuthority
from atomizer_local_client.local_auth.pairing import ExtensionPairingAuthority
from atomizer_local_client.runtime.permissions import PermissionStore
from atomizer_local_client.ui.library_server import LibraryViewServer


def _web_event(event_id: str, content: str) -> dict[str, object]:
    return {
        "event_id": event_id,
        "host": "chatgpt_web",
        "host_project_reference": "g-p-permission-test",
        "host_chat_reference": "permission-chat",
        "host_turn_reference": event_id,
        "role": "user",
        "content": content,
        "captured_at": "2026-08-11T20:00:00+00:00",
        "project_display_name": "Permission Test",
        "chat_display_name": "Permission Test Chat",
    }


def _codex_event(turn_id: str, prompt: str) -> dict[str, object]:
    return {
        "session_id": "permission-session",
        "turn_id": turn_id,
        "cwd": r"C:\Disposable\PermissionTest",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }


class PermissionAndSourceTests(TemporaryDatabaseTest):
    class _SecretStore:
        value: str | None = None

        def load(self) -> str:
            if self.value is None:
                raise FileNotFoundError
            return self.value

        def rotate(self) -> str:
            self.value = "permission-extension-secret-0123456789-abcdef"
            return self.value

        def remove(self) -> None:
            self.value = None

    def _message_contents(self) -> list[str]:
        with database(self.database_path) as connection:
            return [
                str(row["content"])
                for row in connection.execute(
                    "SELECT content FROM messages ORDER BY captured_at, message_id"
                ).fetchall()
            ]

    def test_missing_malformed_and_unknown_permission_state_fail_closed(self) -> None:
        path = self.root / "permissions.json"
        store = PermissionStore(path)
        self.assertFalse(store.is_enabled("chatgpt_web"))
        self.assertFalse(store.is_enabled("codex"))
        self.assertFalse(store.is_enabled("unknown"))
        path.write_text("not json", encoding="utf-8")
        self.assertFalse(PermissionStore(path).is_enabled("chatgpt_web"))

    def test_chatgpt_disable_preserves_history_and_reenable_resumes(self) -> None:
        store = PermissionStore(self.root / "permissions.json")
        store.set_enabled("chatgpt_web", True)
        token = "permission-test-token-0123456789-abcdef"
        pairing = ExtensionPairingAuthority(self._SecretStore())
        secret = pairing.pair(pairing.issue_code())
        server = LocalIngressServer(
            self.database_path,
            token,
            pairing,
            integration_enabled=store.is_enabled,
            _test_port=0,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1/chat-events"

            def send(payload: dict[str, object]) -> dict[str, object]:
                body = json.dumps(payload).encode("utf-8")
                nonce = os.urandom(24).hex()
                timestamp = str(int(time.time()))
                body_sha256 = hashlib.sha256(body).hexdigest()
                request = urllib.request.Request(
                    endpoint,
                    data=body,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Atomizer-Protocol": "1",
                        "X-Atomizer-Nonce": nonce,
                        "X-Atomizer-Timestamp": timestamp,
                        "X-Atomizer-Content-SHA256": body_sha256,
                        "X-Atomizer-Signature": sign_hex(
                            secret,
                            capture_request_material(
                                method="POST",
                                operation="/v1/chat-events",
                                nonce=nonce,
                                timestamp=timestamp,
                                body_sha256=body_sha256,
                            ),
                        ),
                    },
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    return json.loads(response.read())

            self.assertTrue(send(_web_event("web-enabled", "stored before disable"))["inserted"])
            store.set_enabled("chatgpt_web", False)
            disabled = send(_web_event("web-disabled", "must not be stored"))
            self.assertEqual(disabled, {"ok": True, "captured": False, "disabled": True})
            self.assertEqual(self._message_contents(), ["stored before disable"])
            store.set_enabled("chatgpt_web", True)
            self.assertTrue(send(_web_event("web-disabled", "stored after re-enable"))["inserted"])
            self.assertEqual(
                set(self._message_contents()),
                {"stored before disable", "stored after re-enable"},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_codex_disable_preserves_history_and_reenable_resumes_across_restart(self) -> None:
        permission_path = self.root / "permissions.json"
        store = PermissionStore(permission_path)
        store.set_codex_installed(True)
        store.set_enabled("codex", True)
        run_hook(
            io.BytesIO(json.dumps(_codex_event("one", "stored codex before disable")).encode()),
            io.StringIO(),
            self.database_path,
            store,
        )
        store.set_enabled("codex", False)
        restarted = PermissionStore(permission_path)
        self.assertFalse(restarted.is_enabled("codex"))
        run_hook(
            io.BytesIO(json.dumps(_codex_event("two", "must not store codex")).encode()),
            io.StringIO(),
            self.database_path,
            restarted,
        )
        self.assertEqual(self._message_contents(), ["stored codex before disable"])
        restarted.set_enabled("codex", True)
        second_restart = PermissionStore(permission_path)
        self.assertTrue(second_restart.is_enabled("codex"))
        self.assertTrue(second_restart.snapshot()["codex"].installed)
        run_hook(
            io.BytesIO(json.dumps(_codex_event("two", "stored codex after re-enable")).encode()),
            io.StringIO(),
            self.database_path,
            second_restart,
        )
        self.assertEqual(
            set(self._message_contents()),
            {"stored codex before disable", "stored codex after re-enable"},
        )

    def test_permissions_ui_is_human_readable_and_toggle_is_persistent(self) -> None:
        store = PermissionStore(self.root / "permissions.json")
        store.set_codex_installed(True)
        sessions = LibrarySessionAuthority()
        server = LibraryViewServer(
            self.database_path,
            0,
            csrf_token="permission-csrf",
            automatic_maintenance=False,
            permission_store=store,
            session_authority=sessions,
            extension_status_provider=lambda: {
                "state": "connected",
                "protocol_version": "1",
                "last_seen_at": "now",
                "paired": True,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor()
            )
            launch = sessions.issue_launch()
            with opener.open(base + f"/?launch={launch}", timeout=3):
                pass
            with opener.open(base + "/permissions", timeout=3) as response:
                body = response.read().decode("utf-8")
            for label in (
                "ChatGPT Web",
                "Codex",
                "Disabled",
                "Browser: Paired",
                "Hook: Installed",
                "Authorized folders",
                "Add a folder once",
            ):
                self.assertIn(label, body)
            for forbidden in ("bridge token", "43117", "source UUID", "host reference"):
                self.assertNotIn(forbidden, body.casefold())
            request = urllib.request.Request(
                base + "/integration/set",
                data=urllib.parse.urlencode(
                    {
                        "csrf_token": "permission-csrf",
                        "integration": "chatgpt_web",
                        "enabled": "yes",
                    }
                ).encode(),
                method="POST",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": base,
                    "Sec-Fetch-Site": "same-origin",
                },
            )
            with opener.open(request, timeout=3) as response:
                enabled_body = response.read().decode("utf-8")
            self.assertIn("ChatGPT Web is enabled", enabled_body)
            self.assertTrue(PermissionStore(store.path).is_enabled("chatgpt_web"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_directory_containment_overlap_revocation_and_write_safety(self) -> None:
        project = ingest_chat_event(
            self.database_path,
            chat_event(event_id="permission-project", content="permission seed"),
        )
        root = self.root / "authorized"
        nested = root / "sub"
        outside = self.root / "outside"
        nested.mkdir(parents=True)
        outside.mkdir()
        eligible = root / "a.md"
        nested_eligible = nested / "b.txt"
        unsupported = root / "ignored.pdf"
        outside_file = outside / "c.md"
        eligible.write_text("eligible root", encoding="utf-8")
        nested_eligible.write_text("eligible nested", encoding="utf-8")
        unsupported.write_text("unsupported", encoding="utf-8")
        outside_file.write_text("outside", encoding="utf-8")
        before = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and path != self.database_path
        }

        directory = authorize_directory(self.database_path, project.project_id, root)
        file_authorization = authorize_file_source(
            self.database_path, project.project_id, nested_eligible
        )
        names = {row["display_name"] for row in list_documents(self.database_path)}
        self.assertEqual(names, {"a.md", "b.txt"})
        self.assertNotIn("c.md", names)
        self.assertNotIn("ignored.pdf", names)
        self.assertTrue(revoke_source_authorization(self.database_path, directory.source_id))
        self.assertEqual(
            {row["display_name"] for row in list_documents(self.database_path)}, {"b.txt"}
        )
        self.assertTrue(
            revoke_source_authorization(self.database_path, file_authorization.source_id)
        )
        self.assertEqual(list_documents(self.database_path), [])
        self.assertEqual(list_elected_sources(self.database_path), [])
        after = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and path != self.database_path
        }
        self.assertEqual(after, before)
        self.assertTrue(eligible.is_file())
        self.assertTrue(nested_eligible.is_file())
        self.assertTrue(outside_file.is_file())

    def test_symlink_escape_is_not_indexed(self) -> None:
        project = ingest_chat_event(
            self.database_path,
            chat_event(event_id="symlink-project", content="symlink seed"),
        )
        root = self.root / "scope"
        outside = self.root / "outside"
        root.mkdir()
        outside.mkdir()
        outside_file = outside / "escaped.md"
        outside_file.write_text("must stay outside", encoding="utf-8")
        link = root / "escape"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            if os.name != "nt":
                self.skipTest(f"directory symlink unavailable: {type(error).__name__}")
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                self.skipTest("directory symlink and junction creation are unavailable")
        authorize_directory(self.database_path, project.project_id, root)
        self.assertEqual(list_documents(self.database_path), [])
        self.assertTrue(outside_file.is_file())

    def test_ingestion_modules_expose_no_customer_file_write_apis(self) -> None:
        forbidden = {"write_bytes", "write_text", "unlink", "rename", "replace", "chmod", "touch"}
        for relative in (
            Path("atomizer_local_client/library/document_registry.py"),
            Path("atomizer_local_client/library/source_maintenance.py"),
        ):
            source = (SOURCE_ROOT / relative).read_text(encoding="utf-8")
            tree = ast.parse(source)
            calls = {
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            self.assertEqual(calls & forbidden, set(), str(relative))
            self.assertNotIn('open("w', source)
            self.assertNotIn("open('w", source)


if __name__ == "__main__":
    import unittest

    unittest.main()
