"""Select the native per-user credential adapter without changing its contract."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def current_credential_store(
    path: Path,
    *,
    description: str = "Context Atomizer Local management credential",
) -> Any:
    if sys.platform == "darwin":
        from atomizer_local_client.platforms.macos.keychain import (
            MacOSKeychainCredentialStore,
        )

        return MacOSKeychainCredentialStore(path, description=description)
    from atomizer_local_client.runtime.credentials import CredentialStore

    return CredentialStore(path, description=description)
