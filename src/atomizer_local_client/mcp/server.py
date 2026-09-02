"""Dependency-free, read-only MCP 2026-07-28 server over stdio."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, TextIO

from atomizer_local_client.memory_access.access_gate import (
    DirectLibraryAccessMode,
    LibraryAccessGate,
)
from atomizer_local_client.memory_access.query_service import (
    LibraryQueryError,
    LibraryQueryService,
)
from atomizer_local_client.managed_access.policy import (
    LibraryAccessPolicyStore,
    PolicyBackedAccessGate,
)
from atomizer_local_client.memory_access.contracts import InactiveManagedAuthorityProvider
from atomizer_local_client.mcp.contracts import TOOLS
from atomizer_local_client.mcp.tools import LibraryToolRouter, ToolArgumentsError
from atomizer_local_client.runtime.configuration import RuntimePaths
from atomizer_local_client.runtime_health import runtime_version


PROTOCOL_VERSION = "2026-07-28"
SERVER_NAME = "context-atomizer-local-library"
PROTOCOL_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"


class StdioMcpServer:
    def __init__(self, router: LibraryToolRouter, *, diagnostics: TextIO | None = None) -> None:
        self.router = router
        self.diagnostics = diagnostics or sys.stderr

    @staticmethod
    def _server_meta() -> dict[str, Any]:
        return {SERVER_INFO_META_KEY: {"name": SERVER_NAME, "version": runtime_version()}}

    def _complete(self, request_id: object, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"resultType": "complete", **result, "_meta": self._server_meta()},
        }

    @staticmethod
    def _error(request_id: object, code: int, message: str, data: object = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    @staticmethod
    def _request_metadata(request: dict[str, Any]) -> dict[str, Any] | None:
        params = request.get("params")
        if not isinstance(params, dict):
            return None
        metadata = params.get("_meta")
        return metadata if isinstance(metadata, dict) else None

    def handle(self, request: object) -> dict[str, Any] | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        if "id" not in request:
            return None
        metadata = self._request_metadata(request)
        if metadata is None or metadata.get(PROTOCOL_META_KEY) != PROTOCOL_VERSION:
            return self._error(
                request_id,
                -32022,
                "Unsupported protocol version",
                {"supported": [PROTOCOL_VERSION]},
            )
        if not isinstance(metadata.get(CLIENT_CAPABILITIES_META_KEY), dict):
            return self._error(request_id, -32602, "Invalid request metadata")
        client_info = metadata.get(CLIENT_INFO_META_KEY)
        if client_info is not None and (
            not isinstance(client_info, dict)
            or not isinstance(client_info.get("name"), str)
            or not isinstance(client_info.get("version"), str)
        ):
            return self._error(request_id, -32602, "Invalid request metadata")
        method = request.get("method")
        if method == "server/discover":
            return self._complete(
                request_id,
                {
                    "supportedVersions": [PROTOCOL_VERSION],
                    "capabilities": {"tools": {}},
                    "instructions": "Use the read-only tools for bounded local Library retrieval.",
                    "ttlMs": 0,
                    "cacheScope": "private",
                },
            )
        if method == "tools/list":
            return self._complete(
                request_id,
                {"tools": list(TOOLS), "ttlMs": 0, "cacheScope": "private"},
            )
        if method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict):
                return self._error(request_id, -32602, "Invalid params")
            try:
                payload = self.router.call(params.get("name"), params.get("arguments", {}))
            except (ToolArgumentsError, LibraryQueryError, TypeError, ValueError) as error:
                return self._error(request_id, -32602, str(error))
            return self._complete(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        }
                    ],
                    "isError": False,
                },
            )
        return self._error(request_id, -32601, "Method not found")

    def serve(self, input_stream: TextIO, output_stream: TextIO) -> int:
        for line in input_stream:
            try:
                request = json.loads(line)
                response = self.handle(request)
            except json.JSONDecodeError:
                response = self._error(None, -32700, "Parse error")
            except Exception as error:  # fail closed without putting diagnostics on stdout
                print(f"{SERVER_NAME}: {type(error).__name__}", file=self.diagnostics, flush=True)
                response = self._error(None, -32603, "Internal error")
            if response is not None:
                output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                output_stream.flush()
        return 0


def _database_path(argument: Path | None) -> Path:
    if argument is not None:
        return argument
    configured = os.environ.get("ATOMIZER_LIBRARY_DATABASE")
    return Path(configured) if configured else RuntimePaths.current_user().database


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Context Atomizer Local Library MCP server")
    parser.add_argument("--database", type=Path)
    parser.add_argument(
        "--access-mode",
        choices=tuple(mode.value for mode in DirectLibraryAccessMode),
        default=os.environ.get("ATOMIZER_DIRECT_LIBRARY_MODE"),
    )
    arguments = parser.parse_args()
    database_path = _database_path(arguments.database)
    policy = LibraryAccessPolicyStore(
        database_path.parent / "library-access-policy.json"
    )
    if policy.path.exists():
        gate = PolicyBackedAccessGate(
            policy,
            manager=InactiveManagedAuthorityProvider(),
        )
    elif arguments.access_mode is not None:
        gate = LibraryAccessGate(DirectLibraryAccessMode(arguments.access_mode))
    else:
        gate = PolicyBackedAccessGate(
            policy,
            manager=InactiveManagedAuthorityProvider(),
        )
    service = LibraryQueryService(database_path, gate=gate)
    return StdioMcpServer(LibraryToolRouter(service)).serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
