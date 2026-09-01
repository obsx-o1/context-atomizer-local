"""macOS per-user runtime directory permissions."""

from __future__ import annotations

import os
from pathlib import Path


def ensure_private_directory(path: Path) -> None:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
