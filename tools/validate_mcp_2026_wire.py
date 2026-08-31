"""Validate actual Library MCP frames against the official 2026-07-28 schema."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


TOOLS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from validate_portable_plugin import _object, _validate_schema  # noqa: E402
from atomizer_local_client.mcp.server import StdioMcpServer  # noqa: E402


RECEIPT = TOOLS_ROOT / "mcp_2026_wire_receipt.json"
PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"


class _ConformanceRouter:
    names = (
        "search_library",
        "get_library_item",
        "recent_library_context",
        "list_library_projects",
    )

    @staticmethod
    def call(name: object, arguments: object) -> dict[str, object]:
        if name != "list_library_projects" or arguments != {}:
            raise ValueError("unexpected conformance tool call")
        return {"status": "ok", "items": [], "result_count": 0}


def _request(request_id: int, method: str, **params: object) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {
            **params,
            "_meta": {
                PROTOCOL_META_KEY: PROTOCOL_VERSION,
                CLIENT_CAPABILITIES_META_KEY: {},
                CLIENT_INFO_META_KEY: {"name": "wire-conformance", "version": "1.0.0"},
            },
        },
    }


def _definition(schema: dict[str, Any], name: str) -> dict[str, Any]:
    value = schema.get("$defs", {}).get(name)
    if not isinstance(value, dict):
        raise ValueError(f"official schema is missing definition: {name}")
    return value


def _validate(
    value: object,
    schema: dict[str, Any],
    definition: str,
) -> None:
    _validate_schema(value, _definition(schema, definition), schema, definition)


def validate(schema_path: Path) -> dict[str, object]:
    receipt = _object(RECEIPT)
    schema_bytes = Path(schema_path).read_bytes()
    digest = hashlib.sha256(schema_bytes).hexdigest()
    if digest != receipt["schema_sha256"]:
        raise ValueError("official MCP schema hash does not match the pinned release receipt")
    schema = json.loads(schema_bytes)
    if not isinstance(schema, dict):
        raise ValueError("official MCP schema must contain an object")

    server = StdioMcpServer(_ConformanceRouter())  # type: ignore[arg-type]
    exchanges = (
        ("DiscoverRequest", "DiscoverResultResponse", _request(1, "server/discover")),
        ("ListToolsRequest", "ListToolsResultResponse", _request(2, "tools/list")),
        (
            "CallToolRequest",
            "CallToolResultResponse",
            _request(3, "tools/call", name="list_library_projects", arguments={}),
        ),
    )
    for request_definition, response_definition, request in exchanges:
        _validate(request, schema, request_definition)
        response = server.handle(request)
        if response is None:
            raise ValueError(f"server did not answer {request_definition}")
        _validate(response, schema, response_definition)
        result = response.get("result")
        if not isinstance(result, dict) or result.get("resultType") != "complete":
            raise ValueError(f"{response_definition} is not a complete result")
        metadata = result.get("_meta")
        if not isinstance(metadata, dict) or SERVER_INFO_META_KEY not in metadata:
            raise ValueError(f"{response_definition} omits server identity metadata")

    for method in ("server/discover", "tools/list"):
        result = server.handle(_request(4, method))["result"]
        if result.get("ttlMs") != 0 or result.get("cacheScope") != "private":
            raise ValueError(f"{method} omits exact private cache fields")

    initialize = server.handle(_request(5, "initialize"))
    _validate(initialize, schema, "JSONRPCErrorResponse")
    if initialize["error"]["code"] != -32601:
        raise ValueError("legacy initialize was not rejected as an absent modern method")

    cancelled = _request(6, "notifications/cancelled", requestId=5, reason="test")
    cancelled.pop("id")
    _validate(cancelled, schema, "CancelledNotification")
    if server.handle(cancelled) is not None:
        raise ValueError("a client notification produced a response")
    initialized = _request(7, "notifications/initialized")
    initialized.pop("id")
    if server.handle(initialized) is not None:
        raise ValueError("legacy initialized notification produced a response")

    return {
        "passed": True,
        "protocol_version": PROTOCOL_VERSION,
        "schema_sha256": digest,
        "validated_exchanges": len(exchanges),
        "initialize_error": -32601,
        "notification_responses": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schema", type=Path, help="downloaded official tagged schema.json")
    try:
        result = validate(parser.parse_args().schema)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"passed": False, "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
