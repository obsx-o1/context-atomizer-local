from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from versioning import read_release_version


_NSIS_VERSION = re.compile(r"\bv([0-9]+(?:\.[0-9]+)*)\b", re.IGNORECASE)
_QUIET_UNINSTALL_FLAGS = "-NoProfile -NonInteractive -EncodedCommand"
_WINDOWS_COMMAND_LINE_LIMIT = 32767
_NSIS_MAX_STRING_LENGTH = 1024


def normalized_quiet_uninstall_source(project_root: Path) -> str:
    source = (
        Path(project_root).resolve()
        / "release"
        / "windows"
        / "quiet_uninstall.ps1"
    ).read_text(encoding="utf-8")
    return "".join(line.strip() for line in source.splitlines())


def quiet_uninstall_command(project_root: Path) -> str:
    script = normalized_quiet_uninstall_source(project_root)
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def quiet_uninstall_registry_command(
    project_root: Path, system_directory: str = r"C:\Windows\System32"
) -> str:
    power_shell = system_directory.rstrip("\\") + r"\WindowsPowerShell\v1.0\powershell.exe"
    return f'"{power_shell}" {_QUIET_UNINSTALL_FLAGS} {quiet_uninstall_command(project_root)}'


def validate_runtime_source_closure(
    project_root: Path, runtime_directory: Path
) -> str:
    project_root = Path(project_root).resolve()
    runtime_directory = Path(runtime_directory).resolve()
    source_root = project_root / "src"
    package_root = source_root / "atomizer_local_client"
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from atomizer_local_client.runtime_health import (
        BUILD_IDENTITY_FILENAME,
        runtime_build_fingerprint,
    )

    identity_path = runtime_directory / BUILD_IDENTITY_FILENAME
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("runtime build identity could not be read") from exc
    actual = identity.get("runtime_build_fingerprint")
    expected = runtime_build_fingerprint(package_root)
    if actual != expected:
        raise RuntimeError(
            "runtime build identity does not match project source closure"
        )
    return expected


def compiler_version(compiler: Path) -> str:
    result = subprocess.run(
        [str(Path(compiler).resolve()), "/VERSION"],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    match = _NSIS_VERSION.search(result.stdout + result.stderr)
    if match is None:
        raise RuntimeError("could not determine NSIS compiler version")
    return match.group(1)


def build_installer(
    project_root: Path, runtime_directory: Path, output_directory: Path, compiler: Path
) -> Path:
    project_root = Path(project_root).resolve()
    runtime_directory = Path(runtime_directory).resolve()
    output_directory = Path(output_directory).resolve()
    compiler = Path(compiler).resolve()
    version = read_release_version(project_root)
    validate_runtime_source_closure(project_root, runtime_directory)
    actual = compiler_version(compiler)
    if actual != "3.12":
        raise RuntimeError(f"NSIS 3.12 is required, found {actual}")
    required = {
        "atomizer-local-runtime.exe",
        "atomizer-local-manager.exe",
        "atomizer-local-open-library.exe",
        "atomizer-codex-hook.exe",
    }
    present = {path.name for path in runtime_directory.glob("*.exe")}
    if not required.issubset(present):
        raise FileNotFoundError(f"missing runtime executables: {sorted(required - present)}")
    quiet_payload = quiet_uninstall_command(project_root)
    quiet_registry_command = quiet_uninstall_registry_command(project_root)
    if len(quiet_registry_command) >= _NSIS_MAX_STRING_LENGTH:
        raise ValueError("quiet uninstall registry command exceeds the stock NSIS string limit")
    if len(quiet_registry_command) >= _WINDOWS_COMMAND_LINE_LIMIT:
        raise ValueError("quiet uninstall registry command exceeds the Windows command-line limit")
    output_directory.mkdir(parents=True, exist_ok=True)
    script = project_root / "release" / "windows" / "ContextAtomizer.nsi"
    subprocess.run(
        [
            str(compiler),
            "/WX",
            "/V3",
            f"/DSourceDir={runtime_directory}",
            f"/DOutputDir={output_directory}",
            f"/DPackageVersion={version.package}",
            f"/DReleaseLabel={version.label}",
            f"/DQuietUninstallCommand={quiet_payload}",
            str(script),
        ],
        cwd=project_root,
        check=True,
    )
    artifact = output_directory / f"ContextAtomizer-Setup-{version.label}.exe"
    if not artifact.is_file():
        raise FileNotFoundError(artifact)
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compiler", type=Path, required=True)
    arguments = parser.parse_args()
    build_installer(arguments.project_root, arguments.runtime, arguments.output, arguments.compiler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
