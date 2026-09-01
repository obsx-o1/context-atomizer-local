from __future__ import annotations

import json
from unittest import TestCase, mock

from test_support import PACKAGE_ROOT, TemporaryDatabaseTest

from atomizer_local_client.runtime.claude_hook_ownership import (
    ClaudeHookOwnershipConflict,
)
from atomizer_local_client.runtime.claude_integration import (
    install_claude_hooks,
    remove_claude_hooks,
)


class ClaudeHookCompatibilityTests(TemporaryDatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.settings = self.root / ".claude" / "settings.json"
        self.executable = self.root / "bin" / "atomizer-claude-hook.exe"
        self.permissions = self.root / "permissions.json"

    def read(self) -> dict[str, object]:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_install_idempotent_preserves_unrelated_and_uninstall_is_exact(self) -> None:
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps({
            "theme": "dark",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify"}]}]},
        }), encoding="utf-8")
        self.assertTrue(install_claude_hooks(self.settings, self.executable, self.database_path, self.permissions))
        first = self.settings.read_bytes()
        self.assertFalse(install_claude_hooks(self.settings, self.executable, self.database_path, self.permissions))
        self.assertEqual(self.settings.read_bytes(), first)
        self.assertTrue(remove_claude_hooks(self.settings, self.executable, self.database_path, self.permissions))
        value = self.read()
        self.assertEqual(value["theme"], "dark")
        self.assertEqual(value["hooks"]["Stop"][0]["hooks"][0]["command"], "notify")

    def test_ambiguous_entry_fails_closed(self) -> None:
        self.settings.parent.mkdir()
        original = json.dumps({"hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": str(self.root / "other" / "atomizer-claude-hook.exe"),
            "args": [], "timeout": 10,
        }]}]}})
        self.settings.write_text(original, encoding="utf-8")
        with self.assertRaises(ClaudeHookOwnershipConflict):
            install_claude_hooks(self.settings, self.executable, self.database_path, self.permissions)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), original)

    def test_write_failure_leaves_original_file(self) -> None:
        self.settings.parent.mkdir()
        self.settings.write_text('{"unrelated":true}', encoding="utf-8")
        original = self.settings.read_bytes()
        with mock.patch("atomizer_local_client.runtime.claude_integration.os.replace", side_effect=OSError("fail")):
            with self.assertRaises(OSError):
                install_claude_hooks(self.settings, self.executable, self.database_path, self.permissions)
        self.assertEqual(self.settings.read_bytes(), original)

    def test_installer_selection_and_uninstall_ownership_are_independent(self) -> None:
        script = (
            PACKAGE_ROOT / "release" / "windows" / "ContextAtomizer.nsi"
        ).read_text(encoding="utf-8")
        codex = script[
            script.index('Section /o "Enable Codex capture"') :
            script.index("SectionEnd", script.index('Section /o "Enable Codex capture"'))
        ]
        claude = script[
            script.index('Section /o "Enable Claude Code capture"') :
            script.index(
                "SectionEnd", script.index('Section /o "Enable Claude Code capture"')
            )
        ]
        self.assertNotIn("claude", codex.casefold())
        self.assertIn("install --enable-claude --claude-settings", claude)
        self.assertIn('"ClaudeCode" 1', claude)
        self.assertIn(
            'ReadRegDWORD $1 HKCU "Software\\ContextAtomizer\\Installer" "ClaudeCode"',
            script,
        )
        self.assertIn(
            "StrCpy $R2 ' --claude-settings",
            script,
        )
        normal_acceptance = (
            PACKAGE_ROOT / "release" / "test_installer.ps1"
        ).read_text(encoding="utf-8")
        self.assertNotIn("/CLAUDE=1", normal_acceptance)
