"""Internal privileged router over the existing read-only Library query service."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from atomizer_local_client.managed_access.authority import (
    ManagedAuthorityRegistry,
    bound_scope,
)
from atomizer_local_client.memory_access.access_gate import LibraryCaller
from atomizer_local_client.memory_access.query_service import LibraryQueryService


_OPERATIONS = frozenset(
    {
        "search_library",
        "get_library_item",
        "recent_library_context",
        "list_library_projects",
    }
)


def _text(value: object, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return normalized


class ManagedLibraryReader:
    """Require channel authority before selecting the trusted-manager caller."""

    def __init__(
        self,
        database_path: Path,
        service: LibraryQueryService,
        authority: ManagedAuthorityRegistry,
    ) -> None:
        self.database_path = Path(database_path)
        self.service = service
        self.authority = authority

    def _connection(self) -> sqlite3.Connection:
        path = self.database_path.resolve()
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def _project_id(self, host: str, scope_reference: str) -> str:
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT project_id FROM projects WHERE host = ? AND host_project_reference = ?",
                (host, scope_reference),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("managed scope has no matching Library project")
        return str(row["project_id"])

    def _item_project_id(self, item_id: str) -> str:
        connection = self._connection()
        try:
            row = connection.execute(
                """
                SELECT u.project_id FROM claim_evidence e
                JOIN semantic_units u ON u.semantic_unit_id = e.semantic_unit_id
                WHERE e.evidence_id = ?
                UNION
                SELECT c.project_id FROM messages m
                JOIN chats c ON c.chat_id = m.chat_id WHERE m.message_id = ?
                UNION
                SELECT d.project_id FROM documents d WHERE d.document_id = ?
                """,
                (item_id, item_id, item_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("Library item was not found")
        return str(row["project_id"])

    def call(
        self,
        capability: str,
        host: str,
        scope_reference: str,
        host_session_reference: str,
        host_turn_reference: str,
        operation: str,
        arguments: dict[str, object],
    ) -> dict[str, Any]:
        self.authority.require(
            capability,
            bound_scope(host, scope_reference),
            host_session_reference=host_session_reference,
            host_turn_reference=host_turn_reference,
        )
        if operation not in _OPERATIONS:
            raise ValueError("unknown managed Library operation")
        if not isinstance(arguments, dict):
            raise ValueError("managed Library arguments must be an object")
        project_id = self._project_id(host, scope_reference)
        caller = LibraryCaller.TRUSTED_MANAGER
        if operation == "search_library":
            if set(arguments) - {"query", "limit"}:
                raise ValueError("unexpected search_library arguments")
            return self.service.search_library(
                _text(arguments.get("query"), "query", 512),
                project_id,
                arguments.get("limit"),  # type: ignore[arg-type]
                caller=caller,
            )
        if operation == "recent_library_context":
            if set(arguments) - {"limit"}:
                raise ValueError("unexpected recent_library_context arguments")
            return self.service.recent_library_context(
                project_id,
                arguments.get("limit"),  # type: ignore[arg-type]
                caller=caller,
            )
        if operation == "get_library_item":
            if set(arguments) != {"id"}:
                raise ValueError("get_library_item requires only id")
            item_id = _text(arguments.get("id"), "id")
            if self._item_project_id(item_id) != project_id:
                raise PermissionError("managed Library item scope mismatch")
            return self.service.get_library_item(item_id, caller=caller)
        if arguments:
            raise ValueError("list_library_projects accepts no arguments")
        result = self.service.list_library_projects(caller=caller)
        result["items"] = [
            item for item in result.get("items", []) if item.get("project_id") == project_id
        ]
        result["result_count"] = len(result["items"])
        return result


__all__ = ["ManagedLibraryReader"]
