"""HMAC proof and authenticated local capture-request verification."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from atomizer_local_client.local_auth.contracts import (
    BRIDGE_PORT,
    PROTOCOL_VERSION,
    capture_request_material,
    runtime_proof_material,
    sign_hex,
)
from atomizer_local_client.local_auth.replay import ReplayWindow


_NONCE = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class CaptureAuthentication:
    protocol_version: str
    nonce: str
    timestamp: str
    body_sha256: str
    signature: str


def runtime_proof(secret: str, challenge_nonce: str) -> str:
    if not _NONCE.fullmatch(challenge_nonce):
        raise ValueError("invalid challenge nonce")
    return sign_hex(
        secret,
        runtime_proof_material(
            challenge_nonce,
            protocol_version=PROTOCOL_VERSION,
            port=BRIDGE_PORT,
        ),
    )


class CaptureRequestVerifier:
    def __init__(self, replay: ReplayWindow | None = None) -> None:
        self.replay = replay or ReplayWindow()

    def verify(
        self,
        secret: str,
        *,
        method: str,
        operation: str,
        body: bytes,
        authentication: CaptureAuthentication,
    ) -> bool:
        if authentication.protocol_version != PROTOCOL_VERSION:
            return False
        if not _NONCE.fullmatch(authentication.nonce):
            return False
        if not _SHA256.fullmatch(authentication.body_sha256):
            return False
        if not _SHA256.fullmatch(authentication.signature):
            return False
        actual_body_sha256 = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(actual_body_sha256, authentication.body_sha256):
            return False
        try:
            timestamp_value = int(authentication.timestamp)
        except ValueError:
            return False
        expected = sign_hex(
            secret,
            capture_request_material(
                method=method,
                operation=operation,
                nonce=authentication.nonce,
                timestamp=authentication.timestamp,
                body_sha256=authentication.body_sha256,
                protocol_version=authentication.protocol_version,
            ),
        )
        if not hmac.compare_digest(authentication.signature, expected):
            return False
        return self.replay.accept(authentication.nonce, timestamp_value)
