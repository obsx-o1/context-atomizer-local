"""Open a validated local URL with the native macOS user application."""

from __future__ import annotations

import subprocess


def open_url(url: str) -> bool:
    return (
        subprocess.run(
            ["/usr/bin/open", url],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )
