"""Bounded discovery of Codex workspace hook files registered by Codex."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SUPPORTED_STATE_EVENTS = frozenset({"user_prompt_submit", "stop"})
WORKSPACE_DISCOVERY_CONTRACT_VERSION = "codex-hook-state-workspaces-v1"


class CodexWorkspaceDiscoveryError(ValueError):
    """Codex workspace registration is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class WorkspaceHookTarget:
    """One existing workspace hook file explicitly registered by Codex."""

    workspace_root: Path
    hooks_path: Path


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _canonical_path_identity(path: Path, *, strict: bool) -> str:
    """Return one filesystem-resolved identity for path comparisons."""

    try:
        resolved = Path(path).resolve(strict=strict)
    except (OSError, RuntimeError) as exc:
        raise CodexWorkspaceDiscoveryError(
            "Codex hook path could not be canonicalized safely"
        ) from exc
    return _normalized(resolved)


def _parse_state_registration(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    parts = value.rsplit(":", 3)
    if len(parts) != 4:
        if "hooks.json" in value.casefold():
            raise CodexWorkspaceDiscoveryError(
                "Codex workspace hook registration is malformed"
            )
        return None
    source, event, matcher_index, hook_index = parts
    if event not in _SUPPORTED_STATE_EVENTS:
        return None
    try:
        indexes = (int(matcher_index), int(hook_index))
    except ValueError as exc:
        raise CodexWorkspaceDiscoveryError(
            "Codex workspace hook registration indexes are malformed"
        ) from exc
    if any(index < 0 for index in indexes):
        raise CodexWorkspaceDiscoveryError(
            "Codex workspace hook registration indexes are malformed"
        )
    return source, event


def _validated_existing_target(source: str) -> WorkspaceHookTarget | None:
    candidate = Path(source)
    if not candidate.is_absolute():
        raise CodexWorkspaceDiscoveryError(
            "Codex workspace hook registration must use an absolute path"
        )
    if (
        candidate.name.casefold() != "hooks.json"
        or candidate.parent.name.casefold() != ".codex"
    ):
        return None
    if not candidate.exists():
        return None
    if not candidate.is_file():
        raise CodexWorkspaceDiscoveryError(
            "Codex workspace hook registration is not a file"
        )
    resolved = candidate.resolve(strict=True)
    if _normalized(candidate) != _normalized(resolved):
        raise CodexWorkspaceDiscoveryError(
            "Codex workspace hook registration escapes through a link or traversal"
        )
    workspace_root = resolved.parent.parent
    expected = workspace_root / ".codex" / "hooks.json"
    if _normalized(expected) != _normalized(resolved):
        raise CodexWorkspaceDiscoveryError(
            "Codex workspace hook registration is outside its workspace root"
        )
    return WorkspaceHookTarget(workspace_root=workspace_root, hooks_path=resolved)


class CodexWorkspaceSource:
    """Read workspace hook targets only from Codex's authoritative hook state."""

    def __init__(self, config_path: Path, *, global_hooks_path: Path) -> None:
        self.config_path = Path(config_path)
        self.global_hooks_path = Path(global_hooks_path)

    def discover(self) -> tuple[WorkspaceHookTarget, ...]:
        if not self.config_path.exists():
            return ()
        try:
            payload = tomllib.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise CodexWorkspaceDiscoveryError(
                "Codex configuration could not be read safely"
            ) from exc
        hooks = payload.get("hooks", {})
        if not isinstance(hooks, dict):
            raise CodexWorkspaceDiscoveryError("Codex hooks configuration is malformed")
        state = hooks.get("state", {})
        if not isinstance(state, dict):
            raise CodexWorkspaceDiscoveryError("Codex hook state is malformed")

        global_key = _canonical_path_identity(self.global_hooks_path, strict=False)
        targets: dict[str, WorkspaceHookTarget] = {}
        for registration, metadata in state.items():
            parsed = _parse_state_registration(registration)
            if parsed is None:
                continue
            if not isinstance(metadata, dict):
                raise CodexWorkspaceDiscoveryError(
                    "Codex workspace hook state metadata is malformed"
                )
            if metadata.get("enabled") is False:
                continue
            source, _event = parsed
            target = _validated_existing_target(source)
            if target is None:
                continue
            key = _canonical_path_identity(target.hooks_path, strict=True)
            if key == global_key:
                continue
            targets[key] = target
        return tuple(targets[key] for key in sorted(targets))
