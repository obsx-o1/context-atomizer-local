from __future__ import annotations

import hashlib
import json
import http.cookiejar
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_support import PACKAGE_ROOT, SOURCE_ROOT, TemporaryDatabaseTest, chat_event

from atomizer_local_client.chat.ingestion import ingest_chat_event
from atomizer_local_client.library.document_reader import list_elected_sources
from atomizer_local_client.library.document_registry import authorize_directory
from atomizer_local_client.local_auth.contracts import (
    PAIRING_DOMAIN,
    PROTOCOL_VERSION,
    capture_request_material,
    sign_hex,
)
from atomizer_local_client.runtime.codex_integration import (
    hook_command,
    install_codex_hooks,
    remove_codex_hooks,
)
from atomizer_local_client.runtime.configuration import (
    RuntimeConfig,
    RuntimePaths,
    read_state,
    write_json,
)
from atomizer_local_client.platforms.credentials import current_credential_store
from atomizer_local_client.runtime.credentials import CredentialStore
from atomizer_local_client.runtime.lifecycle import LifecycleManager, RuntimeProcessLauncher
from atomizer_local_client.runtime.permissions import PermissionStore
from atomizer_local_client.runtime_health import RuntimeIdentity
from atomizer_local_client.runtime.logging_setup import (
    close_runtime_logging,
    configure_runtime_logging,
)
from atomizer_local_client.runtime.windows_startup import (
    RUN_KEY,
    VALUE_NAME,
    WindowsRunRegistration,
    runtime_startup_command,
)


class FakeStartup:
    def __init__(self) -> None:
        self.command: str | None = None

    def install(self, command: str) -> None:
        self.command = command

    def read(self) -> str | None:
        return self.command

    def remove(self) -> None:
        self.command = None


