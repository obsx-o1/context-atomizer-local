"""Content-safe Codex workspace discovery and ownership diagnostics."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

PROJECT_SOURCE = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(PROJECT_SOURCE))

from atomizer_local_client.runtime.codex_integration import classify_codex_hook
from atomizer_local_client.runtime.codex_workspace import CodexWorkspaceSource


def diagnose(config: Path, global_hooks: Path, current_command: str, labels: dict[Path, str]) -> dict[str, object]:
    targets = CodexWorkspaceSource(config, global_hooks_path=global_hooks).discover()
    results: list[dict[str, object]] = []
    for target in targets:
        resolved = target.hooks_path.resolve()
        label = labels.get(resolved)
        if label is None:
            raise RuntimeError("discovery returned an unlabeled target")
        hooks = json.loads(resolved.read_text(encoding="utf-8-sig"))
        events: dict[str, object] = {}
        for event in ("UserPromptSubmit", "Stop"):
            entries = hooks.get("hooks", {}).get(event, [])
            commands = [hook for group in entries for hook in group.get("hooks", [])]
            events[event] = {
                "count": len(commands),
                "classes": [classify_codex_hook(event, hook, current_command).value for hook in commands],
            }
        results.append({"label": label, "events": events})
    return {"schema_version": 1, "discovered_count": len(results), "targets": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--global-hooks", type=Path, required=True)
    parser.add_argument("--current-command-base64", required=True)
    parser.add_argument("--target", action="append", nargs=2, metavar=("LABEL", "PATH"), default=[])
    args = parser.parse_args()
    try:
        command = base64.b64decode(args.current_command_base64, validate=True).decode("utf-8")
        labels = {Path(path).resolve(): label for label, path in args.target}
        payload = diagnose(args.config, args.global_hooks, command, labels)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError, RuntimeError) as error:
        print(json.dumps({"schema_version": 1, "passed": False, "failure_class": type(error).__name__}, sort_keys=True))
        return 1
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
