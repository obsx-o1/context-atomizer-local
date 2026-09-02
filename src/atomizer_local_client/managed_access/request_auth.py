"""Domain-separated HMAC authentication for the paired manager channel."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass

from atomizer_local_client.local_auth.replay import ReplayWindow


MANAGED_PROTOCOL_VERSION = "1"
MANAGED_PAIRING_DOMAIN = "context-atomizer-local/managed-connector-pairing/v1"
MANAGED_REQUEST_DOMAIN = "context-atomizer-local/managed-request/v1"

_NONCE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def managed_request_material(
    *,
    method: str,
    operation: str,
    nonce: str,
    timestamp: str,
    body_sha256: str,
    protocol_version: str = MANAGED_PROTOCOL_VERSION,
) -> bytes:
    return (
        f"{MANAGED_REQUEST_DOMAIN}\n{protocol_version}\n{method.upper()}\n"
        f"{operation}\n{nonce}\n{timestamp}\n{body_sha256}"
    ).encode("ascii")


def managed_request_signature(
    secret: str,
    *,
    method: str,
    operation: str,
    nonce: str,
    timestamp: str,
    body_sha256: str,
) -> str:
    return hmac.new(
        secret.encode("ascii"),
        managed_request_material(
            method=method,
            operation=operation,
            nonce=nonce,
            timestamp=timestamp,
            body_sha256=body_sha256,
        ),
        hashlib.sha256,
    ).hexdigest()


def managed_request_headers(
    secret: str, *, method: str, operation: str, body: bytes
) -> dict[str, str]:
    nonce = secrets.token_urlsafe(32)
    timestamp = str(int(time.time()))
    body_sha256 = hashlib.sha256(body).hexdigest()
    return {
        "X-Atomizer-Managed-Protocol": MANAGED_PROTOCOL_VERSION,
        "X-Atomizer-Managed-Nonce": nonce,
        "X-Atomizer-Managed-Timestamp": timestamp,
        "X-Atomizer-Managed-Content-SHA256": body_sha256,
        "X-Atomizer-Managed-Signature": managed_request_signature(
            secret,
            method=method,
            operation=operation,
            nonce=nonce,
            timestamp=timestamp,
            body_sha256=body_sha256,
        ),
    }


@dataclass(frozen=True, slots=True)
class ManagedRequestAuthentication:
    protocol_version: str
    nonce: str
    timestamp: str
    body_sha256: str
    signature: str


class ManagedRequestVerifier:
    def __init__(self, replay: ReplayWindow | None = None) -> None:
        self.replay = replay or ReplayWindow()

    def verify(
        self,
        secret: str,
        *,
        method: str,
        operation: str,
        body: bytes,
        authentication: ManagedRequestAuthentication,
    ) -> bool:
        if authentication.protocol_version != MANAGED_PROTOCOL_VERSION:
            return False
        if not _NONCE.fullmatch(authentication.nonce):
            return False
        if not _SHA256.fullmatch(authentication.body_sha256):
            return False
        if not _SHA256.fullmatch(authentication.signature):
            return False
        actual = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(actual, authentication.body_sha256):
            return False
        try:
            timestamp = int(authentication.timestamp)
        except ValueError:
            return False
        expected = managed_request_signature(
            secret,
            method=method,
            operation=operation,
            nonce=authentication.nonce,
            timestamp=authentication.timestamp,
            body_sha256=authentication.body_sha256,
        )
        if not hmac.compare_digest(authentication.signature, expected):
            return False
        return self.replay.accept(authentication.nonce, timestamp)


__all__ = [
    "MANAGED_PAIRING_DOMAIN",
    "MANAGED_PROTOCOL_VERSION",
    "ManagedRequestAuthentication",
    "ManagedRequestVerifier",
    "managed_request_headers",
    "managed_request_material",
    "managed_request_signature",
]
