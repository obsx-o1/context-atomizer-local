"""Bind the one CI-built installer to source, runtime, toolchain, and validation evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--runtime-identity", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--source-fingerprint", required=True)
    parser.add_argument("--nsis-version", required=True)
    parser.add_argument("--nsis-archive-sha256", required=True)
    parser.add_argument("--python-tests", type=int, required=True)
    parser.add_argument("--browser-tests", type=int, required=True)
    parser.add_argument("--javascript-files", type=int, required=True)
    args = parser.parse_args()
    identity = json.loads(args.runtime_identity.read_text(encoding="utf-8"))
    runtime_fingerprint = identity.get("runtime_build_fingerprint")
    if runtime_fingerprint != args.source_fingerprint:
        raise RuntimeError("source/runtime fingerprint mismatch")
    if args.nsis_version != "3.12":
        raise RuntimeError("NSIS 3.12 is required")
    payload = {
        "schema_version": 1,
        "git_commit_sha": args.commit_sha,
        "installer": {"name": args.installer.name, "sha256": sha256(args.installer), "size_bytes": args.installer.stat().st_size},
        "chromium": {"name": args.chromium.name, "sha256": sha256(args.chromium), "size_bytes": args.chromium.stat().st_size},
        "runtime": {"executable_sha256": sha256(args.runtime), "build_fingerprint": runtime_fingerprint},
        "source_fingerprint": args.source_fingerprint,
        "source_runtime_equal": True,
        "nsis": {"version": args.nsis_version, "archive_sha256": args.nsis_archive_sha256, "compressor": "zlib", "third_party_plugins": False},
        "validation": {"python_tests": args.python_tests, "browser_tests": args.browser_tests, "javascript_syntax_files": args.javascript_files},
        "signing": {
            "status": "unsigned-development-build",
            "broad_commercial_release_allowed": False,
            "broad_commercial_release_requirement": "authenticode-signing",
        },
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
