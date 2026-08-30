from __future__ import annotations

import json
import http.cookiejar
import http.client
import os
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from test_support import TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.contracts import Host, Role
from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.history.connection import database
from atomizer_local_client.library.document_reader import list_documents, list_elected_sources
from atomizer_local_client.library.document_registry import elect_document
from atomizer_local_client.local_auth.library_session import LibrarySessionAuthority
from atomizer_local_client.ui.library_server import LibraryViewServer


class LibraryViewTests(TemporaryDatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.user = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="library-user",
                chat="library-chat",
                content="librarychatsearchmarker user message",
                project="library-project",
                project_name="Readable Codex Project",
            ),
        )
        ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="library-assistant",
                chat="library-chat",
                role=Role.ASSISTANT,
                content="assistant response follows user",
                project="library-project",
                project_name="Readable Codex Project",
            ),
        )
        self.web_chat_one = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="web-project",
                chat="web-project-chat",
                host=Host.CHATGPT_WEB,
                project="g-p-library-project",
                project_name="Readable ChatGPT Project",
                content="web project content",
            ),
        )
        self.web_chat_two = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="web-project-two",
                chat="web-project-chat-two",
                host=Host.CHATGPT_WEB,
                project="g-p-library-project",
                project_name="Readable ChatGPT Project",
                content="second distinct web project message",
            ),
        )
        opaque_reference = "g-p-00000000000000004444444444444444"
        self.opaque_chat = ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="opaque-project-chat",
                chat="opaque-project-chat",
                host=Host.CHATGPT_WEB,
                project=opaque_reference,
                project_name=opaque_reference,
                content="opaqueprojectsearchmarker controlled message",
            ),
        )
        ingest_chat_event(
            self.database_path,
            chat_event(
                event_id="unassigned",
                chat="unassigned-chat",
                host=Host.CHATGPT_WEB,
                project=None,
                project_name=None,
                content="unassigned content",
            ),
        )
        with database(self.database_path) as connection:
            connection.execute(
                "UPDATE chats SET display_title = ?, created_at = ?, updated_at = ? "
                "WHERE chat_id = ?",
                (
                    "ChatGPT - Readable ChatGPT Project",
                    "2026-08-10T07:30:59.353677+00:00",
                    "2026-08-10T07:31:04.538284+00:00",
                    self.web_chat_one.chat_id,
                ),
            )
            connection.execute(
                "UPDATE chats SET display_title = ?, created_at = ?, updated_at = ? "
                "WHERE chat_id = ?",
                (
                    "ChatGPT - Readable ChatGPT Project",
                    "2026-08-10T07:34:22.210395+00:00",
                    "2026-08-10T07:34:31.580509+00:00",
                    self.web_chat_two.chat_id,
                ),
            )
            connection.execute(
                "UPDATE chats SET display_title = ?, created_at = ?, updated_at = ? "
                "WHERE chat_id = ?",
                (
                    f"ChatGPT - {opaque_reference}",
                    "2026-08-10T07:40:01.000000+00:00",
                    "2026-08-10T07:40:02.000000+00:00",
                    self.opaque_chat.chat_id,
                ),
            )
        self.test_a = self.root / "TEST_A.md"
        self.test_b = self.root / "TEST_B.txt"
        self.test_a.write_text("unelected library test A", encoding="utf-8")
        self.test_b.write_text("librarydocumentsearchmarker survivor", encoding="utf-8")
        self.document_id = elect_document(
            self.database_path, self.user.project_id, self.test_b
        )
        self.session_authority = LibrarySessionAuthority()
        self.server = LibraryViewServer(
            self.database_path,
            0,
            csrf_token="library-view-test-csrf-token",
            session_authority=self.session_authority,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        capability = self.session_authority.issue_launch()
        with self.opener.open(self.base_url + "/?launch=" + capability, timeout=3) as response:
            self.assertEqual(response.geturl(), self.base_url + "/")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def get(self, path: str) -> tuple[int, str, dict[str, str]]:
        with self.opener.open(self.base_url + path, timeout=3) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)

    def post(self, path: str, values: dict[str, str]) -> tuple[int, str]:
        request = urllib.request.Request(
            self.base_url + path,
            data=urllib.parse.urlencode(values).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.base_url,
                "Sec-Fetch-Site": "same-origin",
            },
        )
        with self.opener.open(request, timeout=3) as response:
            return response.status, response.read().decode("utf-8")

    def raw_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        skip_host: bool = False,
    ) -> tuple[int, str, dict[str, str]]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )
        try:
            connection.putrequest(method, path, skip_host=skip_host)
            for name, value in (headers or {}).items():
                connection.putheader(name, value)
            if body is not None:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            try:
                return (
                    response.status,
                    response.read().decode("utf-8"),
                    dict(response.getheaders()),
                )
            finally:
                response.close()
        finally:
            connection.close()

    def rejected_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        skip_host: bool = False,
    ) -> tuple[str, str, dict[str, str]]:
        try:
            status, response_body, response_headers = self.raw_request(
                method,
                path,
                headers=headers,
                body=body,
                skip_host=skip_host,
            )
        except (ConnectionAbortedError, ConnectionResetError) as error:
            windows_error = getattr(error, "winerror", None) or error.errno
            if os.name != "nt" or windows_error not in {10053, 10054}:
                raise
            transport = f"win32-{windows_error}"
            response_body = ""
            response_headers = {}
        else:
            self.assertEqual(status, 403)
            transport = "http-403"

        self.assertTrue(self.thread.is_alive())
        health_status, health_body, _ = self.get("/health")
        self.assertEqual(health_status, 200)
        health = json.loads(health_body)
        self.assertTrue(health["ok"])
        self.assertEqual(health["service"], "local-library")
        self.assertTrue(health["runtime_running"])
        return transport, response_body, response_headers

    def session_cookie(self) -> str:
        cookie = next(
            value
            for value in self.cookies
            if value.name == self.session_authority.cookie_name
        )
        return f"{cookie.name}={cookie.value}"

    def test_project_listing_and_unassigned_are_human_readable(self) -> None:
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Readable Codex Project", body)
        self.assertIn("Readable ChatGPT Project", body)
        self.assertIn("Unnamed ChatGPT Project", body)
        self.assertNotIn("g-p-00000000000000004444444444444444", body)
        self.assertIn("Unassigned", body)
        self.assertIn("No deterministic Project", body)
        self.assertIn("default-src &#x27;none&#x27;".replace("&#x27;", "'"), headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_opaque_project_and_search_metadata_use_only_safe_display_label(self) -> None:
        project_query = urllib.parse.urlencode(
            {"project_id": self.opaque_chat.project_id}
        )
        _, project_body, _ = self.get("/project?" + project_query)
        self.assertIn("Unnamed ChatGPT Project", project_body)
        self.assertNotIn("g-p-00000000000000004444444444444444", project_body)

        _, results, _ = self.get(
            "/search?" + urllib.parse.urlencode({"q": "opaqueprojectsearchmarker"})
        )
        self.assertIn("Unnamed ChatGPT Project", results)
        self.assertNotIn("g-p-00000000000000004444444444444444", results)

    def test_project_derived_chat_titles_use_distinct_labeled_excerpts(self) -> None:
        project_query = urllib.parse.urlencode(
            {"project_id": self.web_chat_one.project_id}
        )
        _, body, _ = self.get("/project?" + project_query)
        self.assertNotIn("ChatGPT - Readable ChatGPT Project", body)
        self.assertIn("First user message: “web project content”", body)
        self.assertIn("First user message: “second distinct web project message”", body)
        self.assertIn("Aug 10, 2026", body)
        self.assertIn("AM", body)
        self.assertNotIn("2026-08-10T07:31:04.538284+00:00", body)

    def test_opaque_codex_session_title_is_presented_as_a_local_fallback(self) -> None:
        with database(self.database_path) as connection:
            connection.execute(
                "UPDATE chats SET display_title = ? WHERE chat_id = ?",
                ("Codex 019fe9b8", self.user.chat_id),
            )

        project_query = urllib.parse.urlencode({"project_id": self.user.project_id})
        _, project_body, _ = self.get("/project?" + project_query)
        self.assertNotIn("Codex 019fe9b8", project_body)
        self.assertIn("Local fallback", project_body)
        self.assertIn("First user message", project_body)
        self.assertIn("librarychatsearchmarker user message", project_body)

        chat_query = urllib.parse.urlencode({"chat_id": self.user.chat_id})
        _, chat_body, _ = self.get("/chat?" + chat_query)
        self.assertNotIn("Codex 019fe9b8", chat_body)
        self.assertIn("Local fallback", chat_body)
        self.assertIn("librarychatsearchmarker user message", chat_body)

    def test_trustworthy_chat_title_is_labeled_as_host_metadata(self) -> None:
        with database(self.database_path) as connection:
            connection.execute(
                "UPDATE chats SET display_title = ? WHERE chat_id = ?",
                ("A trustworthy host conversation title", self.web_chat_one.chat_id),
            )

        chat_query = urllib.parse.urlencode({"chat_id": self.web_chat_one.chat_id})
        _, body, _ = self.get("/chat?" + chat_query)
        self.assertIn("A trustworthy host conversation title", body)
        self.assertIn("Host title", body)
        self.assertNotIn("Local fallback", body)

    def test_presentation_requests_do_not_change_stored_names_identities_or_timestamps(self) -> None:
        def stored_rows() -> list[tuple[object, ...]]:
            with database(self.database_path) as connection:
                return [
                    tuple(row)
                    for row in connection.execute(
                        """
                        SELECT p.project_id, p.host_project_reference, p.display_name,
                               c.chat_id, c.host_chat_reference, c.display_title,
                               c.created_at, c.updated_at
                        FROM projects p LEFT JOIN chats c ON c.project_id = p.project_id
                        ORDER BY p.project_id, c.chat_id
                        """
                    ).fetchall()
                ]

        before = stored_rows()
        self.get("/")
        self.get(
            "/project?"
            + urllib.parse.urlencode({"project_id": self.web_chat_one.project_id})
        )
        self.get(
            "/chat?" + urllib.parse.urlencode({"chat_id": self.web_chat_one.chat_id})
        )
        self.get(
            "/search?" + urllib.parse.urlencode({"q": "opaqueprojectsearchmarker"})
        )
        self.assertEqual(stored_rows(), before)

    def test_project_chat_messages_and_document_navigation(self) -> None:
        project_query = urllib.parse.urlencode({"project_id": self.user.project_id})
        _, project_body, _ = self.get("/project?" + project_query)
        self.assertIn("Chat library-chat", project_body)
        self.assertIn("TEST_B.txt", project_body)
        self.assertNotIn("TEST_A.md", project_body)
        self.assertIn(str(self.test_b.resolve()), project_body)
        self.assertIn("Authorized sources", project_body)
        self.assertIn("Watching automatically", project_body)
        self.assertIn("Rescan now", project_body)

        chat_query = urllib.parse.urlencode({"chat_id": self.user.chat_id})
        _, chat_body, _ = self.get("/chat?" + chat_query)
        self.assertLess(
            chat_body.index("librarychatsearchmarker user message"),
            chat_body.index("assistant response follows user"),
        )
        self.assertIn("message user", chat_body)
        self.assertIn("message assistant", chat_body)

        document_query = urllib.parse.urlencode({"document_id": self.document_id})
        _, document_body, _ = self.get("/document?" + document_query)
        self.assertIn("librarydocumentsearchmarker survivor", document_body)
        self.assertIn("Active source status", document_body)

    def test_search_results_navigate_to_message_and_document(self) -> None:
        _, chat_results, _ = self.get("/search?" + urllib.parse.urlencode({"q": "librarychatsearchmarker"}))
        self.assertIn("Message", chat_results)
        self.assertIn("/chat?chat_id=", chat_results)
        self.assertIn("#message-", chat_results)

        _, document_results, _ = self.get("/search?" + urllib.parse.urlencode({"q": "librarydocumentsearchmarker"}))
        self.assertIn("Document", document_results)
        self.assertIn("/document?document_id=", document_results)
        self.assertIn("TEST_B.txt", document_results)

    def test_malformed_fts_syntax_is_a_safe_local_search_response(self) -> None:
        _, punctuation_only, _ = self.get(
            "/search?" + urllib.parse.urlencode({"q": "--- * () ::"})
        )
        self.assertIn("query must contain searchable text", punctuation_only)
        self.assertNotIn("Traceback", punctuation_only)
        self.assertNotIn("OperationalError", punctuation_only)
        self.assertNotIn("fts5:", punctuation_only.casefold())

        _, operators, _ = self.get(
            "/search?" + urllib.parse.urlencode({"q": 'OR NEAR * "quoted"'})
        )
        self.assertEqual(operators.count("Search results"), 1)
        self.assertNotIn("Traceback", operators)
        self.assertNotIn("OperationalError", operators)

    def test_elect_and_revoke_actions_use_registry_without_deleting_file(self) -> None:
        disposable = self.root / "UI_DISPOSABLE.md"
        disposable.write_text("uidisposablemarker", encoding="utf-8")
        common = {
            "csrf_token": self.server.csrf_token,
            "project_id": self.user.project_id,
        }
        status, body = self.post(
            "/source/authorize",
            {**common, "source_kind": "FILE", "source_path": str(disposable)},
        )
        self.assertEqual(status, 200)
        self.assertIn("Source authorized and reconciled", body)
        source = next(
            value
            for value in list_elected_sources(self.database_path, self.user.project_id)
            if value["display_name"] == disposable.name
        )
        self.assertTrue(
            any(
                value["display_name"] == disposable.name
                for value in list_documents(self.database_path, self.user.project_id)
            )
        )

        status, body = self.post(
            "/source/revoke",
            {
                **common,
                "source_id": str(source["source_id"]),
                "confirm": "yes",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("Physical files were not deleted", body)
        self.assertTrue(disposable.is_file())
        self.assertFalse(
            any(
                value["display_name"] == disposable.name
                for value in list_documents(self.database_path, self.user.project_id)
            )
        )

    def test_mutation_requires_csrf_and_server_is_loopback_only(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        disposable = self.root / "CSRF_BLOCKED.txt"
        disposable.write_text("must not be elected", encoding="utf-8")
        payload = urllib.parse.urlencode(
            {
                "csrf_token": "wrong-token",
                "project_id": self.user.project_id,
                "source_kind": "FILE",
                "source_path": str(disposable),
            }
        ).encode("utf-8")
        self.rejected_request(
            "POST",
            "/source/authorize",
            body=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": self.session_cookie(),
                "Origin": self.base_url,
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertFalse(
            any(
                value["display_name"] == disposable.name
                for value in list_elected_sources(self.database_path, self.user.project_id)
            )
        )
        status, body, _ = self.get("/health")
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(health["service"], "local-library")
        self.assertTrue(health["runtime_running"])
        self.assertEqual(set(health), {"ok", "service", "runtime_running"})
        management_health = self.server.health_snapshot()
        self.assertTrue(management_health["database"]["healthy"])
        self.assertIn(
            management_health["source_maintenance"]["state"], {"running", "paused"}
        )

    def test_directory_election_and_revocation_keep_physical_tree(self) -> None:
        directory = self.root / "UI_DIRECTORY"
        directory.mkdir()
        nested = directory / "nested.md"
        nested.write_text("uidirectorymarker", encoding="utf-8")
        common = {
            "csrf_token": self.server.csrf_token,
            "project_id": self.user.project_id,
        }
        status, _ = self.post(
            "/source/authorize",
            {**common, "source_kind": "DIRECTORY", "source_path": str(directory)},
        )
        self.assertEqual(status, 200)
        source = next(
            value
            for value in list_elected_sources(self.database_path, self.user.project_id)
            if value["display_name"] == directory.name
        )
        self.assertEqual(source["source_kind"], "DIRECTORY")
        self.assertTrue(
            any(
                value["display_name"] == nested.name
                for value in list_documents(self.database_path, self.user.project_id)
            )
        )
        status, _ = self.post(
            "/source/revoke",
            {**common, "source_id": str(source["source_id"]), "confirm": "yes"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(directory.is_dir())
        self.assertTrue(nested.is_file())


    def test_library_content_requires_runtime_session_and_does_not_expose_csrf(self) -> None:
        status, body, _ = self.raw_request("GET", "/")
        self.assertEqual(status, 401)
        self.assertNotIn("library-view-test-csrf-token", body)
        self.assertNotIn("Readable Codex Project", body)

        forged, forged_body, _ = self.raw_request(
            "GET",
            "/",
            headers={"Cookie": "atomizer_library_session=forged-session"},
        )
        self.assertEqual(forged, 401)
        self.assertNotIn("Readable Codex Project", forged_body)

    def test_library_requires_exact_host_and_rejects_missing_host(self) -> None:
        hostile, hostile_body, _ = self.raw_request(
            "GET",
            "/",
            headers={
                "Host": "attacker.example",
                "Cookie": self.session_cookie(),
            },
            skip_host=True,
        )
        self.assertEqual(hostile, 403)
        self.assertNotIn("Readable Codex Project", hostile_body)

        missing, missing_body, _ = self.raw_request(
            "GET",
            "/",
            headers={"Cookie": self.session_cookie()},
            skip_host=True,
        )
        self.assertEqual(missing, 403)
        self.assertNotIn("Readable Codex Project", missing_body)

    def test_library_post_requires_exact_origin_and_non_cross_site_fetch(self) -> None:
        payload = urllib.parse.urlencode(
            {
                "csrf_token": "library-view-test-csrf-token",
                "integration": "chatgpt_web",
                "enabled": "yes",
            }
        ).encode("utf-8")
        common = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": self.session_cookie(),
        }
        permission_before = self.server.permission_store.snapshot()["chatgpt_web"]
        self.rejected_request(
            "POST",
            "/integration/set",
            headers={**common, "Origin": "https://attacker.example"},
            body=payload,
        )
        self.assertEqual(
            self.server.permission_store.snapshot()["chatgpt_web"], permission_before
        )
        self.rejected_request(
            "POST",
            "/integration/set",
            headers={
                **common,
                "Origin": self.base_url,
                "Sec-Fetch-Site": "cross-site",
            },
            body=payload,
        )
        self.assertEqual(
            self.server.permission_store.snapshot()["chatgpt_web"], permission_before
        )

    def test_launch_capability_is_single_use_and_expired_capability_is_rejected(self) -> None:
        capability = self.session_authority.issue_launch()
        established, _, headers = self.raw_request("GET", "/?launch=" + capability)
        self.assertEqual(established, 303)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertNotIn("Secure", headers["Set-Cookie"])

        replayed, _, _ = self.raw_request("GET", "/?launch=" + capability)
        self.assertEqual(replayed, 403)

        now = [1000.0]
        previous_clock = self.session_authority.clock
        try:
            self.session_authority.clock = lambda: now[0]
            expired = self.session_authority.issue_launch()
            now[0] += self.session_authority.launch_ttl_seconds + 1
            rejected, _, _ = self.raw_request("GET", "/?launch=" + expired)
            self.assertEqual(rejected, 403)
        finally:
            self.session_authority.clock = previous_clock


class Win32RejectionTransportTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "requires Windows socket semantics")
    def test_abort_handling_is_narrow_and_requires_server_health(self) -> None:
        case = LibraryViewTests()
        case.thread = mock.Mock()
        case.thread.is_alive.return_value = True
        case.get = mock.Mock(
            return_value=(
                200,
                '{"ok":true,"service":"local-library","runtime_running":true}',
                {},
            )
        )

        case.raw_request = mock.Mock(
            side_effect=ConnectionAbortedError(10053, "expected Windows rejection")
        )
        transport, body, headers = case.rejected_request("POST", "/rejected")
        self.assertEqual(transport, "win32-10053")
        self.assertEqual(body, "")
        self.assertEqual(headers, {})
        case.get.assert_called_once_with("/health")

        case.raw_request = mock.Mock(return_value=(200, "accepted", {}))
        with self.assertRaises(AssertionError):
            case.rejected_request("POST", "/must-not-be-accepted")

        case.raw_request = mock.Mock(
            side_effect=ConnectionRefusedError(10061, "unrelated connection failure")
        )
        with self.assertRaises(ConnectionRefusedError):
            case.rejected_request("POST", "/unrelated-failure")

        case.raw_request = mock.Mock(
            side_effect=ConnectionAbortedError(10053, "expected Windows rejection")
        )
        case.thread.is_alive.return_value = False
        with self.assertRaises(AssertionError):
            case.rejected_request("POST", "/dead-server")


if __name__ == "__main__":

    unittest.main()
