"""Behavioral credential and authority checks against the packaged runtime.

The helper imports the production path and authentication contracts from the
validation checkout. The installer harness separately binds that source closure
to the installed executable fingerprint before this helper is invoked. Secret
values remain process-local and are never included in stdout or receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import http.cookiejar
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


CHECKOUT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHECKOUT_ROOT / "src"))

from atomizer_local_client.local_auth.contracts import (  # noqa: E402
    PAIRING_DOMAIN,
    PROTOCOL_VERSION,
    capture_request_material,
    sign_hex,
)
from atomizer_local_client.local_auth.request_auth import runtime_proof  # noqa: E402
from atomizer_local_client.runtime.configuration import RuntimePaths  # noqa: E402
from atomizer_local_client.runtime.credentials import CredentialStore  # noqa: E402


_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
_CSRF = re.compile(r"name=['\"]csrf_token['\"] value=['\"]([^'\"]+)['\"]")
_PAIRING_CODE = re.compile(r"<p><code>([^<]+)</code></p>")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    operation = urllib.request.Request(
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers=headers or {},
    )
    try:
        response = (
            opener.open(operation, timeout=5)
            if opener is not None
            else urllib.request.urlopen(operation, timeout=5)
        )
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read(), dict(error.headers)
    with response:
        return response.status, response.read(), dict(response.headers)


def json_request(
    url: str,
    *,
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    effective_headers = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        effective_headers["Content-Type"] = "application/json"
    status, raw, _ = request(url, data=body, headers=effective_headers)
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("packaged endpoint did not return valid JSON") from error
    require(isinstance(decoded, dict), "packaged endpoint returned a non-object response")
    return status, decoded


def signed_capture(
    bridge: str,
    secret: str,
    *,
    event_id: str,
) -> tuple[int, dict[str, Any]]:
    payload = {
        "event_id": event_id,
        "host": "chatgpt_web",
        "host_project_reference": "g-p-installer-security",
        "host_chat_reference": "installer-security-chat",
        "host_turn_reference": event_id,
        "role": "user",
        "content": "disposable packaged credential acceptance evidence",
        "captured_at": "2026-08-20T12:00:00+00:00",
        "project_display_name": "Installer Security Acceptance",
        "chat_display_name": "Packaged authority separation",
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    nonce = secrets.token_urlsafe(24)
    timestamp = str(int(time.time()))
    body_sha256 = hashlib.sha256(body).hexdigest()
    signature = sign_hex(
        secret,
        capture_request_material(
            method="POST",
            operation="/v1/chat-events",
            nonce=nonce,
            timestamp=timestamp,
            body_sha256=body_sha256,
        ),
    )
    status, raw, _ = request(
        bridge + "/v1/chat-events",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Atomizer-Protocol": PROTOCOL_VERSION,
            "X-Atomizer-Nonce": nonce,
            "X-Atomizer-Timestamp": timestamp,
            "X-Atomizer-Content-SHA256": body_sha256,
            "X-Atomizer-Signature": signature,
        },
    )
    return status, json.loads(raw.decode("utf-8"))


def management_status(bridge: str, management: str) -> dict[str, Any]:
    status, payload = json_request(
        bridge + "/v1/management/status",
        headers={"Authorization": f"Bearer {management}"},
    )
    require(status == 200, "management credential did not authorize packaged status")
    return payload


def library_session(
    bridge: str, library: str, management: str
) -> tuple[urllib.request.OpenerDirector, str]:
    status, launch = json_request(
        bridge + "/v1/library/launch",
        payload={},
        headers={"Authorization": f"Bearer {management}"},
    )
    require(status == 200, "management credential could not issue a Library launch")
    launch_url = launch.get("url")
    require(
        isinstance(launch_url, str) and launch_url.startswith(library + "/?launch="),
        "Library launch response was not bound to the packaged Library endpoint",
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    established, _, _ = request(launch_url, opener=opener)
    require(established == 200, "one-time Library launch did not establish a session")
    replayed, _, _ = request(launch_url)
    require(replayed == 403, "Library launch capability was reusable")
    permissions, body, _ = request(library + "/permissions", opener=opener)
    require(permissions == 200, "Library session could not read the permissions page")
    match = _CSRF.search(body.decode("utf-8"))
    require(match is not None, "Library session did not expose its form token")
    return opener, html.unescape(match.group(1))


def form_request(
    opener: urllib.request.OpenerDirector,
    library: str,
    path: str,
    values: dict[str, str],
) -> tuple[int, str]:
    status, raw, _ = request(
        library + path,
        data=urllib.parse.urlencode(values).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": library,
            "Sec-Fetch-Site": "same-origin",
        },
        opener=opener,
    )
    return status, raw.decode("utf-8")


def pair_extension(
    bridge: str,
    library: str,
    opener: urllib.request.OpenerDirector,
    csrf: str,
) -> str:
    status, pairing_page = form_request(
        opener,
        library,
        "/extension/pairing-code",
        {"csrf_token": csrf},
    )
    require(status == 200, "Library could not issue a one-time pairing code")
    match = _PAIRING_CODE.search(pairing_page)
    require(match is not None, "Library did not render the one-time pairing code")
    code = html.unescape(match.group(1))
    status, paired = json_request(
        bridge + "/v1/pair",
        payload={
            "protocolVersion": PROTOCOL_VERSION,
            "pairingDomain": PAIRING_DOMAIN,
            "pairingCode": code,
        },
    )
    require(status == 200, "explicit extension pairing failed")
    extension = paired.get("extensionSecret")
    require(isinstance(extension, str) and _TOKEN.fullmatch(extension) is not None,
            "paired extension secret did not meet the production token contract")
    replay, _ = json_request(
        bridge + "/v1/pair",
        payload={
            "protocolVersion": PROTOCOL_VERSION,
            "pairingDomain": PAIRING_DOMAIN,
            "pairingCode": code,
        },
    )
    require(replay == 403, "one-time extension pairing code was reusable")
    return extension


def assert_runtime_proof(bridge: str, extension: str) -> None:
    challenge = secrets.token_urlsafe(24)
    status, proof = json_request(
        bridge + "/v1/runtime-proof",
        payload={"protocolVersion": PROTOCOL_VERSION, "challengeNonce": challenge},
    )
    require(status == 200, "paired extension could not request runtime proof")
    require(proof.get("proof") == runtime_proof(extension, challenge),
            "runtime proof did not authenticate the paired extension secret")


def assert_extension_cannot_manage(
    bridge: str, library: str, extension: str
) -> None:
    for path in ("/v1/runtime/stop", "/v1/library/launch"):
        status, _ = json_request(
            bridge + path,
            payload={},
            headers={"Authorization": f"Bearer {extension}"},
        )
        require(status == 401, "extension capture authority reached a management action")
    for path in ("/v1/management/rotate-credential", "/v1/lifecycle/uninstall"):
        status, _ = json_request(
            bridge + path,
            payload={},
            headers={"Authorization": f"Bearer {extension}"},
        )
        require(status == 404, "packaged bridge exposed an extension-accessible lifecycle action")
    for path in ("/integration/set", "/source/authorize"):
        status, _, _ = request(
            library + path,
            data=b"",
            headers={
                "Authorization": f"Bearer {extension}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": library,
                "Sec-Fetch-Site": "same-origin",
            },
        )
        require(status == 403, "extension capture authority reached a Library mutation")


def assert_no_secret_disclosure(paths: RuntimePaths, receipt: Path, secrets_: list[str]) -> None:
    candidates = [paths.config, paths.permissions, paths.state, paths.log, receipt]
    candidates.extend(paths.log.parent.glob("runtime.log.*") if paths.log.parent.exists() else ())
    for candidate in candidates:
        if not candidate.is_file() or candidate.stat().st_size > 4_000_000:
            continue
        content = candidate.read_bytes()
        for value in secrets_:
            require(value.encode("ascii") not in content,
                    "credential content appeared in runtime state, logs, or receipt")


def load_context(data_directory: Path) -> tuple[RuntimePaths, str, str, str]:
    paths = RuntimePaths.current_user()
    require(paths.app_data == data_directory.resolve(),
            "installer data directory diverged from the production runtime path contract")
    state = json.loads(paths.state.read_text(encoding="utf-8"))
    bridge = f"http://127.0.0.1:{int(state['bridge_port'])}"
    library = f"http://127.0.0.1:{int(state['library_port'])}"
    management = CredentialStore(paths.credential).load()
    require(_TOKEN.fullmatch(management) is not None and len(management) >= 64,
            "management credential did not meet the production entropy contract")
    require(management.encode("ascii") not in paths.credential.read_bytes(),
            "management credential was stored in plaintext")
    return paths, bridge, library, management


def fresh(data_directory: Path, receipt: Path) -> dict[str, bool]:
    paths, bridge, library, management = load_context(data_directory)
    require(paths.credential.name == "management-credential.bin",
            "production management credential path contract changed")
    require(paths.extension_credential.name == "extension-pairing.bin",
            "production extension credential path contract changed")
    require(not (paths.app_data / "bridge-credential.bin").exists(),
            "obsolete all-purpose bridge credential was recreated")
    require(not paths.extension_credential.exists(),
            "fresh install created a pre-paired extension secret")
    persisted = paths.config.read_text(encoding="utf-8") + paths.state.read_text(encoding="utf-8")
    require(all(term not in persisted for term in ("pairingCode", "launchToken", "librarySession")),
            "ephemeral pairing or Library authority was persisted")

    unauthenticated, _, _ = request(library + "/")
    require(unauthenticated == 401, "Library content was available without a session")
    bootstrap, bootstrap_body = json_request(bridge + "/v1/bootstrap", payload={})
    require(bootstrap == 404 and bootstrap_body == {"ok": False},
            "obsolete unauthenticated bootstrap contract was reachable")
    unpaired_proof, _ = json_request(
        bridge + "/v1/runtime-proof",
        payload={"protocolVersion": PROTOCOL_VERSION, "challengeNonce": secrets.token_urlsafe(24)},
    )
    require(unpaired_proof == 401, "capture authority existed before extension pairing")
    denied_pair, _ = json_request(
        bridge + "/v1/pair",
        payload={
            "protocolVersion": PROTOCOL_VERSION,
            "pairingDomain": PAIRING_DOMAIN,
            "pairingCode": secrets.token_urlsafe(32),
        },
    )
    require(denied_pair == 403, "pairing succeeded without a Library-issued code")
    status = management_status(bridge, management)
    require(status["extension"]["paired"] is False, "fresh packaged runtime reported pre-paired state")

    opener, csrf = library_session(bridge, library, management)
    extension = pair_extension(bridge, library, opener, csrf)
    require(paths.extension_credential.is_file(), "pairing did not persist the extension secret")
    stored_extension = CredentialStore(
        paths.extension_credential,
        description="Context Atomizer Local extension pairing secret",
    ).load()
    require(stored_extension == extension, "paired response and protected extension store diverged")
    require(extension != management, "management and capture credentials were interchangeable")
    require(extension.encode("ascii") not in paths.extension_credential.read_bytes(),
            "extension pairing secret was stored in plaintext")
    assert_runtime_proof(bridge, extension)
    capture_status, capture = signed_capture(
        bridge, extension, event_id="installer-packaged-security-fresh"
    )
    require(capture_status == 200 and capture.get("ok") is True and capture.get("captured") is not False,
            "paired extension could not perform authenticated synthetic capture")
    management_capture, _ = signed_capture(
        bridge, management, event_id="installer-management-must-not-capture"
    )
    require(management_capture == 401, "management credential authenticated extension capture")
    assert_extension_cannot_manage(bridge, library, extension)
    assert_no_secret_disclosure(paths, receipt, [management, extension])
    return {
        "management_initialized": True,
        "management_dpapi_protected": True,
        "extension_absent_before_pairing": True,
        "ephemeral_authority_not_persisted": True,
        "bootstrap_removed": True,
        "library_session_one_time": True,
        "extension_pairing_explicit": True,
        "authenticated_capture": True,
        "authority_separated": True,
        "secrets_not_disclosed": True,
    }


def post_reinstall(data_directory: Path, receipt: Path) -> dict[str, bool]:
    paths, bridge, library, management = load_context(data_directory)
    require(paths.extension_credential.is_file(), "reinstall removed the paired extension secret")
    store = CredentialStore(
        paths.extension_credential,
        description="Context Atomizer Local extension pairing secret",
    )
    old_extension = store.load()
    status = management_status(bridge, management)
    require(status["extension"]["paired"] is True, "reinstall lost paired extension state")
    assert_runtime_proof(bridge, old_extension)
    assert_extension_cannot_manage(bridge, library, old_extension)

    opener, csrf = library_session(bridge, library, management)
    revoke, _ = form_request(
        opener, library, "/extension/revoke", {"csrf_token": csrf}
    )
    require(revoke == 200, "Library session could not revoke extension pairing")
    require(not paths.extension_credential.exists(), "revoke left the extension secret at rest")
    old_capture, _ = signed_capture(
        bridge, old_extension, event_id="installer-revoked-extension-must-fail"
    )
    require(old_capture == 401, "revoked extension secret still authenticated capture")
    require(management_status(bridge, management)["extension"]["paired"] is False,
            "revoked extension remained paired in runtime status")

    new_extension = pair_extension(bridge, library, opener, csrf)
    require(new_extension != old_extension, "re-pair reused the revoked extension secret")
    require(paths.extension_credential.is_file(), "re-pair did not restore protected extension state")
    assert_runtime_proof(bridge, new_extension)
    assert_no_secret_disclosure(paths, receipt, [management, old_extension, new_extension])
    return {
        "management_preserved": True,
        "extension_pairing_preserved": True,
        "revoke_invalidated_old_secret": True,
        "re_pair_required": True,
        "uninstall_cleanup_precondition": True,
        "secrets_not_disclosed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("fresh", "post-reinstall"))
    parser.add_argument("--data-directory", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.phase == "fresh":
        result = fresh(arguments.data_directory, arguments.receipt)
    else:
        result = post_reinstall(arguments.data_directory, arguments.receipt)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
