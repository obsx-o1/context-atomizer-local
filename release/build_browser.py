from __future__ import annotations

import argparse
import importlib.util
import shutil
import zipfile
from pathlib import Path

from versioning import read_release_version


def build_chromium(project_root: Path, output_file: Path) -> Path:
    project_root = Path(project_root).resolve()
    output_file = Path(output_file).resolve()
    version = read_release_version(project_root)
    expected_name = f"ContextAtomizer-Chromium-{version.label}.zip"
    if output_file.name != expected_name:
        raise ValueError(f"Chromium artifact must be named {expected_name}")
    staging = output_file.parent / "chromium-package"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    module_path = project_root / "browser_extension" / "package_extension.py"
    specification = importlib.util.spec_from_file_location("release_extension_builder", module_path)
    module = importlib.util.module_from_spec(specification)
    assert specification.loader is not None
    specification.loader.exec_module(module)
    module.build_package("chromium", staging)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_file, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(staging.rglob("*"), key=lambda item: item.relative_to(staging).as_posix()):
            if not path.is_file():
                continue
            relative = path.relative_to(staging).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    return staging


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    build_chromium(arguments.project_root, arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
