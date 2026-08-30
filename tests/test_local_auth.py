from __future__ import annotations

import hashlib
import logging
import unittest

# Anchor package imports to this checkout before loading authentication modules.
from test_support import SOURCE_ROOT  # noqa: F401

from atomizer_local_client.local_auth.contracts import (
    capture_request_material,
    runtime_proof_material,
    sign_hex,
)
from atomizer_local_client.local_auth.library_session import LibrarySessionAuthority
from atomizer_local_client.local_auth.pairing import (
    ExtensionPairingAuthority,
    PairingRateLimited,
)
from atomizer_local_client.local_auth.replay import ReplayWindow
from atomizer_local_client.local_auth.request_auth import (
    CaptureAuthentication,
    CaptureRequestVerifier,
    runtime_proof,
)


class MemorySecretStore:
    def __init__(self) -> None:
        self.value: str | None = None
        self.rotation = 0

    def load(self) -> str:
        if self.value is None:
            raise FileNotFoundError
        return self.value

    def rotate(self) -> str:
        self.rotation += 1
        self.value = f"extension-secret-{self.rotation:02d}-0123456789-abcdefghijklmnop"
        return self.value

    def remove(self) -> None:
        self.value = None


class PairingTests(unittest.TestCase):
    def test_code_is_high_entropy_single_use_expiring_and_secret_is_stable(self) -> None:
        now = [1000.0]
        store = MemorySecretStore()
        authority = ExtensionPairingAuthority(store, clock=lambda: now[0])
        code = authority.issue_code()
        self.assertGreaterEqual(len(code), 43)
        secret = authority.pair(code)
        self.assertEqual(authority.secret(), secret)
        self.assertEqual(authority.secret(), secret)
        self.assertEqual(store.rotation, 1)
        with self.assertRaises(PermissionError):
            authority.pair(code)

        expired = authority.issue_code()
        now[0] += authority.code_ttl_seconds + 1
        with self.assertRaises(PermissionError):
            authority.pair(expired)
        self.assertEqual(authority.secret(), secret)

    def test_wrong_codes_are_rate_limited_and_revocation_requires_repair(self) -> None:
        now = [1000.0]
        store = MemorySecretStore()
        authority = ExtensionPairingAuthority(store, clock=lambda: now[0])
        authority.issue_code()
        for _ in range(authority.max_attempts):
            with self.assertRaises(PermissionError):
                authority.pair("wrong-code-with-sufficient-length")
        with self.assertRaises(PairingRateLimited):
            authority.pair("wrong-code-with-sufficient-length")
        authority.revoke()
        self.assertFalse(authority.paired)

    def test_secret_never_enters_pairing_logs(self) -> None:
        store = MemorySecretStore()
        authority = ExtensionPairingAuthority(store)
        secret = authority.pair(authority.issue_code())
        records: list[str] = []

        class Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        logger = logging.getLogger("atomizer_local_pairing_test")
        handler = Handler()
        logger.addHandler(handler)
        try:
            logger.info("pairing completed")
        finally:
            logger.removeHandler(handler)
        self.assertNotIn(secret, "\n".join(records))


