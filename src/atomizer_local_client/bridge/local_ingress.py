"""Authenticated, size-limited loopback-only ChatEvent ingress."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import socket
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.chat.normalizer import normalize_host_event
from atomizer_local_client.chats.title_reconciliation import reconcile_existing_chat_titles
from atomizer_local_client.diagnostics import record_capture_error
from atomizer_local_client.local_auth.contracts import (
    BRIDGE_PORT,
    PAIRING_DOMAIN,
    PROTOCOL_VERSION,
)
from atomizer_local_client.local_auth.pairing import (
    ExtensionPairingAuthority,
    PairingRateLimited,
)
from atomizer_local_client.local_auth.request_auth import (
    CaptureAuthentication,
    CaptureRequestVerifier,
    runtime_proof,
)
from atomizer_local_client.runtime.permissions import PermissionStore
from atomizer_local_client.runtime_health import RuntimeIdentity, database_health


_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_CONTROL_BYTES = 4096
_CAPTURE_OPERATIONS = frozenset({"/v1/chat-events", "/v1/chat-titles"})


class LocalIngressServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(
        self,
        database_path: Path,
        management_token: str,
        pairing_authority: ExtensionPairingAuthority,
        port: int = BRIDGE_PORT,
        *,
        runtime_identity: RuntimeIdentity | None = None,
        request_verifier: CaptureRequestVerifier | None = None,
        runtime_stop_callback: Callable[[], None] | None = None,
        library_launch_provider: Callable[[], str] | None = None,
        management_status_provider: Callable[[], dict[str, object]] | None = None,
        extension_seen_callback: Callable[[str], None] | None = None,
        integration_enabled: Callable[[str], bool] | None = None,
        _test_port: int | None = None,
    ) -> None:
        if len(management_token) < 32:
            raise ValueError("management token must contain at least 32 characters")
        if _test_port is None and int(port) != BRIDGE_PORT:
            raise ValueError("the capture bridge must bind the fixed port 43117")
        self.database_path = Path(database_path)
        self.management_token = management_token
        self.pairing_authority = pairing_authority
        self.runtime_identity = runtime_identity or RuntimeIdentity()
        self.request_verifier = request_verifier or CaptureRequestVerifier()
        self.runtime_stop_callback = runtime_stop_callback
        self.library_launch_provider = library_launch_provider
        self.management_status_provider = management_status_provider
        self.extension_seen_callback = extension_seen_callback
        self.integration_enabled = integration_enabled or (lambda integration: True)
        bind_port = BRIDGE_PORT if _test_port is None else int(_test_port)
        super().__init__(("127.0.0.1", bind_port), LocalIngressHandler)


class LocalIngressHandler(BaseHTTPRequestHandler):
    server: LocalIngressServer

    def log_message(self, format: str, *args: object) -> None:
        del format, args
        logging.getLogger("atomizer_local_ingress").info("request status recorded")

    def _json_response(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        runtime = self.server.runtime_identity.snapshot()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Atomizer-Protocol-Version", str(runtime["protocol_version"]))
        self.send_header("X-Atomizer-Runtime-Build", str(runtime["startup_build_sha256"]))
        self.send_header("X-Atomizer-Restart-Required", str(runtime["restart_required"]).lower())
        self.end_headers()
        self.wfile.write(encoded)

    def _read_body(self, maximum: int) -> bytes:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid request size") from error
        if not 0 < content_length <= maximum:
            raise ValueError("invalid request size")
        return self.rfile.read(content_length)

    @staticmethod
    def _object(body: bytes) -> dict[str, object]:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request must be an object")
        return payload

    def _management_authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.management_token}"
        return hmac.compare_digest(supplied, expected)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path != "/v1/management/status":
            self._json_response(HTTPStatus.NOT_FOUND, {"ok": False})
            return
        if not self._management_authorized():
            self._json_response(HTTPStatus.UNAUTHORIZED, {"ok": False})
            return
        provider = self.server.management_status_provider
        if provider is None:
            self._json_response(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False})
            return
        self._json_response(HTTPStatus.OK, provider())

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path == "/v1/bootstrap":
            self._json_response(HTTPStatus.NOT_FOUND, {"ok": False})
            return
        if self.path == "/v1/pair":
            self._pair()
            return
        if self.path == "/v1/runtime-proof":
            self._prove_runtime()
            return
        if self.path in _CAPTURE_OPERATIONS:
            self._capture()
            return
        if self.path in {"/v1/runtime/stop", "/v1/library/launch"}:
            self._management_action()
            return
        self._json_response(HTTPStatus.NOT_FOUND, {"ok": False})

    def _pair(self) -> None:
        try:
            payload = self._object(self._read_body(_MAX_CONTROL_BYTES))
            if (
                payload.get("protocolVersion") != PROTOCOL_VERSION
                or payload.get("pairingDomain") != PAIRING_DOMAIN
            ):
                raise PermissionError("unsupported protocol")
            code = payload.get("pairingCode")
            if not isinstance(code, str):
                raise PermissionError("pairing code was rejected")
            secret = self.server.pairing_authority.pair(code)
        except PairingRateLimited:
            self._json_response(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False})
            return
        except (PermissionError, ValueError, UnicodeError, json.JSONDecodeError):
            self._json_response(HTTPStatus.FORBIDDEN, {"ok": False})
            return
        self._json_response(
            HTTPStatus.OK,
            {
                "ok": True,
                "protocolVersion": PROTOCOL_VERSION,
                "pairingDomain": PAIRING_DOMAIN,
                "extensionSecret": secret,
            },
        )

    def _prove_runtime(self) -> None:
        secret = self.server.pairing_authority.secret()
        if secret is None:
            self._json_response(HTTPStatus.UNAUTHORIZED, {"ok": False})
            return
        try:
            payload = self._object(self._read_body(_MAX_CONTROL_BYTES))
            if payload.get("protocolVersion") != PROTOCOL_VERSION:
                raise ValueError("unsupported protocol")
            nonce = payload.get("challengeNonce")
            if not isinstance(nonce, str):
                raise ValueError("invalid challenge")
            proof = runtime_proof(secret, nonce)
        except (ValueError, UnicodeError, json.JSONDecodeError):
            self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False})
            return
        self._json_response(
            HTTPStatus.OK,
            {
                "ok": True,
                "protocolVersion": PROTOCOL_VERSION,
                "port": BRIDGE_PORT,
                "challengeNonce": nonce,
                "proof": proof,
            },
        )

    def _capture(self) -> None:
        secret = self.server.pairing_authority.secret()
        if secret is None:
            self._json_response(HTTPStatus.UNAUTHORIZED, {"ok": False})
            return
        try:
            raw = self._read_body(_MAX_REQUEST_BYTES)
            authentication = CaptureAuthentication(
                protocol_version=self.headers.get("X-Atomizer-Protocol", ""),
                nonce=self.headers.get("X-Atomizer-Nonce", ""),
                timestamp=self.headers.get("X-Atomizer-Timestamp", ""),
                body_sha256=self.headers.get("X-Atomizer-Content-SHA256", ""),
                signature=self.headers.get("X-Atomizer-Signature", ""),
            )
            if not self.server.request_verifier.verify(
                secret,
                method="POST",
                operation=self.path,
                body=raw,
                authentication=authentication,
            ):
                self._json_response(HTTPStatus.UNAUTHORIZED, {"ok": False})
                return
            payload = self._object(raw)
            if self.server.extension_seen_callback is not None:
                self.server.extension_seen_callback(PROTOCOL_VERSION)
            if not self.server.integration_enabled("chatgpt_web"):
                self._json_response(
                    HTTPStatus.OK,
                    {"ok": True, "captured": False, "disabled": True},
                )
                return
            if self.path == "/v1/chat-events":
                event = normalize_host_event(payload)
                if event is None:
                    raise ValueError("event has no visible message")
                receipt = ingest_chat_event(self.server.database_path, event)
                response = {
                    "ok": True,
                    "inserted": receipt.inserted,
                    "message_id": receipt.message_id,
                }
            else:
                observations = payload.get("observations")
                if not isinstance(observations, list):
                    raise ValueError("observations must be an array")
                result = reconcile_existing_chat_titles(
                    self.server.database_path, observations
                )
                response = {
                    "ok": True,
                    "observed": result.observed,
                    "matched": result.matched,
                    "updated": result.updated,
                    "unchanged": result.unchanged,
                    "rejected": result.rejected,
                }
        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            record_capture_error(
                self.server.database_path.parent, "bridge_event_rejected", error
            )
            self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False})
            return
        except BaseException as error:
            record_capture_error(
                self.server.database_path.parent, "bridge_capture_failed", error
            )
            self._json_response(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False})
            return
        self._json_response(HTTPStatus.OK, response)

    def _management_action(self) -> None:
        if not self._management_authorized():
            self._json_response(HTTPStatus.UNAUTHORIZED, {"ok": False})
            return
        try:
            self._object(self._read_body(_MAX_CONTROL_BYTES))
        except (ValueError, UnicodeError, json.JSONDecodeError):
            self._json_response(HTTPStatus.BAD_REQUEST, {"ok": False})
            return
        if self.path == "/v1/runtime/stop":
            if self.server.runtime_stop_callback is None:
                self._json_response(HTTPStatus.NOT_FOUND, {"ok": False})
                return
            self._json_response(HTTPStatus.ACCEPTED, {"ok": True, "stopping": True})
            threading.Thread(
                target=self.server.runtime_stop_callback,
                name="atomizer-runtime-stop-request",
                daemon=True,
            ).start()
            return
        provider = self.server.library_launch_provider
        if provider is None:
            self._json_response(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False})
            return
        self._json_response(HTTPStatus.OK, {"ok": True, "url": provider()})


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local ChatGPT-web ingestion bridge")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--management-token-env", default="ATOMIZER_LOCAL_MANAGEMENT_TOKEN")
    parser.add_argument("--port", default=BRIDGE_PORT, type=int)
    parser.add_argument("--permissions", type=Path)
    arguments = parser.parse_args()
    token = os.environ.get(arguments.management_token_env)
    if token is None:
        raise SystemExit(
            "required local management token environment variable is unset: "
            + arguments.management_token_env
        )
    del token
    raise SystemExit(
        "the standalone bridge entrypoint requires runtime-owned pairing state; "
        "start atomizer-local-runtime instead"
    )


if __name__ == "__main__":
    raise SystemExit(main())
