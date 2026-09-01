"""Argument validation and direct-frontier dispatch for MCP tools."""

from __future__ import annotations

from typing import Any, Callable

from atomizer_local_client.memory_access.access_gate import LibraryCaller
from atomizer_local_client.memory_access.query_service import LibraryQueryService


class ToolArgumentsError(ValueError):
    pass


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolArgumentsError("tool arguments must be an object")
    return value


def _closed(arguments: dict[str, Any], allowed: set[str]) -> None:
    unexpected = set(arguments) - allowed
    if unexpected:
        raise ToolArgumentsError(f"unsupported tool arguments: {', '.join(sorted(unexpected))}")


class LibraryToolRouter:
    """The caller identity is fixed here and cannot come from MCP arguments."""

    caller = LibraryCaller.DIRECT_FRONTIER

    def __init__(self, service: LibraryQueryService) -> None:
        self.service = service
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "search_library": self._search,
            "get_library_item": self._get,
            "recent_library_context": self._recent,
            "list_library_projects": self._projects,
        }

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def call(self, name: object, arguments: object) -> dict[str, Any]:
        if not isinstance(name, str) or name not in self._handlers:
            raise ToolArgumentsError("unknown Library tool")
        return self._handlers[name](_object(arguments))

    def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _closed(arguments, {"query", "project", "limit"})
        return self.service.search_library(
            arguments.get("query"),
            arguments.get("project"),
            arguments.get("limit"),
            caller=self.caller,
        )

    def _get(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _closed(arguments, {"id"})
        return self.service.get_library_item(arguments.get("id"), caller=self.caller)

    def _recent(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _closed(arguments, {"project", "limit"})
        return self.service.recent_library_context(
            arguments.get("project"), arguments.get("limit"), caller=self.caller
        )

    def _projects(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _closed(arguments, set())
        return self.service.list_library_projects(caller=self.caller)
