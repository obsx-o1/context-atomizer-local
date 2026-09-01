from __future__ import annotations

import argparse
import json
import platform
import shutil
import struct
import tarfile
import tempfile
from pathlib import Path

from versioning import read_release_version


EXECUTABLES = (
    "atomizer-local-runtime",
    "atomizer-local-manager",
    "atomizer-local-open-library",
    "atomizer-codex-hook",
    "atomizer-claude-hook",
    "atomizer-local-mcp",
)
CPU_TYPES = {"arm64": 0x0100000C, "x86_64": 0x01000007}
MACHO_64_MAGIC = 0xFEEDFACF


def _macho_architecture(path: Path) -> str:
    header = Path(path).read_bytes()[:8]
    if len(header) != 8:
        raise ValueError(f"runtime executable is not a Mach-O 64 binary: {path.name}")
    magic, cpu_type = struct.unpack("<II", header)
    if magic != MACHO_64_MAGIC:
        raise ValueError(
            f"runtime executable is not a thin Mach-O 64 binary: {path.name}"
        )
    for architecture, expected in CPU_TYPES.items():
        if cpu_type == expected:
            return architecture
    raise ValueError(f"runtime executable has unsupported CPU type: {path.name}")


def build_macos_artifact(
    project_root: Path,
    runtime_directory: Path,
    output_directory: Path,
    architecture: str,
) -> Path:
    project_root = Path(project_root).resolve()
    runtime_directory = Path(runtime_directory).resolve()
    output_directory = Path(output_directory).resolve()
    if architecture not in CPU_TYPES:
        raise ValueError("macOS architecture must be arm64 or x86_64")
    if platform.system() != "Darwin" or platform.machine() != architecture:
        raise RuntimeError(
            f"macOS artifact must be built natively on {architecture}, found "
            f"{platform.system()} {platform.machine()}"
        )
    for name in EXECUTABLES:
        executable = runtime_directory / name
        if _macho_architecture(executable) != architecture:
            raise RuntimeError(f"runtime executable architecture mismatch: {name}")
    identity = runtime_directory / "runtime-build-identity.json"
    if not identity.is_file():
        raise FileNotFoundError(identity)
    portable_plugin = runtime_directory / "portable_plugin"
    if not (portable_plugin / "plugin.json").is_file():
        raise FileNotFoundError("portable Agent Plugin payload is missing")

    version = read_release_version(project_root)
    output_directory.mkdir(parents=True, exist_ok=True)
    artifact = (
        output_directory
        / f"ContextAtomizerLocal-macos-{architecture}-{version.label}.tar.gz"
    )
    if artifact.exists():
        raise FileExistsError(artifact)
    with tempfile.TemporaryDirectory() as temporary:
        bundle = Path(temporary) / "ContextAtomizerLocal"
        binaries = bundle / "bin"
        binaries.mkdir(parents=True)
        for name in EXECUTABLES:
            target = binaries / name
            shutil.copy2(runtime_directory / name, target)
            target.chmod(0o700)
        shutil.copy2(identity, binaries / identity.name)
        shutil.copytree(portable_plugin, bundle / "portable_plugin")
        shutil.copy2(project_root / "release" / "macos" / "install.sh", bundle / "install.sh")
        shutil.copy2(
            project_root / "release" / "macos" / "uninstall.sh", bundle / "uninstall.sh"
        )
        (bundle / "install.sh").chmod(0o700)
        (bundle / "uninstall.sh").chmod(0o700)
        shutil.copy2(project_root / "LICENSE", bundle / "LICENSE")
        shutil.copy2(project_root / "MACOS.md", bundle / "MACOS.md")
        (bundle / "artifact-metadata.json").write_text(
            json.dumps(
                {
                    "architecture": architecture,
                    "notarized": False,
                    "package_version": version.package,
                    "signed": False,
                    "support": "experimental",
                    "validation": "native-ci-required-before-distribution",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        with tarfile.open(artifact, "w:gz") as archive:
            archive.add(bundle, arcname=bundle.name)
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parent.parent
    )
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--architecture", choices=tuple(CPU_TYPES), required=True)
    arguments = parser.parse_args()
    build_macos_artifact(
        arguments.project_root,
        arguments.runtime,
        arguments.output,
        arguments.architecture,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
