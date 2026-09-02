from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.bridge.local_ingress import LocalIngressServer
from atomizer_local_client.derived_state.cycle import run_derived_state_cycle
from atomizer_local_client.hosts.claude_code.hook_entrypoint import run_hook as run_claude_hook
from atomizer_local_client.hosts.codex.hook_entrypoint import run_hook as run_codex_hook
from atomizer_local_client.managed_access.authority import (
    ManagedAuthorityRegistry,
    VerifiedManagedSession,
)
from atomizer_local_client.managed_access.broker import ManagedContextBroker
from atomizer_local_client.managed_access.ingress import ManagedIngress
from atomizer_local_client.managed_access.policy import (
    LibraryAccessPolicyStore,
    PolicyBackedAccessGate,
)
from atomizer_local_client.managed_access.reader import ManagedLibraryReader
from atomizer_local_client.memory_access.access_gate import (
    DirectLibraryAccessMode,
    LibraryCaller,
)
from atomizer_local_client.memory_access.query_service import LibraryQueryService
from atomizer_local_client.local_auth.pairing import ExtensionPairingAuthority


RUNTIME_BUILD = "a" * 64


class MemorySecretStore:
    def load(self) -> str:
        raise FileNotFoundError

    def rotate(self) -> str:
        return "managed-test-extension-secret-0123456789"

    def remove(self) -> None:
        return None


class FakeVerifier:
    def verify(self, assertion, *, runtime_build):  # type: ignore[no-untyped-def]
        if assertion != {"proof": "verified-by-private-test-provider"}:
            raise PermissionError("invalid assertion")
        return VerifiedManagedSession(
            session_reference="private-session-1",
            runtime_build=runtime_build,
            scope_references=("codex:project-1",),
            expires_at=datetime.now(UTC) + timedelta(minutes=2),
        )


