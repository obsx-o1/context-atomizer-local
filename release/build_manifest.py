from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from versioning import read_release_version


def sha256(path: Path) -> str:
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def build_timestamp() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    moment = datetime.fromtimestamp(int(epoch), UTC) if epoch else datetime.now(UTC)
    return moment.isoformat().replace("+00:00", "Z")


def tool_version(command: list[str]) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True, errors="replace")
    return (result.stdout or result.stderr).strip().splitlines()[0]


def build_manifest(
    project_root: Path,
    artifact_directory: Path,
    commit_sha: str,
    installer_compiler: Path,
    python_tests: int,
    browser_tests: int,
    javascript_files: int,
) -> tuple[Path, Path]:
    project_root = Path(project_root).resolve()
    artifact_directory = Path(artifact_directory).resolve()
    version = read_release_version(project_root)
    from build_windows import compiler_version

    installer_builder_version = compiler_version(Path(installer_compiler))
    if installer_builder_version != "3.12":
        raise RuntimeError(
            f"NSIS 3.12 is required, found {installer_builder_version}"
        )
    installer = artifact_directory / f"ContextAtomizer-Setup-{version.label}.exe"
    chromium = artifact_directory / f"ContextAtomizer-Chromium-{version.label}.zip"
    runtime = artifact_directory.parent / "runtime" / "atomizer-local-runtime.exe"
    runtime_identity = artifact_directory.parent / "runtime" / "runtime-build-identity.json"
    for required in (installer, chromium, runtime, runtime_identity):
        if not required.is_file():
            raise FileNotFoundError(required)
    identity_payload = json.loads(runtime_identity.read_text(encoding="utf-8"))
    runtime_build_fingerprint = identity_payload.get("runtime_build_fingerprint")
    if not isinstance(runtime_build_fingerprint, str):
        raise RuntimeError("runtime build identity is missing")
    runtime_executable_sha256 = sha256(runtime)
    migration_root = project_root / "src/atomizer_local_client/history/migrations"
    migrations = sorted(path.stem for path in migration_root.glob("*.sql"))
    payload = {
        "schema_version": 1,
        "package_version": version.package,
        "build_label": version.label,
        "git_commit_sha": commit_sha,
        "build_timestamp": build_timestamp(),
        "runtime_build_fingerprint": runtime_build_fingerprint,
        "runtime_executable_sha256": runtime_executable_sha256,
        "migrations": migrations,
        "validation": {
            "python_tests": python_tests,
            "browser_tests": browser_tests,
            "javascript_syntax_files": javascript_files,
        },
        "artifacts": {
            installer.name: {"sha256": sha256(installer)},
            chromium.name: {"sha256": sha256(chromium)},
        },
        "toolchain": {
            "python": platform.python_version(),
            "pyinstaller": importlib.metadata.version("PyInstaller"),
            "installer_builder": "NSIS",
            "installer_builder_version": installer_builder_version,
            "node": tool_version(["node", "--version"]),
        },
        "signing": {
            "windows_installer": "unsigned-development-build",
            "broad_commercial_release_allowed": False,
            "broad_commercial_release_requirement": "authenticode-signing",
        },
    }
    manifest = artifact_directory / f"ContextAtomizer-{version.label}-manifest.json"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sums = artifact_directory / f"ContextAtomizer-{version.label}-SHA256SUMS.txt"
    names = (installer.name, chromium.name, manifest.name)
    sums.write_text("".join(f"{sha256(artifact_directory / name)}  {name}\n" for name in names), encoding="utf-8")
    return manifest, sums


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--installer-compiler", type=Path, required=True)
    parser.add_argument("--python-tests", type=int, required=True)
    parser.add_argument("--browser-tests", type=int, required=True)
    parser.add_argument("--javascript-files", type=int, required=True)
    arguments = parser.parse_args()
    build_manifest(
        arguments.project_root,
        arguments.artifacts,
        arguments.commit_sha,
        arguments.installer_compiler,
        arguments.python_tests,
        arguments.browser_tests,
        arguments.javascript_files,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