class RequestAuthenticationTests(unittest.TestCase):
    secret = "extension-secret-01-0123456789-abcdefghijklmnop"

    def authentication(
        self,
        *,
        body: bytes,
        operation: str = "/v1/chat-events",
        nonce: str = "nonce_0123456789_abcdefghijklmnop",
        timestamp: str = "1000",
        secret: str | None = None,
    ) -> CaptureAuthentication:
        body_sha256 = hashlib.sha256(body).hexdigest()
        signature = sign_hex(
            secret or self.secret,
            capture_request_material(
                method="POST",
                operation=operation,
                nonce=nonce,
                timestamp=timestamp,
                body_sha256=body_sha256,
            ),
        )
        return CaptureAuthentication("1", nonce, timestamp, body_sha256, signature)

    def test_runtime_proof_binds_nonce_protocol_and_fixed_port(self) -> None:
        nonce = "challenge_0123456789_abcdefghijklmnop"
        proof = runtime_proof(self.secret, nonce)
        self.assertEqual(proof, sign_hex(self.secret, runtime_proof_material(nonce)))
        self.assertNotEqual(
            proof,
            sign_hex(self.secret, runtime_proof_material(nonce + "x")),
        )
        self.assertNotEqual(
            proof,
            sign_hex(self.secret, runtime_proof_material(nonce, protocol_version="2")),
        )
        self.assertNotEqual(
            proof,
            sign_hex(self.secret, runtime_proof_material(nonce, port=43118)),
        )

    def test_capture_auth_binds_body_operation_and_rejects_replay(self) -> None:
        clock = [1000.0]
        verifier = CaptureRequestVerifier(ReplayWindow(clock=lambda: clock[0]))
        body = b'{"content":"sensitive"}'
        authentication = self.authentication(body=body)
        self.assertTrue(
            verifier.verify(
                self.secret,
                method="POST",
                operation="/v1/chat-events",
                body=body,
                authentication=authentication,
            )
        )
        self.assertFalse(
            verifier.verify(
                self.secret,
                method="POST",
                operation="/v1/chat-events",
                body=body,
                authentication=authentication,
            )
        )
        altered = self.authentication(
            body=body,
            nonce="nonce_0123456789_abcdefghijklmnop2",
        )
        self.assertFalse(
            verifier.verify(
                self.secret,
                method="POST",
                operation="/v1/chat-titles",
                body=body,
                authentication=altered,
            )
        )
        modified = self.authentication(
            body=body,
            nonce="nonce_0123456789_abcdefghijklmnop3",
        )
        self.assertFalse(
            verifier.verify(
                self.secret,
                method="POST",
                operation="/v1/chat-events",
                body=body + b"x",
                authentication=modified,
            )
        )

    def test_freshness_future_skew_and_bounded_expiry_are_deterministic(self) -> None:
        clock = [1000.0]
        replay = ReplayWindow(
            freshness_seconds=10,
            future_skew_seconds=2,
            max_entries=2,
            clock=lambda: clock[0],
        )
        verifier = CaptureRequestVerifier(replay)
        body = b"{}"
        for nonce, timestamp, accepted in (
            ("a" * 32, "989", False),
            ("b" * 32, "1003", False),
            ("c" * 32, "1000", True),
            ("d" * 32, "1000", True),
            ("e" * 32, "1000", True),
        ):
            self.assertEqual(
                verifier.verify(
                    self.secret,
                    method="POST",
                    operation="/v1/chat-events",
                    body=body,
                    authentication=self.authentication(
                        body=body, nonce=nonce, timestamp=timestamp
                    ),
                ),
                accepted,
            )
        self.assertEqual(replay.size, 2)
        clock[0] = 1011.0
        self.assertEqual(replay.size, 0)
        self.assertTrue(
            verifier.verify(
                self.secret,
                method="POST",
                operation="/v1/chat-events",
                body=body,
                authentication=self.authentication(
                    body=body, nonce="f" * 32, timestamp="1011"
                ),
            )
        )


class LibrarySessionTests(unittest.TestCase):
    def test_sessions_cannot_be_forged_and_expire(self) -> None:
        now = [1000.0]
        authority = LibrarySessionAuthority(
            launch_ttl_seconds=10,
            session_ttl_seconds=20,
            clock=lambda: now[0],
        )
        launch = authority.issue_launch()
        session = authority.consume_launch(launch)
        self.assertIsNotNone(session)
        self.assertIsNone(authority.consume_launch(launch))
        self.assertTrue(authority.authenticated(str(session)))
        self.assertFalse(authority.authenticated("forged"))
        now[0] = 1021.0
        self.assertFalse(authority.authenticated(str(session)))


if __name__ == "__main__":
    unittest.main()