class ManagedLibraryTests(TemporaryDatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="managed-memory",
                content="Project Atlas uses amber storage.",
            ),
        )
        run_derived_state_cycle(self.database_path)
        self.policy = LibraryAccessPolicyStore(self.root / "library-access-policy.json")
        self.authority = ManagedAuthorityRegistry(RUNTIME_BUILD)
        self.gate = PolicyBackedAccessGate(self.policy, manager=self.authority)
        self.service = LibraryQueryService(self.database_path, gate=self.gate)
        self.broker = ManagedContextBroker(self.authority)
        self.ingress = ManagedIngress(
            policy=self.policy,
            authority=self.authority,
            broker=self.broker,
            reader=ManagedLibraryReader(
                self.database_path, self.service, self.authority
            ),
            verifier=FakeVerifier(),
        )

    def activate(self) -> str:
        self.policy.set_mode(DirectLibraryAccessMode.MANAGED_EXCLUSIVE)
        result = self.ingress.post(
            "/v1/managed/authority/activate",
            {"assertion": {"proof": "verified-by-private-test-provider"}},
        )
        return str(result["manager_capability"])

    def test_missing_policy_preserves_direct_local_and_transition_is_persisted(self) -> None:
        self.assertEqual(self.policy.mode(), DirectLibraryAccessMode.DIRECT_LOCAL)
        self.policy.set_mode(DirectLibraryAccessMode.MANAGED_EXCLUSIVE)
        reloaded = LibraryAccessPolicyStore(self.policy.path)
        self.assertEqual(reloaded.mode(), DirectLibraryAccessMode.MANAGED_EXCLUSIVE)
        denied = self.service.search_library("amber")
        self.assertEqual(denied["status"], "managed_exclusive")
        self.assertEqual(denied["items"], [])

    def test_managed_reader_requires_verified_channel_and_reuses_query_service(self) -> None:
        capability = self.activate()
        result = self.ingress.post(
            "/v1/managed/library/read",
            {
                "manager_capability": capability,
                "host": "codex",
                "scope_reference": "project-1",
                "operation": "search_library",
                "arguments": {"query": "amber", "limit": 4},
            },
        )
        self.assertEqual(result["status"], "ok")
        self.assertGreater(result["result_count"], 0)
        with self.assertRaises(PermissionError):
            self.ingress.post(
                "/v1/managed/library/read",
                {
                    "manager_capability": "forged",
                    "host": "codex",
                    "scope_reference": "project-1",
                    "operation": "search_library",
                    "arguments": {"query": "amber"},
                },
            )
        with self.assertRaises(PermissionError):
            self.ingress.post(
                "/v1/managed/library/read",
                {
                    "manager_capability": capability,
                    "host": "claude_code",
                    "scope_reference": "project-1",
                    "operation": "search_library",
                    "arguments": {"query": "amber"},
                },
            )

    def test_context_exchange_is_turn_bound_and_replay_rejected(self) -> None:
        capability = self.activate()
        outcome: dict[str, object] = {}

        def request_context() -> None:
            outcome.update(
                self.ingress.post(
                    "/v1/managed/context/request",
                    {
                        "host": "codex",
                        "host_session_reference": "host-session",
                        "host_turn_reference": "turn-7",
                        "scope_reference": "project-1",
                        "prompt": "What does Atlas use?",
                    },
                )
            )

        thread = threading.Thread(target=request_context)
        thread.start()
        for _ in range(50):
            pending = self.ingress.post(
                "/v1/managed/context/next",
                {"manager_capability": capability},
            )
            if pending["status"] == "available":
                break
            time.sleep(0.01)
        request = pending["request"]
        self.assertEqual(request["prompt"], "What does Atlas use?")
        completion = {
            "manager_capability": capability,
            "request_id": request["request_id"],
            "scope_reference": request["scope_reference"],
            "host": request["host"],
            "host_session_reference": request["host_session_reference"],
            "host_turn_reference": request["host_turn_reference"],
            "context": "Verified Library context: Atlas uses amber evidence.",
        }
        with self.assertRaises(PermissionError):
            self.ingress.post(
                "/v1/managed/context/complete",
                {**completion, "host": "claude_code"},
            )
        self.ingress.post("/v1/managed/context/complete", completion)
        thread.join(timeout=2)
        self.assertEqual(outcome["status"], "available")
        self.assertEqual(outcome["host_turn_reference"], "turn-7")
        with self.assertRaises(PermissionError):
            self.ingress.post("/v1/managed/context/complete", completion)

    def test_disconnect_does_not_reopen_direct_tools(self) -> None:
        capability = self.activate()
        self.ingress.post(
            "/v1/managed/authority/revoke",
            {"manager_capability": capability},
        )
        self.assertEqual(self.policy.mode(), DirectLibraryAccessMode.MANAGED_EXCLUSIVE)
        direct = self.service.search_library("amber")
        managed = self.service.search_library(
            "amber", caller=LibraryCaller.TRUSTED_MANAGER
        )
        self.assertEqual(direct["status"], "managed_exclusive")
        self.assertEqual(managed["status"], "manager_not_verified")

    def test_lease_expiry_fails_closed_without_changing_policy(self) -> None:
        self.policy.set_mode(DirectLibraryAccessMode.MANAGED_EXCLUSIVE)
        self.authority.activate(
            VerifiedManagedSession(
                session_reference="expiring-session",
                runtime_build=RUNTIME_BUILD,
                scope_references=("codex:project-1",),
                expires_at=datetime.now(UTC) + timedelta(milliseconds=25),
            )
        )
        time.sleep(0.04)
        self.assertFalse(self.authority.authority().verified_active)
        self.assertEqual(self.policy.mode(), DirectLibraryAccessMode.MANAGED_EXCLUSIVE)
        self.assertEqual(
            self.service.search_library("amber")["status"], "managed_exclusive"
        )

    def test_private_assertion_cannot_be_claimed_by_payload_fields(self) -> None:
        self.policy.set_mode(DirectLibraryAccessMode.MANAGED_EXCLUSIVE)
        with self.assertRaises(PermissionError):
            self.ingress.post(
                "/v1/managed/authority/activate",
                {"assertion": {"manager": True, "trusted": True}},
            )

    def test_managed_http_routes_require_the_existing_management_bearer(self) -> None:
        token = "managed-http-test-token-0123456789-abcdef"
        server = LocalIngressServer(
            self.database_path,
            token,
            ExtensionPairingAuthority(MemorySecretStore()),
            managed_ingress=self.ingress,
            _test_port=0,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(base + "/v1/managed/status", timeout=2)
            self.assertEqual(rejected.exception.code, 401)
            request = urllib.request.Request(
                base + "/v1/managed/status",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["access_mode"], "DIRECT_LOCAL")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_native_hooks_emit_only_official_additional_context_shape(self) -> None:
        codex_payload = {
            "session_id": "codex-session",
            "turn_id": "codex-turn",
            "cwd": str(self.root / "project"),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "original Codex human prompt",
        }
        claude_payload = {
            "session_id": "claude-session",
            "prompt_id": "claude-turn",
            "cwd": str(self.root / "project"),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "original Claude human prompt",
        }
        for target, runner, payload in (
            (
                "atomizer_local_client.hosts.codex.hook_entrypoint.request_managed_context",
                run_codex_hook,
                codex_payload,
            ),
            (
                "atomizer_local_client.hosts.claude_code.hook_entrypoint.request_managed_context",
                run_claude_hook,
                claude_payload,
            ),
        ):
            stdout = io.StringIO()
            with patch(target, return_value="bounded verified context"):
                self.assertEqual(
                    runner(
                        io.BytesIO(json.dumps(payload).encode("utf-8")),
                        stdout,
                        self.database_path,
                    ),
                    0,
                )
            self.assertEqual(
                json.loads(stdout.getvalue()),
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": "bounded verified context",
                    }
                },
            )
        from atomizer_local_client.history.connection import database

        with database(self.database_path) as connection:
            contents = [
                str(row[0])
                for row in connection.execute(
                    "SELECT content FROM messages ORDER BY sequence_number"
                )
            ]
        self.assertEqual(contents.count("original Codex human prompt"), 1)
        self.assertEqual(contents.count("original Claude human prompt"), 1)
        self.assertNotIn("bounded verified context", contents)


if __name__ == "__main__":
    import unittest

    unittest.main()
