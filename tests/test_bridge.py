from __future__ import annotations

import json
import hashlib
import threading
import time
import urllib.error
import urllib.request

from test_support import TemporaryDatabaseTest

from atomizer_local_client.bridge.local_ingress import LocalIngressServer
from atomizer_local_client.history.connection import database
from atomizer_local_client.local_auth.contracts import (
    PAIRING_DOMAIN,
    capture_request_material,
    sign_hex,
)
from atomizer_local_client.local_auth.pairing import ExtensionPairingAuthority


class MemorySecretStore:
    def __init__(self) -> None:
        self.value: str | None = None

    def load(self) -> str:
        if self.value is None:
            raise FileNotFoundError
        return self.value

    def rotate(self) -> str:
        self.value = "paired-extension-secret-0123456789-abcdefghi"
        return self.value

    def remove(self) -> None:
        self.value = None


class BridgeTests(TemporaryDatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.token = "local-test-token-0123456789-abcdef"
        self.pairing = ExtensionPairingAuthority(MemorySecretStore())
        self.secret = self.pairing.pair(self.pairing.issue_code())
        self.server = LocalIngressServer(
            self.database_path,
            self.token,
            self.pairing,
            _test_port=0,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat-events"
        self.nonce_sequence = 0

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(
        self,
        payload: object,
        token: str | None = None,
        path: str = "/v1/chat-events",
    ) -> tuple[int, dict[str, object]]:
        body = json.dumps(payload).encode("utf-8")
        self.nonce_sequence += 1
        nonce = f"{self.nonce_sequence:032d}"
        timestamp = str(int(time.time()))
        body_sha256 = hashlib.sha256(body).hexdigest()
        signature = sign_hex(
            token or self.secret,
            capture_request_material(
                method="POST",
                operation=path,
                nonce=nonce,
                timestamp=timestamp,
                body_sha256=body_sha256,
            ),
        )
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Atomizer-Protocol": "1",
                "X-Atomizer-Nonce": nonce,
                "X-Atomizer-Timestamp": timestamp,
                "X-Atomizer-Content-SHA256": body_sha256,
                "X-Atomizer-Signature": signature,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def plain_post(
        self, path: str, payload: dict[str, object]
    ) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def event(self) -> dict[str, object]:
        return {
            "event_id": "web-event-1",
            "host": "chatgpt_web",
            "host_project_reference": "g-p-visible",
            "host_chat_reference": "conversation-1",
            "host_turn_reference": "message-1",
            "role": "user",
            "content": "visible browser prompt",
            "captured_at": "2026-08-08T12:00:00+00:00",
            "project_display_name": "Visible Project",
            "chat_display_name": "Visible Chat",
        }

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_authenticated_title_endpoint_updates_only_an_existing_chat(self) -> None:
        event = self.event()
        event["host_chat_reference"] = "6a794dcb-ff9c-83ea-8177-7aef25bd782d"
        self.assertEqual(self.request(event)[0], 200)
        payload = {
            "observations": [
                {
                    "host_chat_reference": event["host_chat_reference"],
                    "host_project_reference": None,
                    "visible_title": "Reconciled Host Title",
                    "aria_label": "Reconciled Host Title",
                },
                {
                    "host_chat_reference": "11111111-1111-1111-1111-111111111111",
                    "host_project_reference": None,
                    "visible_title": "Unknown Host Title",
                    "aria_label": "Unknown Host Title",
                },
            ]
        }

        status, body = self.request(payload, path="/v1/chat-titles")

        self.assertEqual(status, 200)
        self.assertEqual(
            {key: body[key] for key in ("observed", "matched", "updated", "rejected")},
            {"observed": 2, "matched": 1, "updated": 1, "rejected": 0},
        )
        with database(self.database_path) as connection:
            chats = connection.execute(
                "SELECT host_chat_reference, display_title FROM chats"
            ).fetchall()
            messages = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["host_chat_reference"], event["host_chat_reference"])
        self.assertEqual(chats[0]["display_title"], "Reconciled Host Title")
        self.assertEqual(messages, 1)

    def test_authenticated_event_reaches_shared_ingestion(self) -> None:
        status, body = self.request(self.event())
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        with database(self.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"]
        self.assertEqual(count, 1)

    def test_duplicate_bridge_observation_is_idempotent(self) -> None:
        self.assertTrue(self.request(self.event())[1]["inserted"])
        self.assertFalse(self.request(self.event())[1]["inserted"])

    def test_new_chat_rebind_and_final_response_share_one_chat_through_bridge(self) -> None:
        provisional_reference = "provisional:new-chat:bridge-instance:submission"
        provisional = self.event()
        provisional.update(
            event_id="bridge-provisional",
            host_chat_reference=provisional_reference,
            host_turn_reference="bridge-submit",
            content="bridge new-chat prompt",
            chat_display_name=None,
        )
        rebound = dict(provisional)
        rebound.update(
            event_id="bridge-rebound",
            host_chat_reference="bridge-stable-conversation",
            rebind_from_host_chat_reference=provisional_reference,
            chat_display_name="Bridge Stable Chat",
        )
        assistant = dict(provisional)
        assistant.update(
            event_id="bridge-assistant",
            host_chat_reference="bridge-stable-conversation",
            host_turn_reference="bridge-assistant-turn",
            role="assistant",
            content="bridge final response",
            chat_display_name="Bridge Stable Chat",
        )

        self.assertTrue(self.request(provisional)[1]["inserted"])
        self.assertFalse(self.request(rebound)[1]["inserted"])
        self.assertFalse(self.request(rebound)[1]["inserted"])
        self.assertTrue(self.request(assistant)[1]["inserted"])
        with database(self.database_path) as connection:
            chats = connection.execute(
                "SELECT chat_id, host_chat_reference FROM chats WHERE host = ?",
                ("chatgpt_web",),
            ).fetchall()
            messages = connection.execute(
                "SELECT role, content FROM messages ORDER BY sequence_number"
            ).fetchall()
            provisional_count = connection.execute(
                "SELECT COUNT(*) FROM chats WHERE host = ? AND host_chat_reference LIKE ?",
                ("chatgpt_web", "provisional:new-chat:%"),
            ).fetchone()[0]
            distinct_chat_count = connection.execute(
                "SELECT COUNT(DISTINCT chat_id) FROM messages WHERE content IN (?, ?)",
                ("bridge new-chat prompt", "bridge final response"),
            ).fetchone()[0]
            lexical_counts = connection.execute(
                """
                SELECT COUNT(*) AS total, COUNT(DISTINCT source_id) AS distinct_sources
                FROM lexical_entries WHERE source_id IN (
                    SELECT message_id FROM messages WHERE content IN (?, ?)
                )
                """,
                ("bridge new-chat prompt", "bridge final response"),
            ).fetchone()
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["host_chat_reference"], "bridge-stable-conversation")
        self.assertEqual(
            [(row["role"], row["content"]) for row in messages],
            [("user", "bridge new-chat prompt"), ("assistant", "bridge final response")],
        )
        self.assertEqual(provisional_count, 0)
        self.assertEqual(distinct_chat_count, 1)
        self.assertEqual((lexical_counts["total"], lexical_counts["distinct_sources"]), (2, 2))

    def test_trusted_project_home_name_survives_rebind_and_null_assistant(self) -> None:
        project_reference = "g-p-00000000000000001111111111111111"
        initial = self.event()
        initial.update(
            event_id="name-sync-existing-chat",
            host_project_reference=project_reference,
            host_chat_reference="name-sync-existing-stable",
            host_turn_reference="name-sync-existing-turn",
            content="existing Project message",
            project_display_name=None,
        )
        provisional_reference = "provisional:new-chat:name-sync:submission"
        provisional = self.event()
        provisional.update(
            event_id="name-sync-provisional",
            host_project_reference=project_reference,
            host_chat_reference=provisional_reference,
            host_turn_reference="name-sync-submit",
            content="Reply with exactly: ATOMIZER PROJECT NAME SYNC ORIGINAL",
            project_display_name="ATOMIZER PROJECT TEST",
            chat_display_name=None,
        )
        rebound = dict(provisional)
        rebound.update(
            event_id="name-sync-rebound",
            host_chat_reference="name-sync-new-stable",
            rebind_from_host_chat_reference=provisional_reference,
            chat_display_name="Project Name Sync",
        )
        assistant = dict(provisional)
        assistant.update(
            event_id="name-sync-assistant",
            host_chat_reference="name-sync-new-stable",
            host_turn_reference="name-sync-assistant-turn",
            role="assistant",
            content="ATOMIZER PROJECT NAME SYNC ORIGINAL",
            project_display_name=None,
            chat_display_name="Project Name Sync",
        )

        self.assertTrue(self.request(initial)[1]["inserted"])
        self.assertTrue(self.request(provisional)[1]["inserted"])
        self.assertFalse(self.request(rebound)[1]["inserted"])
        self.assertTrue(self.request(assistant)[1]["inserted"])

        with database(self.database_path) as connection:
            projects = connection.execute(
                """
                SELECT project_id, host_project_reference, display_name
                FROM projects
                WHERE host = 'chatgpt_web' AND host_project_reference = ?
                """,
                (project_reference,),
            ).fetchall()
            chats = connection.execute(
                "SELECT chat_id, project_id, host_chat_reference FROM chats ORDER BY host_chat_reference"
            ).fetchall()

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["host_project_reference"], project_reference)
        self.assertEqual(projects[0]["display_name"], "ATOMIZER PROJECT TEST")
        self.assertEqual(len(chats), 2)
        self.assertEqual({row["project_id"] for row in chats}, {projects[0]["project_id"]})
        self.assertEqual(
            {row["host_chat_reference"] for row in chats},
            {"name-sync-existing-stable", "name-sync-new-stable"},
        )

    def test_two_project_home_flows_rebind_to_two_stable_chats_in_one_project(self) -> None:
        project_reference = "g-p-00000000000000001111111111111111"
        flows = (
            (
                "a",
                "6a771727-f250-83ea-ae5b-0e14abe188c6",
                "Reply with exactly: ATOMIZER PROJECT CHAT A",
                "ATOMIZER PROJECT CHAT A",
            ),
            (
                "b",
                "6a77178f-64a8-83ea-bdbc-3de93959a239",
                "Reply with exactly: ATOMIZER PROJECT CHAT B",
                "ATOMIZER PROJECT CHAT B",
            ),
        )
        for label, stable_reference, prompt_content, assistant_content in flows:
            provisional_reference = f"provisional:new-chat:project-tab-{label}:submission"
            provisional = self.event()
            provisional.update(
                event_id=f"project-{label}-provisional",
                host_project_reference=project_reference,
                host_chat_reference=provisional_reference,
                host_turn_reference=f"project-{label}-submit",
                content=prompt_content,
                project_display_name=None,
                chat_display_name=None,
            )
            rebound = dict(provisional)
            rebound.update(
                event_id=f"project-{label}-rebound",
                host_chat_reference=stable_reference,
                rebind_from_host_chat_reference=provisional_reference,
                chat_display_name=f"Chat {label.upper()}",
            )
            assistant = dict(provisional)
            assistant.update(
                event_id=f"project-{label}-assistant",
                host_chat_reference=(
                    provisional_reference if label == "b" else stable_reference
                ),
                host_turn_reference=f"project-{label}-assistant-turn",
                role="assistant",
                content=assistant_content,
                chat_display_name=f"Chat {label.upper()}",
            )

            self.assertTrue(self.request(provisional)[1]["inserted"])
            if label == "b":
                self.assertTrue(self.request(assistant)[1]["inserted"])
                self.assertFalse(self.request(rebound)[1]["inserted"])
            else:
                self.assertFalse(self.request(rebound)[1]["inserted"])
                self.assertTrue(self.request(assistant)[1]["inserted"])

        with database(self.database_path) as connection:
            projects = connection.execute(
                """
                SELECT project_id, host_project_reference
                FROM projects WHERE host = 'chatgpt_web'
                """
            ).fetchall()
            chats = connection.execute(
                """
                SELECT chat_id, project_id, host_chat_reference
                FROM chats WHERE host = 'chatgpt_web'
                ORDER BY host_chat_reference
                """
            ).fetchall()
            messages = connection.execute(
                """
                SELECT c.host_chat_reference, m.sequence_number, m.role, m.content
                FROM messages m JOIN chats c ON c.chat_id = m.chat_id
                WHERE c.host = 'chatgpt_web'
                ORDER BY c.host_chat_reference, m.sequence_number
                """
            ).fetchall()
            temporary_count = connection.execute(
                """
                SELECT COUNT(*) FROM chats
                WHERE host = 'chatgpt_web' AND (
                    host_chat_reference LIKE 'provisional:new-chat:%' OR
                    host_chat_reference LIKE 'route:/g/%/project'
                )
                """
            ).fetchone()[0]
            lexical_project_ids = connection.execute(
                """
                SELECT DISTINCT project_id FROM lexical_entries
                WHERE source_id IN (
                    SELECT m.message_id FROM messages m
                    JOIN chats c ON c.chat_id = m.chat_id
                    WHERE c.host = 'chatgpt_web'
                )
                """
            ).fetchall()

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["host_project_reference"], project_reference)
        self.assertEqual(len(chats), 2)
        self.assertEqual(len({row["chat_id"] for row in chats}), 2)
        self.assertEqual(len({row["project_id"] for row in chats}), 1)
        self.assertEqual(
            {row["host_chat_reference"] for row in chats},
            {flow[1] for flow in flows},
        )
        for _, stable_reference, prompt_content, assistant_content in flows:
            observed = [
                (row["sequence_number"], row["role"], row["content"])
                for row in messages
                if row["host_chat_reference"] == stable_reference
            ]
            self.assertEqual(
                observed,
                [(1, "user", prompt_content), (2, "assistant", assistant_content)],
            )
        self.assertEqual(temporary_count, 0)
        self.assertEqual(len(lexical_project_ids), 1)

    def test_authentication_and_shape_fail_closed_for_capture_only(self) -> None:
        rejected_status, rejected_body = self.request(self.event(), token="x" * 32)
        self.assertEqual(rejected_status, 401)
        self.assertFalse(rejected_body["ok"])
        self.assertTrue(self.thread.is_alive())
        with database(self.database_path) as connection:
            rejected_message_count = connection.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
            rejected_lexical_count = connection.execute(
                "SELECT COUNT(*) FROM lexical_entries"
            ).fetchone()[0]
        self.assertEqual((rejected_message_count, rejected_lexical_count), (0, 0))

        accepted_status, accepted_body = self.request(self.event())
        self.assertEqual(accepted_status, 200)
        self.assertTrue(accepted_body["ok"])
        self.assertTrue(accepted_body["inserted"])
        self.assertTrue(self.thread.is_alive())
        with database(self.database_path) as connection:
            accepted_message_count = connection.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
            accepted_lexical_count = connection.execute(
                "SELECT COUNT(*) FROM lexical_entries"
            ).fetchone()[0]
        self.assertEqual((accepted_message_count, accepted_lexical_count), (1, 1))

        malformed_status, malformed_body = self.request({"host": "chatgpt_web"})
        self.assertEqual(malformed_status, 400)
        self.assertFalse(malformed_body["ok"])
        self.assertTrue(self.thread.is_alive())
        with database(self.database_path) as connection:
            final_message_count = connection.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
            final_lexical_count = connection.execute(
                "SELECT COUNT(*) FROM lexical_entries"
            ).fetchone()[0]
        self.assertEqual((final_message_count, final_lexical_count), (1, 1))

    def test_server_is_bound_to_loopback_only(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_bootstrap_cannot_return_a_long_lived_secret(self) -> None:
        status, body = self.request({}, path="/v1/bootstrap")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"ok": False})
        self.assertNotIn(self.secret, json.dumps(body))
        self.assertNotIn(self.token, json.dumps(body))

    def test_pairing_endpoint_is_versioned_single_use_and_code_authorized(self) -> None:
        code = self.pairing.issue_code()
        wrong_domain_status, wrong_domain = self.plain_post(
            "/v1/pair",
            {
                "protocolVersion": "1",
                "pairingDomain": "wrong-domain",
                "pairingCode": code,
            },
        )
        self.assertEqual(wrong_domain_status, 403)
        self.assertEqual(wrong_domain, {"ok": False})

        status, body = self.plain_post(
            "/v1/pair",
            {
                "protocolVersion": "1",
                "pairingDomain": PAIRING_DOMAIN,
                "pairingCode": code,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["pairingDomain"], PAIRING_DOMAIN)
        self.assertEqual(body["extensionSecret"], self.pairing.secret())
        replay_status, replay = self.plain_post(
            "/v1/pair",
            {
                "protocolVersion": "1",
                "pairingDomain": PAIRING_DOMAIN,
                "pairingCode": code,
            },
        )
        self.assertEqual(replay_status, 403)
        self.assertEqual(replay, {"ok": False})

    def test_extension_capture_authority_cannot_invoke_management_actions(self) -> None:
        for path in ("/v1/runtime/stop", "/v1/library/launch"):
            with self.subTest(path=path):
                status, body = self.request({}, path=path)
                self.assertEqual(status, 401)
                self.assertEqual(body, {"ok": False})


if __name__ == "__main__":
    import unittest

    unittest.main()
