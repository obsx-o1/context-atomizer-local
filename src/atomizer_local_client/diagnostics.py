"""Bounded, content-free local capture error logging."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def record_capture_error(data_directory: Path, code: str, error: BaseException) -> None:
    safe_code = "".join(character for character in code if character.isalnum() or character in "_-")[:48]
    safe_type = type(error).__name__[:64]
    line = f"{datetime.now(timezone.utc).isoformat()} {safe_code or 'capture_error'} {safe_type}\n"
    try:
        data_directory.mkdir(parents=True, exist_ok=True)
        log_path = data_directory / "capture-errors.log"
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(line)
    except OSError:
        pass

