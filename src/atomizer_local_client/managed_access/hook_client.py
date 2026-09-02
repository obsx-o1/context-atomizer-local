"""Fail-open native-hook client for verified managed context only."""

from __future__ import annotations

import http.client
import json
from pathlib import Path

from atomizer_local_client.chat.contracts import ChatEvent
from atomizer_local_client.managed_access.policy import LibraryAccessPolicyStore
from atomizer_local_client.memory_access.access_gate import DirectLibraryAccessMode
from atomizer_local_client.platforms.credentials import current_credential_store
from atomizer_local_client.local_auth.contracts import BRIDGE_PORT
from atomizer_local_client.runtime.configuration import RuntimePaths


def request_managed_context(
    event: ChatEvent,
    database_path: Path,
    *,
    port: int = BRIDGE_PORT,
) -> str | None:
    """Return bounded context or None; never persist or log prompt/context here."""

    if (
        event.host_project_reference is None
        or event.host_chat_reference is None
        or event.host_turn_reference is None
    ):
        return None
    paths = RuntimePaths.for_root(Path(database_path).parent)
    try:
        if LibraryAccessPolicyStore(paths.access_policy).mode() != (
            DirectLibraryAccessMode.MANAGED_EXCLUSIVE
        ):
            return None
        token = current_credential_store(paths.credential).load()
        body = json.dumps(
            {
                "host": event.host.value,
                "host_session_reference": event.host_chat_reference,
                "host_turn_reference": event.host_turn_reference,
                "scope_reference": event.host_project_reference,
                "prompt": event.content,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=4.0)
        try:
            connection.request(
                "POST",
                "/v1/managed/context/request",
                body=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            raw = response.read(64 * 1024)
        finally:
            connection.close()
        if response.status != 200:
            return None
        payload = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "available"
            or payload.get("host_turn_reference") != event.host_turn_reference
            or payload.get("scope_reference") != event.host_project_reference
        ):
            return None
        context = payload.get("context")
        return context if isinstance(context, str) and context.strip() else None
    except BaseException:
        return None


__all__ = ["request_managed_context"]
