from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from test_support import PACKAGE_ROOT


EXPECTED_CONTENT_SEQUENCE = [
    "core/types.js",
    "core/dedupe.js",
    "core/conversation.js",
    "core/selectors.js",
    "core/chat_titles.js",
    "core/capture.js",
    "browsers/shared/api.js",
    "browsers/shared/content_script.js",
]
EXPECTED_HOST_PERMISSIONS = [
    "https://chatgpt.com/*",
    "https://chat.openai.com/*",
    "http://127.0.0.1:43117/*",
]


class SupportedClientValidationTests(unittest.TestCase):
    def manifest(self, browser: str) -> dict[str, object]:
        path = PACKAGE_ROOT / "browser_extension" / "browsers" / browser / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_chromium_manifest_matches_the_validated_mv3_runtime_contract(self) -> None:
        manifest = self.manifest("chromium")
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["permissions"], ["storage", "scripting"])
        self.assertEqual(manifest["host_permissions"], EXPECTED_HOST_PERMISSIONS)
        self.assertEqual(
            manifest["background"],
            {"service_worker": "browsers/shared/service_worker.js"},
        )
        self.assertEqual(manifest["content_scripts"][0]["js"], EXPECTED_CONTENT_SEQUENCE)

    def test_firefox_manifest_uses_its_mv3_event_page_contract(self) -> None:
        manifest = self.manifest("firefox")
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["permissions"], ["storage", "scripting"])
        self.assertEqual(manifest["host_permissions"], EXPECTED_HOST_PERMISSIONS)
        self.assertEqual(
            manifest["background"],
            {"scripts": [
                "browsers/shared/api.js",
                "browsers/shared/service_worker.js",
            ]},
        )
        self.assertEqual(manifest["content_scripts"][0]["js"], EXPECTED_CONTENT_SEQUENCE)
        self.assertEqual(
            manifest["browser_specific_settings"]["gecko"]["strict_min_version"],
            "121.0",
        )

    def test_shared_shell_is_namespace_and_background_context_compatible(self) -> None:
        shared = PACKAGE_ROOT / "browser_extension" / "browsers" / "shared"
        api = (shared / "api.js").read_text(encoding="utf-8")
        worker = (shared / "service_worker.js").read_text(encoding="utf-8")
        self.assertIn("root.browser || root.chrome", api)
        self.assertNotIn("chrome.", worker)
        self.assertNotIn("browser.", worker)
        self.assertIn('typeof importScripts === "function"', worker)
        self.assertIn('X-Atomizer-Protocol', worker)
        self.assertIn('X-Atomizer-Signature', worker)

    def test_only_declared_client_shells_are_packaged_and_share_identical_assets(self) -> None:
        host_adapters = sorted(
            path.name
            for path in (PACKAGE_ROOT / "src" / "atomizer_local_client" / "hosts").iterdir()
            if path.is_dir() and path.name != "__pycache__"
        )
        browser_shells = sorted(
            path.name
            for path in (PACKAGE_ROOT / "browser_extension" / "browsers").iterdir()
            if path.is_dir() and path.name != "shared"
        )
        self.assertEqual(host_adapters, ["codex"])
        self.assertEqual(browser_shells, ["chromium", "firefox"])

        module_path = PACKAGE_ROOT / "browser_extension" / "package_extension.py"
        specification = importlib.util.spec_from_file_location("package_extension_under_test", module_path)
        module = importlib.util.module_from_spec(specification)
        assert specification.loader is not None
        specification.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chromium = root / "chromium"
            firefox = root / "firefox"
            module.build_package("chromium", chromium)
            module.build_package("firefox", firefox)
            chromium_assets = {
                path.relative_to(chromium).as_posix(): path.read_bytes()
                for path in chromium.rglob("*")
                if path.is_file() and path.name != "manifest.json"
            }
            firefox_assets = {
                path.relative_to(firefox).as_posix(): path.read_bytes()
                for path in firefox.rglob("*")
                if path.is_file() and path.name != "manifest.json"
            }
            self.assertEqual(chromium_assets, firefox_assets)


if __name__ == "__main__":
    unittest.main()
