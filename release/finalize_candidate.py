"""Finalize an already-built candidate after all isolated lifecycle receipts pass."""

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
    parser.add_argument("--receipt", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    candidate = json.loads(args.candidate.read_text(encoding="utf-8-sig"))
    candidate_root = args.candidate.parent
    expected = {"normal", "ambiguous", "failure"}
    receipts: dict[str, dict[str, object]] = {}
    for path in args.receipt:
        receipt = json.loads(path.read_text(encoding="utf-8-sig"))
        scenario = receipt.get("scenario")
        if scenario in receipts or scenario not in expected or receipt.get("passed") is not True:
            raise RuntimeError("lifecycle receipt set is not three unique passes")
        if receipt.get("git_commit_sha") != candidate["git_commit_sha"]:
            raise RuntimeError("lifecycle receipt commit mismatch")
        if receipt.get("installer_sha256") != candidate["installer"]["sha256"]:
            raise RuntimeError("lifecycle receipt installer mismatch")
        if receipt.get("installer_size_bytes") != candidate["installer"]["size_bytes"]:
            raise RuntimeError("lifecycle receipt installer size mismatch")
        receipts[scenario] = receipt
    if set(receipts) != expected:
        raise RuntimeError("required lifecycle receipts are missing")
    installer = candidate_root / candidate["installer"]["name"]
    chromium = candidate_root / candidate["chromium"]["name"]
    if sha256(installer) != candidate["installer"]["sha256"] or installer.stat().st_size != candidate["installer"]["size_bytes"]:
        raise RuntimeError("final installer identity mismatch")
    if sha256(chromium) != candidate["chromium"]["sha256"]:
        raise RuntimeError("final Chromium identity mismatch")
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(installer, args.output / installer.name)
    shutil.copy2(chromium, args.output / chromium.name)
    for scenario, receipt in sorted(receipts.items()):
        (args.output / f"lifecycle-{scenario}.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {**candidate, "lifecycle": {name: {"passed": True, "evidence": receipt["evidence"]} for name, receipt in sorted(receipts.items())}}
    manifest_path = args.output / "ContextAtomizer-v0.1.0-dev0-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksummed = [installer.name, chromium.name, manifest_path.name, *(f"lifecycle-{name}.json" for name in sorted(receipts))]
    sums = args.output / "ContextAtomizer-v0.1.0-dev0-SHA256SUMS.txt"
    sums.write_text("".join(f"{sha256(args.output / name)}  {name}\n" for name in checksummed), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
