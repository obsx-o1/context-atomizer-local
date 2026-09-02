"""Finalize an already-built candidate after all isolated lifecycle jobs pass."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--lifecycle", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    candidate = json.loads(args.candidate.read_text(encoding="utf-8-sig"))
    candidate_root = args.candidate.parent
    expected = {"normal", "ambiguous", "failure"}
    lifecycle: dict[str, str] = {}
    for value in args.lifecycle:
        scenario, separator, result = value.partition("=")
        if separator != "=" or scenario in lifecycle or scenario not in expected or result != "PASS":
            raise RuntimeError("lifecycle result set is not three unique passes")
        lifecycle[scenario] = result
    if set(lifecycle) != expected:
        raise RuntimeError("required lifecycle results are missing")
    installer = candidate_root / candidate["installer"]["name"]
    chromium = candidate_root / candidate["chromium"]["name"]
    if sha256(installer) != candidate["installer"]["sha256"] or installer.stat().st_size != candidate["installer"]["size_bytes"]:
        raise RuntimeError("final installer identity mismatch")
    if sha256(chromium) != candidate["chromium"]["sha256"]:
        raise RuntimeError("final Chromium identity mismatch")
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(installer, args.output / installer.name)
    shutil.copy2(chromium, args.output / chromium.name)
    manifest = {**candidate, "lifecycle": {name: {"passed": True} for name in sorted(lifecycle)}}
    manifest_path = args.output / "ContextAtomizer-v0.2.0-dev0-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksummed = [installer.name, chromium.name, manifest_path.name]
    sums = args.output / "ContextAtomizer-v0.2.0-dev0-SHA256SUMS.txt"
    sums.write_text("".join(f"{sha256(args.output / name)}  {name}\n" for name in checksummed), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
