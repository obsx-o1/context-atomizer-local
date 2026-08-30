from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


_DEVELOPMENT_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.dev(\d+)$")


@dataclass(frozen=True, slots=True)
class ReleaseVersion:
    package: str
    label: str
    browser: str


def read_release_version(project_root: Path) -> ReleaseVersion:
    payload = tomllib.loads((Path(project_root) / "pyproject.toml").read_text(encoding="utf-8"))
    package = str(payload["project"]["version"])
    match = _DEVELOPMENT_VERSION.fullmatch(package)
    if match is None:
        raise ValueError("package version must be a PEP 440 development version")
    major, minor, patch, development = match.groups()
    return ReleaseVersion(
        package=package,
        label=f"v{major}.{minor}.{patch}-dev{development}",
        browser=f"{major}.{minor}.{patch}.{development}",
    )
