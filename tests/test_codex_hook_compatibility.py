from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from atomizer_local_client.runtime.codex_hook_ownership import (
    HookOwnership,
    HookOwnershipConflict,
    classify_codex_hook,
)
from atomizer_local_client.runtime.codex_integration import (
    hook_command,
    install_codex_hooks,
    remove_codex_hooks,
)


EVENTS = ("UserPromptSubmit", "Stop")


def _entry(command: str) -> dict[str, object]:
    return {"hooks": [{"type": "command", "command": command}]}


def _pair(command: str, *, copies: int = 1) -> dict[str, list[dict[str, object]]]:
    return {event: [_entry(command) for _ in range(copies)] for event in EVENTS}


def _commands(payload: dict[str, object]) -> list[str]:
    hooks = payload["hooks"]
    assert isinstance(hooks, dict)
    return [
        str(hook["command"])
        for event in EVENTS
        for entry in hooks.get(event, [])
        if isinstance(entry, dict)
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict) and "command" in hook
    ]


class CodexHookCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "hooks.json"
        self.database = self.root / "Data" / "history.sqlite3"
        self.executable = self.root / "Programs" / "ContextAtomizer" / "atomizer-codex-hook.exe"
        self.current = hook_command(self.executable, self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, hooks: dict[str, object]) -> None:
        self.path.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")

    def test_enable_is_deterministic_deduplicated_and_idempotent(self) -> None:
        unrelated = {"hooks": [{"type": "command", "command": "keep-me"}]}
        for label, hooks in {
            "empty": {},
            "unrelated": {"Stop": [unrelated]},
            "current": _pair(self.current),
            "duplicates": _pair(self.current, copies=3),
        }.items():
            with self.subTest(label=label):
                path = self.root / f"{label}.json"
                path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
                install_codex_hooks(path, self.current)
                first = path.read_bytes()
                commands = _commands(json.loads(first))
                self.assertEqual(commands.count(self.current), 2)
                if label == "unrelated":
                    self.assertIn("keep-me", first.decode("utf-8"))
                self.assertFalse(install_codex_hooks(path, self.current))
                self.assertEqual(path.read_bytes(), first)

    def test_ambiguous_atomizer_like_hook_fails_closed_without_path_disclosure(self) -> None:
        ambiguous = hook_command(self.root / "custom" / "atomizer-codex-hook.exe", self.database)
        self._write({"Stop": [_entry(ambiguous)]})
        before = self.path.read_bytes()
        with self.assertRaises(HookOwnershipConflict) as caught:
            install_codex_hooks(self.path, self.current)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(caught.exception.events, ("Stop",))
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_exact_entrypoint_with_unrecognized_arguments_is_ambiguous(self) -> None:
        command = f'"{self.executable}" --other "{self.database}"'
        self.assertIs(
            classify_codex_hook(
                "Stop", {"type": "command", "command": command}, self.current
            ),
            HookOwnership.AMBIGUOUS,
        )

    def test_unrelated_command_containing_atomizer_text_is_not_a_marker(self) -> None:
        commands = (
            f'"{self.root / "custom" / "run-atomizer-codex-hook-wrapper.exe"}" --database "{self.database}"',
            'unrelated-tool --label atomizer-codex-hook',
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertIs(
                    classify_codex_hook(
                        "Stop", {"type": "command", "command": command}, self.current
                    ),
                    HookOwnership.UNRELATED,
                )

    def test_unrelated_and_unsupported_entries_are_preserved(self) -> None:
        unsupported = _entry("unsupported-event-tool")
        self._write({
            "Stop": [_entry("unrelated-tool --mode safe")],
            "OtherEvent": [unsupported],
        })
        install_codex_hooks(self.path, self.current)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["hooks"]["OtherEvent"], [unsupported])
        self.assertIn(_entry("unrelated-tool --mode safe"), payload["hooks"]["Stop"])

    def test_remove_clears_current_only_and_preserves_unrelated(self) -> None:
        self._write({event: [_entry("keep-me"), _entry(self.current)] for event in EVENTS})
        self.assertTrue(remove_codex_hooks(self.path, self.current))
        self.assertEqual(_commands(json.loads(self.path.read_text(encoding="utf-8"))), ["keep-me", "keep-me"])
        first = self.path.read_bytes()
        self.assertFalse(remove_codex_hooks(self.path, self.current))
        self.assertEqual(self.path.read_bytes(), first)

    @unittest.skipUnless(os.name == "nt", "requires Windows path identity")
    def test_windows_case_slash_and_quoting_normalization_identifies_current(self) -> None:
        executable = str(self.executable.resolve()).upper().replace("\\", "/")
        database = str(self.database.resolve()).upper().replace("\\", "/")
        command = f'"{executable}" --database "{database}"'
        self.assertIs(
            classify_codex_hook("Stop", {"type": "command", "command": command}, self.current),
            HookOwnership.CURRENT_ATOMIZER,
        )

    def test_posix_hook_identity_is_case_sensitive(self) -> None:
        current = (
            "'/Applications/Context Atomizer/atomizer-codex-hook' "
            "--database '/Users/synthetic/Library/Application Support/Context Atomizer/history.sqlite3'"
        )
        exact = {"type": "command", "command": current}
        different_case = {
            "type": "command",
            "command": current.replace("Context Atomizer", "context atomizer"),
        }
        with mock.patch(
            "atomizer_local_client.runtime.codex_hook_ownership.os.name", "posix"
        ):
            self.assertIs(
                classify_codex_hook("Stop", exact, current),
                HookOwnership.CURRENT_ATOMIZER,
            )
            self.assertIs(
                classify_codex_hook("Stop", different_case, current),
                HookOwnership.AMBIGUOUS,
            )

    def test_malformed_config_and_atomic_failure_preserve_original_bytes(self) -> None:
        for label, raw in (
            ("json", b"{not-json"),
            ("event", b'{"hooks":{"Stop":"not-a-list"}}'),
            ("entry", b'{"hooks":{"Stop":[{"hooks":"not-a-list"}]}}'),
        ):
            with self.subTest(label=label):
                path = self.root / f"malformed-{label}.json"
                path.write_bytes(raw)
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    install_codex_hooks(path, self.current)
                self.assertEqual(path.read_bytes(), raw)

        self._write({"Stop": [_entry("keep-me")]})
        before = self.path.read_bytes()
        with mock.patch(
            "atomizer_local_client.runtime.codex_integration.os.replace",
            side_effect=OSError("controlled replacement failure"),
        ):
            with self.assertRaises(OSError):
                install_codex_hooks(self.path, self.current)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
