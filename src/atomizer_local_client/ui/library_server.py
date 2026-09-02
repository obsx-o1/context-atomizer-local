"""Loopback-only server for the local human-facing Library view."""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
import socket
import threading
from http.cookies import CookieError, SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from atomizer_local_client.local_auth.library_session import LibrarySessionAuthority

from atomizer_local_client.library.document_registry import (
    authorize_directory,
    authorize_file_source,
    revoke_source_authorization,
    sync_elected_source,
)
from atomizer_local_client.library.source_maintenance import AutomaticSourceMaintainer
from atomizer_local_client.library.export_service import export_captured_library
from atomizer_local_client.derived_state.maintenance import AutomaticDerivedStateMaintainer
from atomizer_local_client.runtime_health import RuntimeIdentity, bridge_reachable, database_health
from atomizer_local_client.runtime.permissions import PermissionStore
from atomizer_local_client.managed_access.policy import LibraryAccessPolicyStore
from atomizer_local_client.memory_access.access_gate import DirectLibraryAccessMode
from atomizer_local_client.library.view_service import (
    list_projects,
    read_chat_view,
    read_document_view,
    read_project_overview,
    search_library,
)
from atomizer_local_client.ui.library_html import (
    render_chat,
    render_document,
    render_error,
    render_home,
    render_project,
    render_permissions,
    render_search,
    render_runtime_status,
)

_MAX_FORM_BYTES = 64 * 1024


class LibraryViewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(
        self,
        database_path: Path,
        port: int = 43118,
        *,
        csrf_token: str | None = None,
        automatic_maintenance: bool = True,
        maintenance_interval_seconds: float = 2.0,
        bridge_port: int = 43117,
        runtime_identity: RuntimeIdentity | None = None,
        extension_status_provider: Callable[[], dict[str, object]] | None = None,
        permission_store: PermissionStore | None = None,
        session_authority: LibrarySessionAuthority | None = None,
        pairing_code_provider: Callable[[], str] | None = None,
        pairing_revoke_callback: Callable[[], None] | None = None,
        access_policy: LibraryAccessPolicyStore | None = None,
        managed_status_provider: Callable[[], dict[str, object]] | None = None,
        managed_pairing_code_provider: Callable[[], str] | None = None,
        managed_pairing_revoke_callback: Callable[[], None] | None = None,
        access_mode_setter: Callable[[str], DirectLibraryAccessMode] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.bridge_port = int(bridge_port)
        self.runtime_identity = runtime_identity or RuntimeIdentity()
        self.extension_status_provider = extension_status_provider
        self.permission_store = permission_store or PermissionStore(
            self.database_path.parent / "permissions.json"
        )
        self.session_authority = session_authority or LibrarySessionAuthority()
        self.pairing_code_provider = pairing_code_provider
        self.pairing_revoke_callback = pairing_revoke_callback
        self.access_policy = access_policy or LibraryAccessPolicyStore(
            self.database_path.parent / "library-access-policy.json"
        )
        self.managed_status_provider = managed_status_provider
        self.managed_pairing_code_provider = managed_pairing_code_provider
        self.managed_pairing_revoke_callback = managed_pairing_revoke_callback
        self.access_mode_setter = access_mode_setter or self.access_policy.set_mode
        self.csrf_token = csrf_token or secrets.token_urlsafe(32)
        self.automatic_maintenance_enabled = automatic_maintenance
        self.source_operation_lock = threading.RLock()
        self.source_maintainer: AutomaticSourceMaintainer | None = None
        self.derived_state_maintainer: AutomaticDerivedStateMaintainer | None = None
        super().__init__(("127.0.0.1", port), LibraryViewHandler)
        actual_port = int(self.server_address[1])
        self.expected_host = f"127.0.0.1:{actual_port}"
        self.expected_origin = f"http://{self.expected_host}"
        if automatic_maintenance:
            self.source_maintainer = AutomaticSourceMaintainer(
                self.database_path,
                interval_seconds=maintenance_interval_seconds,
                operation_lock=self.source_operation_lock,
            )
            self.source_maintainer.start()
            self.derived_state_maintainer = AutomaticDerivedStateMaintainer(
                self.database_path,
                interval_seconds=maintenance_interval_seconds,
                operation_lock=self.source_operation_lock,
            )
            self.derived_state_maintainer.start()

    def server_close(self) -> None:
        if self.source_maintainer is not None:
            self.source_maintainer.stop()
        if self.derived_state_maintainer is not None:
            self.derived_state_maintainer.stop()
        super().server_close()

    def health_snapshot(self) -> dict[str, object]:
        runtime = self.runtime_identity.snapshot()
        maintainer = self.source_maintainer
        cycle = maintainer.last_cycle if maintainer is not None else None
        maintenance_state = "disabled" if maintainer is None else maintainer.health_state
        derived = self.derived_state_maintainer
        derived_health = (
            {
                "state": "disabled",
                "running": False,
                "last_successful_cycle": None,
                "pending_count": None,
                "units_indexed": None,
                "units_failed": None,
                "backend_version": "local-feature-hash-v1",
                "last_error_class": None,
                "convergence_state": "disabled",
            }
            if derived is None
            else derived.health_snapshot()
        )
        payload: dict[str, object] = {
            "ok": True,
            "service": "local-library",
            "runtime_running": True,
            "runtime": runtime,
            "database": database_health(self.database_path),
            "source_maintenance": {
                "state": maintenance_state,
                "running": bool(maintainer and maintainer.is_running),
                "last_cycle_error_count": None if cycle is None else len(cycle.errors),
            },
            "derived_state": derived_health,
            "browser_bridge": {
                "reachable": bridge_reachable(self.bridge_port),
                "port": self.bridge_port,
            },
            "extension": (
                self.extension_status_provider()
                if self.extension_status_provider is not None
                else {"state": "unknown", "protocol_version": None, "last_seen_at": None}
            ),
            "integrations": {
                name: {
                    "enabled": permission.enabled,
                    "installed": permission.installed,
                }
                for name, permission in self.permission_store.snapshot().items()
            },
            "library_access": {
                "mode": self._access_mode(),
                "managed_authority": (
                    self.managed_status_provider()
                    if self.managed_status_provider is not None
                    else {"verified_active": False, "expires_at": None}
                ),
            },
        }
        payload["ok"] = bool(
            payload["database"]["healthy"]
            and not runtime["restart_required"]
            and maintenance_state != "error"
            and derived_health["state"] != "error"
        )
        return payload

    def _access_mode(self) -> str:
        try:
            return self.access_policy.mode().value
        except ValueError:
            return DirectLibraryAccessMode.DISABLED.value


class LibraryViewHandler(BaseHTTPRequestHandler):
    server: LibraryViewServer
    server_version = "ContextAtomizerLibrary/0.1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        # Search terms and local paths can occur in URLs/forms; do not log request lines.
        return

    def _local_client(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _expected_host(self) -> bool:
        return self.headers.get("Host") == self.server.expected_host

    def _session_token(self) -> str:
        raw = self.headers.get("Cookie", "")
        try:
            cookies = SimpleCookie(raw)
        except CookieError:
            return ""
        morsel = cookies.get(self.server.session_authority.cookie_name)
        return "" if morsel is None else morsel.value

    def _authenticated(self) -> bool:
        return self.server.session_authority.authenticated(self._session_token())

    def _browser_post_allowed(self) -> bool:
        if self.headers.get("Origin") != self.server.expected_origin:
            return False
        fetch_site = self.headers.get("Sec-Fetch-Site")
        return fetch_site is None or fetch_site in {"same-origin", "none"}

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send_bytes(status, body)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._html(render_error(status.value, message), status)

    def _redirect(self, path: str, *, status: str) -> None:
        separator = "&" if "?" in path else "?"
        location = path + separator + urlencode({"status": status})
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _establish_session(self, session: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", self.server.session_authority.cookie_header(session))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _single(self, parameters: dict[str, list[str]], name: str) -> str:
        values = parameters.get(name, [])
        if len(values) != 1 or not values[0].strip():
            raise ValueError(f"{name} is required")
        return values[0].strip()

    def _form(self) -> dict[str, list[str]]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid form size") from error
        if not 0 < content_length <= _MAX_FORM_BYTES:
            raise ValueError("invalid form size")
        content_type = self.headers.get("Content-Type", "").partition(";")[0].strip()
        if content_type != "application/x-www-form-urlencoded":
            raise ValueError("form encoding is required")
        return parse_qs(
            self.rfile.read(content_length).decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
        )

    def _require_csrf(self, form: dict[str, list[str]]) -> None:
        supplied = self._single(form, "csrf_token")
        if not hmac.compare_digest(supplied, self.server.csrf_token):
            raise PermissionError("invalid local form token")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if not self._local_client() or not self._expected_host():
            self._error(HTTPStatus.FORBIDDEN, "The Library request was rejected.")
            return
        parsed = urlsplit(self.path)
        parameters = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/health":
            self._send_bytes(
                HTTPStatus.OK,
                b'{"ok":true,"service":"local-library","runtime_running":true}',
                content_type="application/json",
            )
            return
        if parsed.path == "/" and "launch" in parameters:
            values = parameters.get("launch", [])
            session = (
                self.server.session_authority.consume_launch(values[0])
                if len(values) == 1
                else None
            )
            if session is None:
                self._error(HTTPStatus.FORBIDDEN, "The Library launch was rejected.")
                return
            self._establish_session(session)
            return
        if not self._authenticated():
            self._error(HTTPStatus.UNAUTHORIZED, "Open the Library from Context Atomizer Local.")
            return
        try:
            if parsed.path == "/export":
                body = export_captured_library(self.server.database_path)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=atomizer-library-export.json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/":
                status = parameters.get("status", [None])[0]
                self._html(render_home(list_projects(self.server.database_path), status=status))
                return
            if parsed.path == "/project":
                project_id = self._single(parameters, "project_id")
                status = parameters.get("status", [None])[0]
                project = read_project_overview(self.server.database_path, project_id)
                project["automatic_maintenance"] = self.server.automatic_maintenance_enabled
                cycle = (
                    self.server.source_maintainer.last_cycle
                    if self.server.source_maintainer is not None
                    else None
                )
                source_errors = {
                    error.source_id: error.error_class
                    for error in (() if cycle is None else cycle.errors)
                }
                for source in project["sources"]:
                    source["error"] = source_errors.get(str(source["source_id"]))
                self._html(render_project(project, self.server.csrf_token, status=status))
                return
            if parsed.path == "/chat":
                chat_id = self._single(parameters, "chat_id")
                self._html(render_chat(read_chat_view(self.server.database_path, chat_id)))
                return
            if parsed.path == "/document":
                document_id = self._single(parameters, "document_id")
                self._html(render_document(read_document_view(self.server.database_path, document_id)))
                return
            if parsed.path == "/search":
                query = self._single(parameters, "q")
                project_id = parameters.get("project_id", [None])[0] or None
                project = (
                    read_project_overview(self.server.database_path, project_id)
                    if project_id
                    else None
                )
                try:
                    results = search_library(
                        self.server.database_path, query, project_id=project_id
                    )
                    self._html(render_search(query, results, project=project))
                except ValueError as error:
                    self._html(render_search(query, [], project=project, error=str(error)))
                return
            if parsed.path == "/status":
                self._html(render_runtime_status(self.server.health_snapshot()))
                return
            if parsed.path == "/permissions":
                status = parameters.get("status", [None])[0]
                self._html(
                    render_permissions(
                        self.server.permission_store.snapshot(),
                        self.server.health_snapshot()["extension"],
                        self.server.csrf_token,
                        access=self.server.health_snapshot()["library_access"],
                        status=status,
                    )
                )
                return
            self._error(HTTPStatus.NOT_FOUND, "The requested local Library page does not exist.")
        except KeyError:
            self._error(HTTPStatus.NOT_FOUND, "The requested local Library source does not exist.")
        except (OSError, ValueError) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if not self._local_client() or not self._expected_host():
            self._error(HTTPStatus.FORBIDDEN, "The Library request was rejected.")
            return
        if not self._authenticated() or not self._browser_post_allowed():
            self._error(HTTPStatus.FORBIDDEN, "The Library request was rejected.")
            return
        parsed = urlsplit(self.path)
        try:
            form = self._form()
            self._require_csrf(form)
            if parsed.path == "/extension/pairing-code":
                if self.server.pairing_code_provider is None:
                    raise RuntimeError("extension pairing is unavailable")
                code = self.server.pairing_code_provider()
                self._html(
                    render_permissions(
                        self.server.permission_store.snapshot(),
                        self.server.health_snapshot()["extension"],
                        self.server.csrf_token,
                        access=self.server.health_snapshot()["library_access"],
                        pairing_code=code,
                        status="A new one-time pairing code was created.",
                    )
                )
                return
            if parsed.path == "/extension/revoke":
                if self.server.pairing_revoke_callback is None:
                    raise RuntimeError("extension pairing is unavailable")
                self.server.pairing_revoke_callback()
                self._redirect(
                    "/permissions",
                    status="Browser extension pairing was revoked.",
                )
                return
            if parsed.path == "/managed/pairing-code":
                if self.server.managed_pairing_code_provider is None:
                    raise RuntimeError("managed connector pairing is unavailable")
                code = self.server.managed_pairing_code_provider()
                self._html(
                    render_permissions(
                        self.server.permission_store.snapshot(),
                        self.server.health_snapshot()["extension"],
                        self.server.csrf_token,
                        access=self.server.health_snapshot()["library_access"],
                        managed_pairing_code=code,
                        status="A new one-time managed connector pairing code was created.",
                    )
                )
                return
            if parsed.path == "/managed/revoke":
                if self.server.managed_pairing_revoke_callback is None:
                    raise RuntimeError("managed connector pairing is unavailable")
                self.server.managed_pairing_revoke_callback()
                self._redirect(
                    "/permissions",
                    status="Trusted manager pairing was revoked.",
                )
                return
            if parsed.path == "/integration/set":
                integration = self._single(form, "integration")
                enabled_text = self._single(form, "enabled")
                if enabled_text not in {"yes", "no"}:
                    raise ValueError("enabled must be yes or no")
                self.server.permission_store.set_enabled(
                    integration, enabled_text == "yes"
                )
                label = {
                    "chatgpt_web": "ChatGPT Web",
                    "codex": "Codex",
                    "claude_code": "Claude Code",
                }.get(integration, integration)
                state = "enabled" if enabled_text == "yes" else "disabled"
                self._redirect(
                    "/permissions",
                    status=f"{label} is {state}. Existing Library history was preserved.",
                )
                return
            if parsed.path == "/library-access/set":
                selected = self._single(form, "mode")
                mode = self.server.access_mode_setter(selected)
                self._redirect(
                    "/permissions",
                    status=(
                        f"Library access mode is {mode.value}. "
                        "Changing mode does not grant managed authority."
                    ),
                )
                return
            project_id = self._single(form, "project_id")
            project_path = "/project?" + urlencode({"project_id": project_id})
            if parsed.path in {"/source/authorize", "/source/elect"}:
                source_path = Path(self._single(form, "source_path"))
                source_kind = self._single(form, "source_kind")
                with self.server.source_operation_lock:
                    if source_kind == "FILE":
                        result = authorize_file_source(
                            self.server.database_path, project_id, source_path
                        )
                    elif source_kind == "DIRECTORY":
                        result = authorize_directory(
                            self.server.database_path, project_id, source_path
                        )
                    else:
                        raise ValueError("source_kind must be FILE or DIRECTORY")
                self._redirect(
                    project_path,
                    status=(
                        f"Source authorized and reconciled: {result.scanned} scanned, "
                        f"{result.added} added, {result.updated} updated."
                    ),
                )
                return
            if parsed.path in {"/source/rescan", "/source/sync"}:
                source_id = self._single(form, "source_id")
                with self.server.source_operation_lock:
                    result = sync_elected_source(self.server.database_path, source_id)
                self._redirect(
                    project_path,
                    status=(
                        f"Source reconciled: {result.scanned} scanned, {result.added} added, "
                        f"{result.updated} updated, {result.removed} removed."
                    ),
                )
                return
            if parsed.path == "/source/revoke":
                if self._single(form, "confirm") != "yes":
                    raise ValueError("source authorization revocation must be confirmed")
                source_id = self._single(form, "source_id")
                with self.server.source_operation_lock:
                    if not revoke_source_authorization(
                        self.server.database_path, source_id
                    ):
                        raise KeyError(source_id)
                self._redirect(
                    project_path,
                    status="Source removed. Physical files were not deleted.",
                )
                return
            self._error(HTTPStatus.NOT_FOUND, "The requested local Library action does not exist.")
        except PermissionError:
            self._error(HTTPStatus.FORBIDDEN, "The local form token was rejected.")
        except KeyError:
            self._error(HTTPStatus.NOT_FOUND, "The requested local Library source does not exist.")
        except (OSError, UnicodeError, ValueError) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))


def main() -> int:
    raise SystemExit(
        "the Library requires runtime-owned session and management state; "
        "open it through Context Atomizer Local"
    )


if __name__ == "__main__":
    raise SystemExit(main())
