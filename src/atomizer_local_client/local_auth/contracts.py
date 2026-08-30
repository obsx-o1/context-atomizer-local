"""Versioned, domain-separated local authentication message shapes."""

from __future__ import annotations

import hashlib
import hmac


PROTOCOL_VERSION = "1"
BRIDGE_PORT = 43117
RUNTIME_PROOF_DOMAIN = "context-atomizer-local/runtime-proof/v1"
CAPTURE_REQUEST_DOMAIN = "context-atomizer-local/capture-request/v1"
PAIRING_DOMAIN = "context-atomizer-local/pairing/v1"


def runtime_proof_material(
    challenge_nonce: str,
    *,
    protocol_version: str = PROTOCOL_VERSION,
    port: int = BRIDGE_PORT,
) -> bytes:
    return (
        f"{RUNTIME_PROOF_DOMAIN}\n{protocol_version}\n{challenge_nonce}\n{int(port)}"
    ).encode("ascii")


def capture_request_material(
    *,
    method: str,
    operation: str,
    nonce: str,
    timestamp: str,
    body_sha256: str,
    protocol_version: str = PROTOCOL_VERSION,
) -> bytes:
    return (
        f"{CAPTURE_REQUEST_DOMAIN}\n{protocol_version}\n{method.upper()}\n"
        f"{operation}\n{nonce}\n{timestamp}\n{body_sha256}"
    ).encode("ascii")


def sign_hex(secret: str, material: bytes) -> str:
    return hmac.new(secret.encode("ascii"), material, hashlib.sha256).hexdigest()
