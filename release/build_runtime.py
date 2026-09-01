from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from versioning import read_release_version


EXECUTABLES = (
    ("atomizer-local-runtime", "runtime.py", True),
    ("atomizer-local-manager", "manager.py", False),
    ("atomizer-local-open-library", "open_library.py", True),
    ("atomizer-codex-hook", "codex_hook.py", False),
    ("atomizer-claude-hook", "claude_hook.py", False),
    ("atomizer-local-mcp", "mcp.py", False),
)


def build_runtime(project_root: Path, output_directory: Path) -> None:
    project_root = Path(project_root).resolve()
    output_directory = Path(output_directory).resolve()
    read_release_version(project_root)
    if output_directory.exists() and any(output_directory.iterdir()):
        raise FileExistsError("runtime output directory must be empty")
    output_directory.mkdir(parents=True, exist_ok=True)
    source_root = project_root / "src"
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from atomizer_local_client.runtime_health import (
        BUILD_IDENTITY_FILENAME,
        runtime_build_fingerprint,
    )

    build_fingerprint = runtime_build_fingerprint(
        source_root / "atomizer_local_client"
    )
    identity_path = output_directory / BUILD_IDENTITY_FILENAME
    identity_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_build_fingerprint": build_fingerprint,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    work_root = output_directory.parent / "pyinstaller-work"
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True)
    migrations = project_root / "src" / "atomizer_local_client" / "history" / "migrations"
    entries = project_root / "release" / "entrypoints"
    for name, entrypoint, windowed in EXECUTABLES:
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            name,
            "--paths",
            str(project_root / "src"),
            "--copy-metadata",
            "context-atomizer-local-client",
            "--add-data",
            f"{migrations}{os.pathsep}atomizer_local_client/history/migrations",
            "--add-data",
            f"{identity_path}{os.pathsep}atomizer_local_client",
            "--distpath",
            str(output_directory),
            "--workpath",
            str(work_root / name),
            "--specpath",
            str(work_root),
            "--windowed" if windowed and sys.platform == "win32" else "--console",
            str(entries / entrypoint),
        ]
        subprocess.run(command, cwd=project_root, check=True)
    shutil.copytree(
        source_root / "atomizer_local_client" / "portable_plugin",
        output_directory / "portable_plugin",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    build_runtime(arguments.project_root, arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
