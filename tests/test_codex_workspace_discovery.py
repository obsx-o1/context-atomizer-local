from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from atomizer_local_client.runtime import codex_integration
from atomizer_local_client.runtime.codex_hook_ownership import HookOwnershipConflict
from atomizer_local_client.runtime.codex_integration import (
    hook_command,
    install_codex_hooks,
    reconcile_codex_hook_targets,
)
from atomizer_local_client.runtime.codex_workspace import (
    CodexWorkspaceDiscoveryError,
    CodexWorkspaceSource,
)


EVENTS = ("UserPromptSubmit", "Stop")


def _entry(command: str) -> dict[str, object]:
    return {"hooks": [{"type": "command", "command": command}]}


def _pair(*commands: str) -> dict[str, list[dict[str, object]]]:
    return {event: [_entry(command) for command in commands] for event in EVENTS}


def _write_hooks(path: Path, hooks: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")


def _registration(path: Path, event: str, index: int = 0) -> str:
    return f"{path}:{event}:{index}:0"


def _write_codex_config(path: Path, registrations: list[str]) -> None:
    blocks = [
        f"[hooks.state.'{registration}']\ntrusted_hash = \"sha256:fixture\"\n"
        for registration in registrations
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(blocks), encoding="utf-8")


class CodexWorkspaceDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "profile" / ".codex" / "config.toml"
        self.global_hooks = self.root / "profile" / ".codex" / "hooks.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _workspace_hooks(self, name: str) -> Path:
        path = self.root / name / ".codex" / "hooks.json"
        _write_hooks(path, {})
        return path.resolve()

    def test_zero_registered_workspaces_is_empty(self) -> None:
        self.assertEqual(
            CodexWorkspaceSource(
                self.config, global_hooks_path=self.global_hooks
            ).discover(),
            (),
        )

    def test_registered_existing_targets_are_deduplicated_and_sorted(self) -> None:
        first = self._workspace_hooks("first")
        second = self._workspace_hooks("second")
        _write_hooks(self.global_hooks, {})
        _write_codex_config(
            self.config,
            [
                _registration(second, "stop"),
                _registration(first, "user_prompt_submit"),
                _registration(first, "stop"),
                _registration(first, "stop", 1),
                _registration(self.global_hooks.resolve(), "stop"),
                _registration(self.root / "missing" / ".codex" / "hooks.json", "stop"),
            ],
        )
        targets = CodexWorkspaceSource(
            self.config, global_hooks_path=self.global_hooks
        ).discover()
        self.assertEqual(
            {target.hooks_path for target in targets}, {first, second}
        )

    def test_global_hooks_file_is_never_returned_as_a_workspace_target(self) -> None:
        _write_hooks(self.global_hooks, {})
        resolved_global = self.global_hooks.resolve()
        _write_codex_config(
            self.config,
            [
                _registration(resolved_global, "user_prompt_submit"),
                _registration(resolved_global, "stop"),
            ],
        )

        self.assertEqual(
            CodexWorkspaceSource(
                self.config, global_hooks_path=self.global_hooks
            ).discover(),
            (),
        )

    def test_global_exclusion_uses_resolved_identity_not_absolute_spelling(self) -> None:
        _write_hooks(self.global_hooks, {})
        alias_parent = self.global_hooks.parent / "identity-alias"
        alias_parent.mkdir()
        global_alias = alias_parent / ".." / self.global_hooks.name
        _write_codex_config(
            self.config,
            [_registration(self.global_hooks.resolve(), "stop")],
        )

        self.assertNotEqual(
            str(global_alias.absolute()), str(self.global_hooks.resolve())
        )
        self.assertEqual(
            CodexWorkspaceSource(
                self.config, global_hooks_path=global_alias
            ).discover(),
            (),
        )

    @unittest.skipUnless(os.name == "nt", "Windows path equivalence only")
    def test_windows_case_and_slash_equivalent_registrations_deduplicate(self) -> None:
        workspace = self._workspace_hooks("MixedCaseWorkspace")
        slash_variant = str(workspace).replace("\\", "/")
        case_variant = str(workspace).swapcase()
        _write_codex_config(
            self.config,
            [
                _registration(workspace, "user_prompt_submit"),
                f"{slash_variant}:stop:0:0",
                f"{case_variant}:stop:1:0",
            ],
        )

        targets = CodexWorkspaceSource(
            self.config, global_hooks_path=self.global_hooks
        ).discover()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].hooks_path, workspace)

    @unittest.skipUnless(os.name == "nt", "Windows path equivalence only")
    def test_windows_short_and_long_global_identity_when_exposed(self) -> None:
        _write_hooks(self.global_hooks, {})
        absolute_global = self.global_hooks.absolute()
        resolved_global = self.global_hooks.resolve()
        if os.path.normcase(str(absolute_global)) == os.path.normcase(
            str(resolved_global)
        ):
            self.skipTest("temporary root does not expose distinct short/long paths")
        _write_codex_config(
            self.config,
            [_registration(resolved_global, "stop")],
        )

        self.assertEqual(
            CodexWorkspaceSource(
                self.config, global_hooks_path=absolute_global
            ).discover(),
            (),
        )

    def test_disabled_and_unsupported_state_entries_are_ignored(self) -> None:
        workspace = self._workspace_hooks("workspace")
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(
            f"""
[hooks.state.'{_registration(workspace, "stop")}']
enabled = false
[hooks.state.'{workspace}:pre_tool_use:0:0']
trusted_hash = "sha256:fixture"
""",
            encoding="utf-8",
        )
        self.assertEqual(
            CodexWorkspaceSource(
                self.config, global_hooks_path=self.global_hooks
            ).discover(),
            (),
        )

    def test_malformed_config_and_relative_target_fail_closed(self) -> None:
        self.config.parent.mkdir(parents=True)
        self.config.write_text("[hooks.state\n", encoding="utf-8")
        with self.assertRaises(CodexWorkspaceDiscoveryError):
            CodexWorkspaceSource(
                self.config, global_hooks_path=self.global_hooks
            ).discover()
        _write_codex_config(
            self.config, ["relative\\.codex\\hooks.json:stop:0:0"]
        )
        with self.assertRaises(CodexWorkspaceDiscoveryError):
            CodexWorkspaceSource(
                self.config, global_hooks_path=self.global_hooks
            ).discover()

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_link_escape_is_rejected_when_supported(self) -> None:
        outside = self._workspace_hooks("outside")
        link = self.root / "linked" / ".codex" / "hooks.json"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")
        _write_codex_config(self.config, [_registration(link, "stop")])
        with self.assertRaises(CodexWorkspaceDiscoveryError):
            CodexWorkspaceSource(
                self.config, global_hooks_path=self.global_hooks
            ).discover()


class CodexWorkspaceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "data" / "history.sqlite3"
        self.current = hook_command(
            self.root / "installed" / "atomizer-codex-hook.exe", self.database
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_target_set_reconciliation_collapses_current_duplicates(self) -> None:
        global_hooks = self.root / "profile" / ".codex" / "hooks.json"
        workspace_hooks = self.root / "workspace" / ".codex" / "hooks.json"
        config = global_hooks.with_name("config.toml")
        _write_hooks(global_hooks, _pair(self.current))
        _write_hooks(workspace_hooks, _pair(self.current, self.current))
        _write_codex_config(
            config,
            [
                _registration(workspace_hooks.resolve(), "user_prompt_submit"),
                _registration(workspace_hooks.resolve(), "stop"),
            ],
        )

        install_codex_hooks(global_hooks, self.current)
        targets = CodexWorkspaceSource(
            config, global_hooks_path=global_hooks
        ).discover()
        receipt = reconcile_codex_hook_targets(
            (global_hooks, *(target.hooks_path for target in targets)),
            self.current,
            enabled=True,
        )
        self.assertEqual(receipt.target_count, 2)
        payload = json.loads(workspace_hooks.read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for event in EVENTS
            for entry in payload["hooks"][event]
            for hook in entry["hooks"]
        ]
        self.assertEqual(commands, [self.current, self.current])

    def test_ambiguous_workspace_preflight_prevents_partial_global_write(self) -> None:
        global_hooks = self.root / "profile" / ".codex" / "hooks.json"
        workspace_hooks = self.root / "workspace" / ".codex" / "hooks.json"
        _write_hooks(global_hooks, _pair(self.current))
        ambiguous = hook_command(
            self.root / "custom" / "atomizer-codex-hook.exe", self.database
        )
        _write_hooks(workspace_hooks, _pair(ambiguous))
        global_before = global_hooks.read_bytes()
        workspace_before = workspace_hooks.read_bytes()
        with self.assertRaises(HookOwnershipConflict):
            reconcile_codex_hook_targets(
                (global_hooks, workspace_hooks), self.current, enabled=True
            )
        self.assertEqual(global_hooks.read_bytes(), global_before)
        self.assertEqual(workspace_hooks.read_bytes(), workspace_before)

    def test_write_failure_rolls_back_every_previously_changed_target(self) -> None:
        first = self.root / "first" / ".codex" / "hooks.json"
        second = self.root / "second" / ".codex" / "hooks.json"
        _write_hooks(first, _pair("keep-first"))
        _write_hooks(second, _pair("keep-second"))
        before = {first: first.read_bytes(), second: second.read_bytes()}
        real_save = codex_integration._save
        calls = 0

        def controlled_save(path: Path, payload: dict[str, object]) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("controlled second-target failure")
            real_save(path, payload)

        with mock.patch.object(codex_integration, "_save", side_effect=controlled_save):
            with self.assertRaises(OSError):
                reconcile_codex_hook_targets(
                    (first, second), self.current, enabled=True
                )
        self.assertEqual(first.read_bytes(), before[first])
        self.assertEqual(second.read_bytes(), before[second])


if __name__ == "__main__":
    unittest.main()
