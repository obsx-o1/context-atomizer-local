"""Build one browser package from shared core assets and a thin manifest."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tomllib
from pathlib import Path


_DEVELOPMENT_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.dev(\d+)$")


def release_versions() -> tuple[str, str]:
    """Derive browser and display versions from the canonical package version."""

    project_root = Path(__file__).resolve().parent.parent
    project = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    package_version = str(project["project"]["version"])
    match = _DEVELOPMENT_VERSION.fullmatch(package_version)
    if match is None:
        raise ValueError("packaging requires a PEP 440 development version")
    major, minor, patch, development = match.groups()
    return f"{major}.{minor}.{patch}.{development}", f"{major}.{minor}.{patch}-dev{development}"


def build_package(browser: str, output_directory: Path) -> Path:
    if browser not in {"chromium", "firefox"}:
        raise ValueError("browser must be chromium or firefox")
    source_root = Path(__file__).resolve().parent
    output_directory = Path(output_directory).resolve()
    if output_directory.exists() and any(output_directory.iterdir()):
        raise FileExistsError("output directory must be empty")
    output_directory.mkdir(parents=True, exist_ok=True)
    for relative in (Path("core"), Path("browsers/shared"), Path("browsers/claude")):
        target = output_directory / relative
        shutil.copytree(source_root / relative, target)
    manifest_source = source_root / "browsers" / browser / "manifest.json"
    manifest = json.loads(manifest_source.read_text(encoding="utf-8"))
    browser_version, release_label = release_versions()
    manifest["version"] = browser_version
    manifest["version_name"] = release_label
    manifest_target = output_directory / "manifest.json"
    manifest_target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_target


def main() -> int:
    parser = argparse.ArgumentParser(description="Package the shared ChatGPT web sensor")
    parser.add_argument("browser", choices=("chromium", "firefox"))
    parser.add_argument("output_directory", type=Path)
    arguments = parser.parse_args()
    build_package(arguments.browser, arguments.output_directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
