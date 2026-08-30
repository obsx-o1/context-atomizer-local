from __future__ import annotations

import ast
import importlib.util
import json
import tempfile
from pathlib import Path

from test_support import PACKAGE_ROOT, TemporaryDatabaseTest


class ArchitectureTests(TemporaryDatabaseTest):
    def python_files(self) -> list[Path]:
        return sorted((PACKAGE_ROOT / "src" / "atomizer_local_client").rglob("*.py"))

    def test_host_adapter_has_no_direct_persistence_search_or_peer_import(self) -> None:
        host_root = PACKAGE_ROOT / "src" / "atomizer_local_client" / "hosts" / "codex"
        forbidden = (".history", ".lexical", ".projects", ".chats", ".chatgpt_web")
        for path in host_root.glob("*.py"):
            content = path.read_text(encoding="utf-8")
            self.assertFalse(any(value in content for value in forbidden), str(path))

    def test_browser_core_is_vendor_neutral_and_has_no_persistence_logic(self) -> None:
        core = PACKAGE_ROOT / "browser_extension" / "core"
        forbidden = ("chrome.", "browser.", "sqlite", "fts5")
        for path in core.glob("*.js"):
            content = path.read_text(encoding="utf-8").casefold()
            self.assertFalse(any(value in content for value in forbidden), str(path))

    def test_browser_manifests_are_minimal_and_reuse_shared_core(self) -> None:
        manifests = []
        for browser in ("chromium", "firefox"):
            path = PACKAGE_ROOT / "browser_extension" / "browsers" / browser / "manifest.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifests.append(manifest)
            serialized = json.dumps(manifest)
            self.assertNotIn("<all_urls>", serialized)
            self.assertEqual(manifest["permissions"], ["storage", "scripting"])
            self.assertIn("http://127.0.0.1:43117/*", manifest["host_permissions"])
            self.assertNotIn("http://127.0.0.1/*", manifest["host_permissions"])
            self.assertEqual(
                manifest["content_scripts"][0]["matches"],
                ["https://chatgpt.com/*", "https://chat.openai.com/*"],
            )
            scripts = manifest["content_scripts"][0]["js"]
            self.assertIn("core/capture.js", scripts)
            self.assertIn("browsers/shared/content_script.js", scripts)
        self.assertEqual(
            manifests[0]["content_scripts"][0]["js"],
            manifests[1]["content_scripts"][0]["js"],
        )

    def test_thin_browser_packages_build_from_the_same_core(self) -> None:
        module_path = PACKAGE_ROOT / "browser_extension" / "package_extension.py"
        specification = importlib.util.spec_from_file_location("package_extension", module_path)
        module = importlib.util.module_from_spec(specification)
        assert specification.loader is not None
        specification.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chromium = root / "chromium"
            firefox = root / "firefox"
            module.build_package("chromium", chromium)
            module.build_package("firefox", firefox)
            self.assertEqual(
                (chromium / "core" / "capture.js").read_bytes(),
                (firefox / "core" / "capture.js").read_bytes(),
            )
            self.assertTrue((chromium / "manifest.json").is_file())
            self.assertTrue((firefox / "manifest.json").is_file())

if __name__ == "__main__":
    import unittest

    unittest.main()
