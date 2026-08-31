"""Fail closed on common credential, machine-state, and export-hygiene leaks."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


TEXT_SUFFIXES = {
    ".css", ".html", ".js", ".json", ".md", ".nsi", ".ps1", ".py",
    ".sh", ".sql", ".toml", ".txt", ".yml", ".yaml",
}
IGNORED_PARTS = {
    ".git", ".venv", "artifacts", "build", "dist", "node_modules", "__pycache__",
}
BINARY_OR_STATE_SUFFIXES = {
    ".db", ".dmp", ".dump", ".exe", ".key", ".log", ".p12", ".pem",
    ".pfx", ".sqlite", ".sqlite3",
}
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\bgh[opusr]_[A-Za-z0-9_]{30,}\b"),
    "generic_api_secret": re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret)\b\s*[:=]\s*['\"][^'\"]{12,}['\"]"
    ),
    "windows_profile": re.compile(r"(?i)\b[A-Z]:\\Users\\(?!Synthetic(?:User)?\\)[^\\\s'\"]+\\"),
    "posix_profile": re.compile(r"/(?:Users|home)/(?!synthetic/)[^/\s'\"]+/"),
}


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
            continue
        yield path


def _git_findings(root: Path) -> list[dict[str, object]]:
    if not (root / ".git").exists():
        return []
    environment = {**os.environ, "GIT_CONFIG_COUNT": "0"}
    command = [
        "git",
        "-c",
        f"safe.directory={root.as_posix()}",
        "-C",
        str(root),
    ]

    def lines(*arguments: str) -> list[str]:
        result = subprocess.run(
            [*command, *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        if result.returncode not in {0, 1}:
            raise subprocess.CalledProcessError(result.returncode, result.args)
        return [line for line in result.stdout.splitlines() if line]

    findings: list[dict[str, object]] = []
    try:
        current = lines("symbolic-ref", "--quiet", "--short", "HEAD")
        current_ref = f"refs/heads/{current[0]}" if current else None
        refs = lines("for-each-ref", "--format=%(refname)")
        for reference in refs:
            unexpected = (
                reference.startswith("refs/notes/")
                or reference.startswith("refs/tags/")
                or (reference.startswith("refs/heads/") and reference != current_ref)
            )
            if unexpected:
                findings.append({"category": "unexpected_git_ref", "ref": reference})
        remotes = lines("remote")
        for remote in remotes:
            if remote != "origin":
                findings.append({"category": "unexpected_git_remote", "remote": remote})
        pushes = lines("config", "--get-all", "remote.origin.push")
        for refspec in pushes:
            findings.append({"category": "explicit_push_refspec", "refspec": refspec})
    except (OSError, subprocess.CalledProcessError) as error:
        findings.append({"category": "git_audit_unavailable", "error": type(error).__name__})
    return findings


def audit(root: Path, *, inspect_git: bool = False) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    scanned = 0
    for path in iter_files(root):
        relative = path.relative_to(root).as_posix()
        scanned += 1
        if path.suffix.lower() in BINARY_OR_STATE_SUFFIXES:
            findings.append({"category": "binary_or_local_state", "path": relative})
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {".gitattributes", ".gitignore"}:
            continue
        try:
            value = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append({"category": "unexpected_binary", "path": relative})
            continue
        if relative == "tools/repository_audit.py":
            continue
        for category, pattern in PATTERNS.items():
            for match in pattern.finditer(value):
                findings.append({
                    "category": category,
                    "path": relative,
                    "line": value.count("\n", 0, match.start()) + 1,
                })
    if inspect_git:
        findings.extend(_git_findings(root))
    return {"passed": not findings, "scanned_files": scanned, "findings": findings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--inspect-git", action="store_true")
    arguments = parser.parse_args()
    result = audit(arguments.root.resolve(), inspect_git=arguments.inspect_git)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