class ManagedChildLauncher(RuntimeProcessLauncher):
    def __init__(self) -> None:
        self.processes: list[subprocess.Popen[bytes]] = []
        self.creationflags: list[int] = []
        self.runtime_identity_root: Path | None = None

    def launch(self, command: list[str]) -> subprocess.Popen[bytes]:
        config_path = Path(command[-1])
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(SOURCE_ROOT)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys; from pathlib import Path; "
                    "from atomizer_local_client.runtime.application import AtomizerLocalRuntime; "
                    "from atomizer_local_client.runtime.configuration import RuntimeConfig, RuntimePaths; "
                    "from atomizer_local_client.runtime_health import RuntimeIdentity; "
                    "p=Path(sys.argv[1]); identity=RuntimeIdentity(Path(sys.argv[2])) if sys.argv[2] else None; "
                    "r=AtomizerLocalRuntime(RuntimePaths.for_root(p.parent), RuntimeConfig.load(p), "
                    "runtime_identity=identity, _test_bridge_port=0); "
                    "r.start(); r.wait(); r.stop()"
                ),
                str(config_path),
                str(self.runtime_identity_root) if self.runtime_identity_root else "",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=PACKAGE_ROOT,
            env=environment,
            close_fds=True,
            creationflags=flags,
        )
        self.processes.append(process)
        self.creationflags.append(flags)
        return process

    def cleanup(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


class FakeRegistryKey:
    def __init__(self, registry: "FakeRegistry") -> None:
        self.registry = registry

    def __enter__(self) -> "FakeRegistryKey":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def CreateKey(self, root: object, path: str) -> FakeRegistryKey:
        assert root is self.HKEY_CURRENT_USER
        assert path == RUN_KEY
        return FakeRegistryKey(self)

    def OpenKey(self, root: object, path: str, *args: object) -> FakeRegistryKey:
        del args
        assert root is self.HKEY_CURRENT_USER
        assert path == RUN_KEY
        if VALUE_NAME not in self.values:
            raise FileNotFoundError
        return FakeRegistryKey(self)

    def SetValueEx(
        self, key: FakeRegistryKey, name: str, reserved: int, kind: int, value: str
    ) -> None:
        del key, reserved
        assert kind == self.REG_SZ
        self.values[name] = value

    def QueryValueEx(self, key: FakeRegistryKey, name: str) -> tuple[str, int]:
        del key
        return self.values[name], self.REG_SZ

    def DeleteValue(self, key: FakeRegistryKey, name: str) -> None:
        del key
        if name not in self.values:
            raise FileNotFoundError
        del self.values[name]


class RuntimeProductizationTests(TemporaryDatabaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.paths = RuntimePaths.for_root(
            self.root / "application-data",
            shortcut=self.root / "start-menu" / "Context Atomizer Library.lnk",
        )
        self.startup = FakeStartup()
        self.launcher = ManagedChildLauncher()
        self.runtime_executable = self.root / "installed" / "atomizer-local-runtime.exe"
        self.manager = LifecycleManager(
            self.paths,
            startup=self.startup,
            runtime_command_prefix=[str(self.runtime_executable)],
            process_launcher=self.launcher,
        )

    def tearDown(self) -> None:
        try:
            if read_state(self.paths.state) is not None:
                self.manager.stop(timeout=3)
        except Exception:
            pass
        self.launcher.cleanup()
        super().tearDown()

    def _json_request(
        self,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            url,
            data=data,
            method="POST" if data is not None else "GET",
            headers=headers or {},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def _signed_capture(
        self,
        bridge_port: int,
        extension_secret: str,
        *,
        event_id: str,
    ) -> tuple[int, dict[str, object]]:
        payload = {
            "event_id": event_id,
            "host": "chatgpt_web",
            "host_project_reference": "productized-update-project",
            "host_chat_reference": "productized-update-chat",
            "host_turn_reference": event_id,
            "role": "user",
            "content": "productized update pairing evidence",
            "captured_at": "2026-08-22T12:00:00+00:00",
            "project_display_name": "Productized update",
            "chat_display_name": "Pairing preservation",
        }
        body = json.dumps(payload).encode("utf-8")
        nonce = f"{time.time_ns():032d}"
        timestamp = str(int(time.time()))
        body_sha256 = hashlib.sha256(body).hexdigest()
        signature = sign_hex(
            extension_secret,
            capture_request_material(
                method="POST",
                operation="/v1/chat-events",
                nonce=nonce,
                timestamp=timestamp,
                body_sha256=body_sha256,
            ),
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{bridge_port}/v1/chat-events",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Atomizer-Protocol": PROTOCOL_VERSION,
                "X-Atomizer-Nonce": nonce,
                "X-Atomizer-Timestamp": timestamp,
                "X-Atomizer-Content-SHA256": body_sha256,
                "X-Atomizer-Signature": signature,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def _wait_for_derived_convergence(
        self, *, timeout: float = 20.0, poll_interval: float = 0.05
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while True:
            status = self.manager.status()
            health = status.get("health")
            derived = health.get("derived_state") if isinstance(health, dict) else None
            units_indexed = derived.get("units_indexed") if isinstance(derived, dict) else None
            if (
                isinstance(derived, dict)
                and derived.get("convergence_state") == "converged"
                and derived.get("state") == "idle"
                and derived.get("pending_count") == 0
                and isinstance(units_indexed, int)
                and units_indexed >= 1
                and derived.get("units_failed") == 0
            ):
                return derived
            diagnostics = {
                "running": bool(status.get("running")),
                "health_available": isinstance(health, dict),
                "derived_available": isinstance(derived, dict),
                "convergence_state": (
                    derived.get("convergence_state") if isinstance(derived, dict) else None
                ),
                "state": derived.get("state") if isinstance(derived, dict) else None,
                "pending_count": (
                    derived.get("pending_count") if isinstance(derived, dict) else None
                ),
                "units_indexed": units_indexed,
                "units_failed": (
                    derived.get("units_failed") if isinstance(derived, dict) else None
                ),
            }
            if time.monotonic() >= deadline:
                self.fail(
                    "derived state did not converge before graceful stop: "
                    + json.dumps(diagnostics, sort_keys=True)
                )
            if poll_interval > 0:
                time.sleep(poll_interval)

    def test_fresh_repeated_install_pairing_boundary_restart_update_and_uninstall(self) -> None:
        old_identity_root = self.root / "old-runtime-identity"
        old_identity_root.mkdir()
        (old_identity_root / "old_build.py").write_text(
            "OLD_BUILD = True\n", encoding="utf-8"
        )
        old_identity = RuntimeIdentity(old_identity_root).startup_build_sha256
        current_identity = self.manager.runtime_identity.startup_build_sha256
        self.assertNotEqual(old_identity, current_identity)
        self.launcher.runtime_identity_root = old_identity_root

        self.assertFalse(self.paths.database.exists())
        first = self.manager.install()
        self.assertTrue(first["installed"])
        self.assertTrue(first["startup_registered"])
        self.assertTrue(first["running"])
        self.assertTrue(self.paths.database.is_file())
        self.assertFalse(self.paths.library_shortcut.exists())
        self.assertNotIn("Bearer", str(self.startup.command))
        self.assertNotIn("token", str(self.startup.command).casefold())
        self.assertTrue(self.startup.command.startswith(str(self.runtime_executable.resolve())))
        if os.name == "nt":
            self.assertTrue(self.launcher.creationflags[-1] & subprocess.CREATE_NO_WINDOW)

        state = read_state(self.paths.state)
        self.assertIsNotNone(state)
        self.assertEqual(state["runtime_build"], old_identity)
        initial_status = self.manager.status()
        self.assertTrue(initial_status["update_required"])
        self.assertEqual(
            initial_status["health"]["runtime"]["startup_build_sha256"], old_identity
        )
        self.assertFalse(initial_status["health"]["runtime"]["restart_required"])
        bridge_port = int(state["bridge_port"])
        library_port = int(state["library_port"])
        bootstrap_status, bootstrap = self._json_request(
            f"http://127.0.0.1:{bridge_port}/v1/bootstrap",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(bootstrap_status, 404)
        self.assertEqual(bootstrap, {"ok": False})
        management_token = current_credential_store(self.paths.credential).load()
        wrong_status, _ = self._json_request(
            f"http://127.0.0.1:{bridge_port}/v1/library/launch",
            data=b"{}",
            headers={
                "Authorization": "Bearer extension-secret-cannot-manage",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(wrong_status, 401)
        launch_status, launch = self._json_request(
            f"http://127.0.0.1:{bridge_port}/v1/library/launch",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {management_token}",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(launch_status, 200)
        self.assertNotIn(management_token, json.dumps(launch))
        _, health = self._json_request(f"http://127.0.0.1:{library_port}/health")
        self.assertEqual(set(health), {"ok", "service", "runtime_running"})
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        with opener.open(str(launch["url"]), timeout=3):
            pass
        library = f"http://127.0.0.1:{library_port}"
        with opener.open(library + "/status", timeout=3) as response:
            status_html = response.read().decode("utf-8")
        self.assertIn("Atomizer Local status", status_html)
        self.assertNotIn(str(self.paths.app_data), status_html)
        self.assertNotIn(management_token, status_html)

        self.assertFalse(self.paths.extension_credential.exists())
        with opener.open(library + "/permissions", timeout=3) as response:
            permissions_html = response.read().decode("utf-8")
        csrf_match = re.search(
            r"name=['\"]csrf_token['\"] value=['\"]([^'\"]+)['\"]",
            permissions_html,
        )
        self.assertIsNotNone(csrf_match)
        csrf = str(csrf_match.group(1))
        pairing_request = urllib.request.Request(
            library + "/extension/pairing-code",
            data=urllib.parse.urlencode({"csrf_token": csrf}).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": library,
                "Sec-Fetch-Site": "same-origin",
            },
        )
        with opener.open(pairing_request, timeout=3) as response:
            pairing_html = response.read().decode("utf-8")
        pairing_match = re.search(r"<p><code>([^<]+)</code></p>", pairing_html)
        self.assertIsNotNone(pairing_match)
        pair_status, paired = self._json_request(
            f"http://127.0.0.1:{bridge_port}/v1/pair",
            data=json.dumps(
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "pairingDomain": PAIRING_DOMAIN,
                    "pairingCode": pairing_match.group(1),
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(pair_status, 200)
        extension_secret = str(paired["extensionSecret"])
        extension_store = current_credential_store(
            self.paths.extension_credential,
            description="Context Atomizer Local extension pairing secret",
        )
        self.assertEqual(extension_store.load(), extension_secret)
        capture_status, capture = self._signed_capture(
            bridge_port, extension_secret, event_id="productized-before-restart"
        )
        self.assertEqual(capture_status, 200)
        self.assertTrue(capture["ok"])
        management_blob = self.paths.credential.read_bytes()
        extension_blob = self.paths.extension_credential.read_bytes()

        launches = len(self.launcher.processes)
        second = self.manager.install()
        self.assertTrue(second["running"])
        self.assertEqual(len(self.launcher.processes), launches)
        self.assertEqual(
            management_token, current_credential_store(self.paths.credential).load()
        )

        crashed_pid = int(read_state(self.paths.state)["pid"])
        crashed_process = self.launcher.processes[-1]
        crashed_process.terminate()
        crashed_process.wait(timeout=5)
        self.assertIsNotNone(read_state(self.paths.state))
        recovered = self.manager.start()
        self.assertTrue(recovered["running"])
        self.assertNotEqual(int(read_state(self.paths.state)["pid"]), crashed_pid)

        receipt = ingest_chat_event(
            self.paths.database,
            chat_event(event_id="productized-project", content="productized runtime seed"),
        )
        source_root = self.root / "authorized"
        source_root.mkdir()
        (source_root / "PERSIST.md").write_text("productized persistence term", encoding="utf-8")
        authorize_directory(self.paths.database, receipt.project_id, source_root)
        original_source_id = str(list_elected_sources(self.paths.database)[0]["source_id"])

        before_restart_pid = int(read_state(self.paths.state)["pid"])
        restarted = self.manager.restart()
        after_restart_pid = int(read_state(self.paths.state)["pid"])
        self.assertTrue(restarted["running"])
        self.assertNotEqual(before_restart_pid, after_restart_pid)
        self.assertEqual(self.paths.credential.read_bytes(), management_blob)
        self.assertEqual(self.paths.extension_credential.read_bytes(), extension_blob)
        restarted_state = read_state(self.paths.state)
        self.assertEqual(restarted_state["runtime_build"], old_identity)
        restarted_status = self.manager.status()
        self.assertTrue(restarted_status["update_required"])
        self.assertFalse(restarted_status["health"]["runtime"]["restart_required"])
        restart_capture_status, _ = self._signed_capture(
            int(restarted_state["bridge_port"]),
            extension_secret,
            event_id="productized-after-restart",
        )
        self.assertEqual(restart_capture_status, 200)

        before_update_pid = int(restarted_state["pid"])
        self.launcher.runtime_identity_root = None
        updated = self.manager.update()
        after_update = read_state(self.paths.state)
        self.assertTrue(updated["running"])
        self.assertNotEqual(before_update_pid, int(after_update["pid"]))
        self.assertEqual(updated["runtime_build"], current_identity)
        self.assertEqual(after_update["runtime_build"], current_identity)
        updated_status = self.manager.status()
        self.assertFalse(updated_status["update_required"])
        self.assertEqual(
            updated_status["health"]["runtime"]["startup_build_sha256"],
            current_identity,
        )
        self.assertEqual(
            updated_status["health"]["runtime"]["current_build_sha256"],
            current_identity,
        )
        self.assertFalse(updated_status["health"]["runtime"]["restart_required"])
        self.assertEqual(self.paths.credential.read_bytes(), management_blob)
        self.assertEqual(self.paths.extension_credential.read_bytes(), extension_blob)
        update_capture_status, _ = self._signed_capture(
            int(after_update["bridge_port"]),
            extension_secret,
            event_id="productized-after-update",
        )
        self.assertEqual(update_capture_status, 200)
        self.assertEqual(
            str(list_elected_sources(self.paths.database)[0]["source_id"]), original_source_id
        )

        old_token = current_credential_store(self.paths.credential).load()
        rotated = self.manager.rotate_credential()
        self.assertTrue(rotated["running"])
        rotated_state = read_state(self.paths.state)
        new_token = current_credential_store(self.paths.credential).load()
        self.assertNotEqual(new_token, old_token)
        old_status, _ = self._json_request(
            f"http://127.0.0.1:{rotated_state['bridge_port']}/v1/management/status",
            headers={
                "Authorization": f"Bearer {old_token}",
            },
        )
        self.assertEqual(old_status, 401)
        new_status, _ = self._json_request(
            f"http://127.0.0.1:{rotated_state['bridge_port']}/v1/management/status",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        self.assertEqual(new_status, 200)
        self.assertEqual(self.paths.extension_credential.read_bytes(), extension_blob)

        self.manager.stop()
        database_bytes = self.paths.database.read_bytes()
        removed = self.manager.uninstall()
        self.assertTrue(removed["uninstalled"])
        self.assertFalse(removed["startup_registered"])
        self.assertTrue(removed["database_preserved"])
        self.assertEqual(self.paths.database.read_bytes(), database_bytes)
        self.assertFalse(self.paths.config.exists())
        self.assertFalse(self.paths.credential.exists())
        self.assertFalse(self.paths.extension_credential.exists())
        self.assertFalse(self.paths.library_shortcut.exists())

    def test_derived_convergence_poll_tolerates_transient_missing_health(self) -> None:
        converged = {
            "convergence_state": "converged",
            "state": "idle",
            "pending_count": 0,
            "units_indexed": 1,
            "units_failed": 0,
        }
        statuses = (
            {"running": False, "health": None},
            {"running": False, "health": None},
            {"running": True, "health": {"derived_state": converged}},
        )
        with patch.object(self.manager, "status", side_effect=statuses) as status:
            result = self._wait_for_derived_convergence(timeout=1.0, poll_interval=0)

        self.assertEqual(result, converged)
        self.assertEqual(status.call_count, 3)

    def test_derived_convergence_poll_fails_if_health_never_recovers(self) -> None:
        with patch.object(
            self.manager, "status", return_value={"running": False, "health": None}
        ):
            with self.assertRaisesRegex(AssertionError, '"health_available": false'):
                self._wait_for_derived_convergence(timeout=0, poll_interval=0)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows file sharing semantics")
    def test_graceful_stop_survives_transient_state_reader_after_derived_convergence(self) -> None:
        installed = self.manager.install()
        self.assertTrue(installed["running"])
        state = read_state(self.paths.state)
        self.assertIsNotNone(state)
        receipt = ingest_chat_event(
            self.paths.database,
            chat_event(event_id="graceful-stop", content="synthetic graceful shutdown seed"),
        )
        self.assertTrue(receipt.message_id)
        self._wait_for_derived_convergence()

        state_reader = self.paths.state.open("rb")

        def release_reader() -> None:
            time.sleep(2.0)
            state_reader.close()

        release = threading.Thread(target=release_reader, name="release-runtime-state-reader")
        release.start()
        try:
            self.assertTrue(self.manager.stop(timeout=7))
        finally:
            release.join(timeout=3)
            state_reader.close()
        process = self.launcher.processes[-1]
        process.wait(timeout=5)
        self.assertIsNotNone(process.returncode)
        self.assertIsNone(read_state(self.paths.state))
        for port in (int(state["bridge_port"]), int(state["library_port"])):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(0.25)
                self.assertNotEqual(client.connect_ex(("127.0.0.1", port)), 0)

    def test_stop_rejects_runtime_that_accepts_request_but_does_not_exit(self) -> None:
        write_json(self.paths.state, {"bridge_port": 43117, "pid": 12345})

        class Credential:
            def load(self) -> str:
                return "x" * 48

        self.manager.credential_store = Credential()
        response = MagicMock()
        response.__enter__.return_value.status = 202
        with (
            patch(
                "atomizer_local_client.runtime.lifecycle.urllib.request.urlopen",
                return_value=response,
            ),
            patch(
                "atomizer_local_client.runtime.lifecycle._process_is_running",
                return_value=True,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "runtime did not stop before timeout"):
                self.manager.stop(timeout=0.06)
        self.assertIsNotNone(read_state(self.paths.state))

    def test_install_permission_choices_persist_and_repeated_install_does_not_reset_them(self) -> None:
        first = self.manager.install(start=False, chatgpt_enabled=True)
        self.assertEqual(
            first["permissions"], {"chatgpt_web": True, "codex": False}
        )
        hooks = self.root / "workspace" / ".codex" / "hooks.json"
        executable = self.root / "atomizer-codex-hook.exe"
        self.manager.install(
            start=False,
            codex_hooks=hooks,
            codex_hook_executable=executable,
        )
        restarted = PermissionStore(self.paths.permissions)
        self.assertTrue(restarted.is_enabled("chatgpt_web"))
        self.assertTrue(restarted.is_enabled("codex"))
        self.assertTrue(restarted.snapshot()["codex"].installed)
        self.manager.install(start=False)
        preserved = PermissionStore(self.paths.permissions)
        self.assertTrue(preserved.is_enabled("chatgpt_web"))
        self.assertTrue(preserved.is_enabled("codex"))

    def test_bridge_port_squatting_fails_closed_without_fallback(self) -> None:
        from atomizer_local_client.runtime.application import AtomizerLocalRuntime

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        owns_listener = False
        try:
            try:
                listener.bind(("127.0.0.1", 43117))
                listener.listen(1)
                owns_listener = True
            except OSError:
                pass
            runtime = AtomizerLocalRuntime(self.paths, RuntimeConfig())
            with self.assertRaisesRegex(RuntimeError, "43117.*failed closed"):
                runtime.start()
            self.assertIsNone(read_state(self.paths.state))
        finally:
            listener.close()
        if not owns_listener:
            return

        runtime = AtomizerLocalRuntime(self.paths, RuntimeConfig())
        try:
            state = runtime.start()
            self.assertEqual(int(state["bridge_port"]), 43117)
            self.assertIn(int(state["library_port"]), range(43118, 43129))
            self.assertNotEqual(int(state["bridge_port"]), int(state["library_port"]))
        finally:
            runtime.stop()

    @unittest.skipUnless(sys.platform == "win32", "requires Windows DPAPI")
    def test_dpapi_credential_is_stable_rotatable_and_not_plaintext_at_rest(self) -> None:
        store = CredentialStore(self.paths.credential)
        token = store.load_or_create()
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(store.load(), token)
        self.assertNotIn(token.encode("ascii"), self.paths.credential.read_bytes())
        rotated = store.rotate()
        self.assertNotEqual(rotated, token)
        self.assertEqual(store.load(), rotated)

    def test_hkcu_run_registration_is_idempotent_and_removable(self) -> None:
        registry = FakeRegistry()
        registration = WindowsRunRegistration(registry)
        command = runtime_startup_command(
            [str(self.runtime_executable)], self.paths.config
        )
        registration.install(command)
        registration.install(command)
        self.assertEqual(registration.read(), command)
        self.assertEqual(set(registry.values), {VALUE_NAME})
        registration.remove()
        registration.remove()
        self.assertIsNone(registration.read())

    def test_codex_registration_is_explicit_idempotent_and_exactly_removable(self) -> None:
        hooks_path = self.root / "workspace" / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep-me"}]}]}}),
            encoding="utf-8",
        )
        command = hook_command(self.root / "atomizer-codex-hook.exe", self.paths.database)
        self.assertTrue(install_codex_hooks(hooks_path, command))
        self.assertFalse(install_codex_hooks(hooks_path, command))
        payload = json.loads(hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(
            sum(
                hook.get("command") == command
                for entries in payload["hooks"].values()
                for entry in entries
                for hook in entry["hooks"]
            ),
            2,
        )
        self.assertTrue(remove_codex_hooks(hooks_path, command))
        self.assertFalse(remove_codex_hooks(hooks_path, command))
        self.assertIn("keep-me", hooks_path.read_text(encoding="utf-8"))

    def test_codex_duplicate_hooks_reconcile_on_install_update_and_remove(self) -> None:
        hooks_path = self.root / "workspace" / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        current_executable = self.root / "installed" / "atomizer-codex-hook.exe"
        current = hook_command(current_executable, self.paths.database)
        unrelated = {"hooks": [{"type": "command", "command": "keep-me"}]}
        hooks_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [
                            unrelated,
                            {"hooks": [{"type": "command", "command": current}]},
                            {"hooks": [{"type": "command", "command": current}]},
                        ]
                        for event in ("UserPromptSubmit", "Stop")
                    }
                }
            ),
            encoding="utf-8",
        )

        installed = self.manager.install(
            start=False,
            codex_hooks=hooks_path,
            codex_hook_executable=current_executable,
        )
        self.assertTrue(installed["codex_hooks_changed"])
        payload = json.loads(hooks_path.read_text(encoding="utf-8"))
        for event in ("UserPromptSubmit", "Stop"):
            commands = [
                hook["command"]
                for entry in payload["hooks"][event]
                for hook in entry["hooks"]
            ]
            self.assertEqual(commands, ["keep-me", current])

        for event in ("UserPromptSubmit", "Stop"):
            payload["hooks"][event].append(
                {"hooks": [{"type": "command", "command": current}]}
            )
        hooks_path.write_text(json.dumps(payload), encoding="utf-8")
        updated = self.manager.update(
            codex_hooks=hooks_path,
            codex_hook_executable=current_executable,
        )
        self.assertTrue(updated["codex_hooks_changed"])
        payload = json.loads(hooks_path.read_text(encoding="utf-8"))
        for event in ("UserPromptSubmit", "Stop"):
            commands = [
                hook["command"]
                for entry in payload["hooks"][event]
                for hook in entry["hooks"]
            ]
            self.assertEqual(commands, ["keep-me", current])

        for event in ("UserPromptSubmit", "Stop"):
            payload["hooks"][event].append(
                {"hooks": [{"type": "command", "command": current}]}
            )
        hooks_path.write_text(json.dumps(payload), encoding="utf-8")
        removed = self.manager.uninstall(
            codex_hooks=hooks_path,
            codex_hook_executable=current_executable,
        )
        self.assertTrue(removed["codex_hooks_changed"])
        payload = json.loads(hooks_path.read_text(encoding="utf-8"))
        for event in ("UserPromptSubmit", "Stop"):
            self.assertEqual(
                [
                    hook["command"]
                    for entry in payload["hooks"][event]
                    for hook in entry["hooks"]
                ],
                ["keep-me"],
            )

    def test_uninstall_removes_owned_runtime_logs_and_empty_log_directory(self) -> None:
        self.manager.install(start=False)
        ingest_chat_event(
            self.paths.database,
            chat_event(event_id="preserved-on-uninstall", content="preserved"),
        )
        runtime_logs = tuple(
            self.paths.log if index == 0 else Path(str(self.paths.log) + f".{index}")
            for index in range(3)
        )
        self.paths.log.parent.mkdir(parents=True, exist_ok=True)
        for path in runtime_logs:
            path.write_text("owned runtime log", encoding="utf-8")

        receipt = self.manager.uninstall()

        self.assertTrue(receipt["uninstalled"])
        self.assertTrue(self.paths.database.exists())
        for path in runtime_logs:
            self.assertFalse(path.exists(), str(path))
        self.assertFalse(self.paths.log.parent.exists())

    def test_partial_codex_uninstall_preserves_ambiguity_and_cleans_core_state(self) -> None:
        self.manager.install(start=False, chatgpt_enabled=True)
        ingest_chat_event(
            self.paths.database,
            chat_event(event_id="preserved-on-partial-uninstall", content="preserved"),
        )
        current_credential_store(
            self.paths.extension_credential,
            description="Context Atomizer Local extension pairing secret",
        ).rotate()
        self.paths.library_shortcut.parent.mkdir(parents=True, exist_ok=True)
        self.paths.library_shortcut.write_text("fixture", encoding="utf-8")
        capture_log = self.paths.app_data / "capture-errors.log"
        capture_log.write_text("fixture", encoding="utf-8")
        runtime_logs = tuple(
            self.paths.log if index == 0 else Path(str(self.paths.log) + f".{index}")
            for index in range(3)
        )
        self.paths.log.parent.mkdir(parents=True, exist_ok=True)
        for path in runtime_logs:
            path.write_text("owned runtime log", encoding="utf-8")

        global_hooks = self.root / "profile" / ".codex" / "hooks.json"
        workspace_hooks = self.root / "workspace" / ".codex" / "hooks.json"
        for path in (global_hooks, workspace_hooks):
            path.parent.mkdir(parents=True, exist_ok=True)
        executable = self.root / "installed" / "atomizer-codex-hook.exe"
        current = hook_command(executable, self.paths.database)
        ambiguous = hook_command(
            self.root / "other" / "atomizer-codex-hook.exe", self.paths.database
        )
        global_hooks.write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": ambiguous}]}]}}),
            encoding="utf-8",
        )
        before_ambiguous = global_hooks.read_bytes()
        workspace_hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [{"hooks": [{"type": "command", "command": current}]}]
                        for event in ("UserPromptSubmit", "Stop")
                    }
                }
            ),
            encoding="utf-8",
        )
        config = global_hooks.with_name("config.toml")
        config.write_text(
            "\n".join(
                f"[hooks.state.'{workspace_hooks.resolve()}:{event}:0:0']\n"
                'trusted_hash = "sha256:fixture"\n'
                for event in ("user_prompt_submit", "stop")
            ),
            encoding="utf-8",
        )

        receipt = self.manager.uninstall(
            codex_hooks=global_hooks,
            codex_config=config,
            codex_hook_executable=executable,
        )

        self.assertTrue(receipt["uninstalled"])
        self.assertFalse(receipt["codex_cleanup_complete"])
        self.assertTrue(receipt["codex_cleanup_warnings"])
        self.assertEqual(global_hooks.read_bytes(), before_ambiguous)
        self.assertNotIn("atomizer-codex-hook", workspace_hooks.read_text(encoding="utf-8"))
        self.assertIsNone(self.startup.read())
        for path in (
            self.paths.credential,
            self.paths.extension_credential,
            self.paths.config,
            self.paths.permissions,
            self.paths.state,
            self.paths.library_shortcut,
            capture_log,
            *runtime_logs,
        ):
            self.assertFalse(path.exists(), str(path))
        self.assertFalse(self.paths.log.parent.exists())
        self.assertTrue(self.paths.database.exists())

    def test_unrelated_atomizer_text_does_not_poison_uninstall(self) -> None:
        self.manager.install(start=False)
        hooks = self.root / "profile" / ".codex" / "hooks.json"
        hooks.parent.mkdir(parents=True, exist_ok=True)
        hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "unrelated-tool --label atomizer-codex-hook",
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        before = hooks.read_bytes()
        receipt = self.manager.uninstall(
            codex_hooks=hooks,
            codex_hook_executable=self.root / "installed" / "atomizer-codex-hook.exe",
        )
        self.assertTrue(receipt["codex_cleanup_complete"])
        self.assertEqual(hooks.read_bytes(), before)

    def test_install_discovers_multiple_registered_atomizer_workspaces_only(self) -> None:
        global_hooks = self.root / "profile" / ".codex" / "hooks.json"
        first = self.root / "first" / ".codex" / "hooks.json"
        second = self.root / "second" / ".codex" / "hooks.json"
        unrelated = self.root / "unrelated" / ".codex" / "hooks.json"
        for path in (global_hooks, first, second, unrelated):
            path.parent.mkdir(parents=True, exist_ok=True)
        executable = self.root / "installed" / "atomizer-codex-hook.exe"
        current = hook_command(executable, self.paths.database)
        global_hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [{"hooks": [{"type": "command", "command": current}]}]
                        for event in ("UserPromptSubmit", "Stop")
                    }
                }
            ),
            encoding="utf-8",
        )
        first.write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [
                            {"hooks": [{"type": "command", "command": current}]},
                            {"hooks": [{"type": "command", "command": current}]},
                        ]
                        for event in ("UserPromptSubmit", "Stop")
                    }
                }
            ),
            encoding="utf-8",
        )
        second.write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [
                            {"hooks": [{"type": "command", "command": current}]},
                            {"hooks": [{"type": "command", "command": current}]},
                        ]
                        for event in ("UserPromptSubmit", "Stop")
                    }
                }
            ),
            encoding="utf-8",
        )
        unrelated_bytes = json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "keep-me"}]}
                    ]
                }
            }
        ).encode()
        unrelated.write_bytes(unrelated_bytes)
        config = global_hooks.with_name("config.toml")
        registrations = []
        for path in (first, second, unrelated):
            for event in ("user_prompt_submit", "stop"):
                registrations.append(
                    f"[hooks.state.'{path.resolve()}:{event}:0:0']\ntrusted_hash = \"sha256:fixture\"\n"
                )
        config.write_text("\n".join(registrations), encoding="utf-8")

        receipt = self.manager.install(
            start=False,
            codex_hooks=global_hooks,
            codex_config=config,
            codex_hook_executable=executable,
        )
        self.assertEqual(receipt["codex_workspace_targets"], 2)
        self.assertEqual(receipt["codex_hook_changed_paths"], 2)
        for path in (first, second):
            payload = json.loads(path.read_text(encoding="utf-8"))
            commands = [
                hook["command"]
                for event in ("UserPromptSubmit", "Stop")
                for entry in payload["hooks"][event]
                for hook in entry["hooks"]
            ]
            self.assertEqual(commands, [current, current])
        self.assertEqual(unrelated.read_bytes(), unrelated_bytes)

    def test_install_populates_empty_registered_workspace_without_claiming_unrelated(self) -> None:
        global_hooks = self.root / "profile" / ".codex" / "hooks.json"
        empty = self.root / "empty" / ".codex" / "hooks.json"
        unrelated = self.root / "unrelated" / ".codex" / "hooks.json"
        for path in (global_hooks, empty, unrelated):
            path.parent.mkdir(parents=True, exist_ok=True)
        global_hooks.write_text('{"hooks": {}}', encoding="utf-8")
        empty.write_text('{"hooks": {}}', encoding="utf-8")
        unrelated_bytes = b'{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"keep-me"}]}]}}'
        unrelated.write_bytes(unrelated_bytes)
        config = global_hooks.with_name("config.toml")
        config.write_text(
            "\n".join(
                f"[hooks.state.'{path.resolve()}:{event}:0:0']\ntrusted_hash = \"sha256:fixture\"\n"
                for path in (empty, unrelated)
                for event in ("user_prompt_submit", "stop")
            ),
            encoding="utf-8",
        )
        executable = self.root / "installed" / "atomizer-codex-hook.exe"
        current = hook_command(executable, self.paths.database)

        receipt = self.manager.install(
            start=False,
            codex_hooks=global_hooks,
            codex_config=config,
            codex_hook_executable=executable,
        )

        self.assertEqual(receipt["codex_workspace_targets"], 1)
        self.assertEqual(receipt["codex_hook_changed_paths"], 2)
        payload = json.loads(empty.read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for event in ("UserPromptSubmit", "Stop")
            for entry in payload["hooks"][event]
            for hook in entry["hooks"]
        ]
        self.assertEqual(commands, [current, current])
        self.assertEqual(unrelated.read_bytes(), unrelated_bytes)

    def test_install_reconciles_powershell_utf8_bom_workspace_idempotently(self) -> None:
        global_hooks = self.root / "profile" / ".codex" / "hooks.json"
        workspace_hooks = self.root / "workspace" / ".codex" / "hooks.json"
        for path in (global_hooks, workspace_hooks):
            path.parent.mkdir(parents=True, exist_ok=True)
        executable = self.root / "installed" / "atomizer-codex-hook.exe"
        current = hook_command(executable, self.paths.database)
        global_hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [{"hooks": [{"type": "command", "command": current}]}]
                        for event in ("UserPromptSubmit", "Stop")
                    }
                }
            ),
            encoding="utf-8-sig",
        )
        workspace_hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [
                            {"hooks": [{"type": "command", "command": f"keep-{event}"}]},
                            {"hooks": [{"type": "command", "command": current}]},
                            {"hooks": [{"type": "command", "command": current}]},
                        ]
                        for event in ("UserPromptSubmit", "Stop")
                    }
                }
            ),
            encoding="utf-8-sig",
        )
        config = global_hooks.with_name("config.toml")
        config.write_text(
            "\n".join(
                f"[hooks.state.'{workspace_hooks.resolve()}:{event}:0:0']\n"
                'trusted_hash = "sha256:fixture"\n'
                for event in ("user_prompt_submit", "stop")
            ),
            encoding="utf-8-sig",
        )

        first = self.manager.install(
            start=False,
            codex_hooks=global_hooks,
            codex_config=config,
            codex_hook_executable=executable,
        )
        self.assertEqual(first["codex_workspace_targets"], 1)
        self.assertEqual(first["codex_hook_changed_paths"], 1)
        payload = json.loads(workspace_hooks.read_text(encoding="utf-8-sig"))
        for event in ("UserPromptSubmit", "Stop"):
            commands = [
                hook["command"]
                for entry in payload["hooks"][event]
                for hook in entry["hooks"]
            ]
            self.assertEqual(commands, [f"keep-{event}", current])
        first_bytes = {
            path: path.read_bytes() for path in (global_hooks, workspace_hooks, config)
        }

        second = self.manager.install(
            start=False,
            codex_hooks=global_hooks,
            codex_config=config,
            codex_hook_executable=executable,
        )
        self.assertEqual(second["codex_hook_changed_paths"], 0)
        self.assertEqual(
            {path: path.read_bytes() for path in first_bytes},
            first_bytes,
        )
        for path in (*first_bytes, self.paths.app_data):
            self.assertTrue(path.resolve().is_relative_to(self.root.resolve()))

    def test_malformed_workspace_registration_fails_before_hook_writes(self) -> None:
        global_hooks = self.root / "profile" / ".codex" / "hooks.json"
        global_hooks.parent.mkdir(parents=True)
        executable = self.root / "installed" / "atomizer-codex-hook.exe"
        current = hook_command(executable, self.paths.database)
        global_hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        event: [{"hooks": [{"type": "command", "command": current}]}]
                        for event in ("UserPromptSubmit", "Stop")
                    }
                }
            ),
            encoding="utf-8",
        )
        before = global_hooks.read_bytes()
        config = global_hooks.with_name("config.toml")
        config.write_text("[hooks.state\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.manager.install(
                start=False,
                codex_hooks=global_hooks,
                codex_config=config,
                codex_hook_executable=executable,
            )
        self.assertEqual(global_hooks.read_bytes(), before)

    def test_logs_rotate_with_bounded_content_free_records(self) -> None:
        logger = configure_runtime_logging(self.paths.log, max_bytes=256, backup_count=2)
        secret = "do-not-log-this-secret"
        for index in range(100):
            logger.info("controlled_startup_record index=%d", index)
        for handler in logger.handlers:
            handler.flush()
        files = list(self.paths.log.parent.glob("runtime.log*"))
        self.assertLessEqual(len(files), 3)
        combined = b"".join(path.read_bytes() for path in files)
        self.assertNotIn(secret.encode("utf-8"), combined)
        self.assertNotIn(str(self.root).encode("utf-8"), combined)
        close_runtime_logging(logger)

    def test_package_declares_windowless_runtime_and_open_library_launchers(self) -> None:
        pyproject = (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[project.gui-scripts]", pyproject)
        self.assertIn("atomizer-local-runtime", pyproject)
        self.assertIn("atomizer-local-open-library", pyproject)
        self.assertIn("atomizer-local-setup", pyproject)
        self.assertIn("atomizer-local-restart", pyproject)
        self.assertIn("atomizer-local-uninstall", pyproject)
        options_html = (
            PACKAGE_ROOT / "browser_extension" / "browsers" / "shared" / "options.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn("bridge-token", options_html)
        self.assertIn("type=\"password\"", options_html)
        with self.assertRaises(ValueError):
            runtime_startup_command([str(self.root / "python.exe")], self.paths.config)

    def test_runtime_configuration_rejects_unadvertised_port_ranges(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeConfig(bridge_port=50000).validate()
        with self.assertRaises(ValueError):
            RuntimeConfig(port_span=11).validate()


if __name__ == "__main__":
    unittest.main()
