"""Closed MCP tool declarations for the direct frontier surface."""

from __future__ import annotations

from typing import Any


READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "search_library",
        "description": "Search the existing local Library with deterministic lexical, vector, fused, and reranked retrieval.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 512},
                "project": {"type": "string", "minLength": 1, "maxLength": 256},
                "limit": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY_ANNOTATIONS,
    },
    {
        "name": "get_library_item",
        "description": "Read one Library evidence or source item by its stable identifier.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "minLength": 1, "maxLength": 256}
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY_ANNOTATIONS,
    },
    {
        "name": "recent_library_context",
        "description": "Read bounded recent context from the existing local Library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "minLength": 1, "maxLength": 256},
                "limit": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            "additionalProperties": False,
        },
        "annotations": READ_ONLY_ANNOTATIONS,
    },
    {
        "name": "list_library_projects",
        "description": "List bounded local Library project identifiers and content-free counts.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "annotations": READ_ONLY_ANNOTATIONS,
    },
)
