from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

from test_support import SOURCE_ROOT, TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.derived_state.cycle import run_derived_state_cycle
from atomizer_local_client.mcp.contracts import TOOLS
from atomizer_local_client.mcp.server import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_META_KEY,
    PROTOCOL_VERSION,
    StdioMcpServer,
)
from atomizer_local_client.mcp.tools import LibraryToolRouter
from atomizer_local_client.memory_access.query_service import LibraryQueryService
from atomizer_local_client.runtime_health import runtime_version


def request(request_id: int, method: str, **params: object) -> dict[str, object]:
    # Literal envelope from the official 2026-07-28 discovery example. Do not
    # derive its wire keys or values from the implementation under test.
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {
            **params,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {
                    "name": "portable-library-tests",
                    "version": "1.0.0",
                },
            },
        },
    }


class LibraryMcpTests(TemporaryDatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        ingest_chat_event(
            self.database_path,
            chat_event(event_id="mcp-memory", content="Project Atlas uses amber storage."),
        )
        run_derived_state_cycle(self.database_path)
        self.server = StdioMcpServer(LibraryToolRouter(LibraryQueryService(self.database_path)))

    def test_discovery_and_tool_list_are_exactly_mcp_2026_07_28(self) -> None:
        self.assertEqual(PROTOCOL_META_KEY, "io.modelcontextprotocol/protocolVersion")
        self.assertEqual(CLIENT_CAPABILITIES_META_KEY, "io.modelcontextprotocol/clientCapabilities")
        self.assertEqual(CLIENT_INFO_META_KEY, "io.modelcontextprotocol/clientInfo")
        self.assertEqual(PROTOCOL_VERSION, "2026-07-28")
        discovery = self.server.handle(request(1, "server/discover"))
        self.assertEqual(
            set(discovery["result"]),
            {
                "resultType", "supportedVersions", "capabilities", "instructions",
                "ttlMs", "cacheScope", "_meta",
            },
        )
        self.assertEqual(discovery["result"]["supportedVersions"], ["2026-07-28"])
        self.assertEqual(discovery["result"]["resultType"], "complete")
        self.assertNotIn("serverInfo", discovery["result"])
        self.assertEqual(
            discovery["result"]["_meta"],
            {
                "io.modelcontextprotocol/serverInfo": {
                    "name": "context-atomizer-local-library",
                    "version": runtime_version(),
                }
            },
        )

        listed = self.server.handle(request(2, "tools/list"))
        self.assertEqual(
            set(listed["result"]),
            {"resultType", "tools", "ttlMs", "cacheScope", "_meta"},
        )
        self.assertEqual(listed["result"]["resultType"], "complete")
        self.assertEqual(listed["result"]["ttlMs"], 0)
        self.assertEqual(listed["result"]["cacheScope"], "private")
        self.assertEqual(
            [tool["name"] for tool in listed["result"]["tools"]],
            ["search_library", "get_library_item", "recent_library_context", "list_library_projects"],
        )

    def test_server_rejects_legacy_initialize_and_missing_or_wrong_protocol(self) -> None:
        initialize = self.server.handle(
            request(1, "initialize")
        )
        self.assertEqual(initialize["error"]["code"], -32601)
        wrong = request(2, "tools/list")
        wrong["params"]["_meta"][PROTOCOL_META_KEY] = "2025-11-25"
        self.assertEqual(self.server.handle(wrong)["error"]["code"], -32022)
        missing_capabilities = request(3, "tools/list")
        del missing_capabilities["params"]["_meta"][CLIENT_CAPABILITIES_META_KEY]
        self.assertEqual(self.server.handle(missing_capabilities)["error"]["code"], -32602)

    def test_modern_notifications_get_no_response_and_no_initialized_handshake(self) -> None:
        cancelled = request(8, "notifications/cancelled", requestId=7, reason="test")
        del cancelled["id"]
        initialized = request(9, "notifications/initialized")
        del initialized["id"]
        self.assertIsNone(self.server.handle(cancelled))
        self.assertIsNone(self.server.handle(initialized))

        input_stream = io.StringIO(
            json.dumps(cancelled) + "\n" + json.dumps(initialized) + "\n" + json.dumps(request(10, "tools/list")) + "\n"
        )
        output_stream = io.StringIO()
        self.assertEqual(self.server.serve(input_stream, output_stream), 0)
        lines = output_stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["id"], 10)

    def test_tools_are_read_only_closed_and_cannot_claim_manager_privilege(self) -> None:
        self.assertEqual({tool["name"] for tool in TOOLS}, set(self.server.router.names))
        for tool in TOOLS:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
            serialized = json.dumps(tool).casefold()
            for forbidden in ("write", "delete", "sql", "caller", "admin", "bypass", "manager"):
                self.assertNotIn(f'"{forbidden}"', serialized)
            self.assertFalse(tool["inputSchema"].get("additionalProperties", True))

        forged = self.server.handle(
            request(
                3,
                "tools/call",
                name="search_library",
                arguments={"query": "amber", "caller": "TRUSTED_MANAGER"},
            )
        )
        self.assertEqual(forged["error"]["code"], -32602)

    def test_tool_call_returns_bounded_content(self) -> None:
        response = self.server.handle(
            request(4, "tools/call", name="search_library", arguments={"query": "amber", "limit": 8})
        )
        self.assertEqual(
            set(response["result"]),
            {"resultType", "content", "isError", "_meta"},
        )
        self.assertEqual(response["result"]["resultType"], "complete")
        self.assertFalse(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "ok")
        self.assertGreater(payload["result_count"], 0)

    def test_stdio_separates_protocol_output_and_diagnostics(self) -> None:
        input_stream = io.StringIO(json.dumps(request(5, "tools/list")) + "\nnot-json\n")
        output_stream = io.StringIO()
        diagnostics = io.StringIO()
        server = StdioMcpServer(self.server.router, diagnostics=diagnostics)
        self.assertEqual(server.serve(input_stream, output_stream), 0)
        lines = output_stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(json.loads(line)["jsonrpc"] == "2.0" for line in lines))
        self.assertEqual(json.loads(lines[1])["error"]["code"], -32700)
        self.assertEqual(diagnostics.getvalue(), "")

    def test_spawned_stdio_process_starts_serves_and_shuts_down_cleanly(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(SOURCE_ROOT)}
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "atomizer_local_client.mcp.server",
                "--database",
                str(self.database_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
            cwd=Path(__file__).resolve().parents[1],
        )
        stdout, stderr = process.communicate(json.dumps(request(6, "server/discover")) + "\n", timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(stdout.splitlines()), 1)
        self.assertEqual(json.loads(stdout)["result"]["supportedVersions"], [PROTOCOL_VERSION])

    def test_managed_exclusive_process_returns_status_without_library_content(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(SOURCE_ROOT)}
        call = request(
            7,
            "tools/call",
            name="search_library",
            arguments={"query": "amber"},
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "atomizer_local_client.mcp.server",
                "--database",
                str(self.database_path),
                "--access-mode",
                "MANAGED_EXCLUSIVE",
            ],
            input=json.dumps(call) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(json.loads(completed.stdout)["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "managed_exclusive")
        self.assertEqual(payload["items"], [])
        self.assertNotIn("amber", json.dumps(payload).casefold())


if __name__ == "__main__":
    import unittest

    unittest.main()
