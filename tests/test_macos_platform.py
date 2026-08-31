from __future__ import annotations

import ctypes
import importlib.util
import io
import json
import os
import plistlib
import signal
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_support import PACKAGE_ROOT

from atomizer_local_client.platforms.macos.browser import open_url
from atomizer_local_client.platforms.macos.keychain import (
    MacOSKeychainCredentialStore,
    SecurityFrameworkKeychain,
)
from atomizer_local_client.platforms.macos.launch_agent import (
    LABEL,
    MacOSLaunchAgentRegistration,
    runtime_startup_command,
)
from atomizer_local_client.platforms.macos.paths import current_user_locations
from atomizer_local_client.platforms.macos.permissions import ensure_private_directory
from atomizer_local_client.runtime.codex_integration import hook_command
from atomizer_local_client.runtime.configuration import RuntimePaths, write_json
from atomizer_local_client.runtime.credentials import CredentialStore


def _release_module(name: str):
    path = PACKAGE_ROOT / "release" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"macos_test_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    release = str(PACKAGE_ROOT / "release")
    with mock.patch.object(sys, "path", [release, *sys.path]):
        spec.loader.exec_module(module)
    return module


class FakeKeychainBackend:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], bytes] = {}

    def load(self, service: str, account: str) -> bytes:
        try:
            return self.items[(service, account)]
        except KeyError as exc:
            raise FileNotFoundError(account) from exc

    def store(
        self,
        service: str,
        account: str,
        payload: bytes,
        trusted_executables: tuple[Path, ...],
    ) -> None:
        self.last_trusted_executables = trusted_executables
        self.items[(service, account)] = payload

    def remove(self, service: str, account: str) -> None:
        self.items.pop((service, account), None)


class MacOSPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_paths_are_current_user_scoped_and_never_system_scoped(self) -> None:
        locations = current_user_locations(home=self.root / "synthetic-home")
        self.assertEqual(
            locations.app_data,
            (self.root / "synthetic-home" / "Library" / "Application Support" / "Context Atomizer").resolve(),
        )
        self.assertEqual(
            locations.launch_agent.parent,
            (self.root / "synthetic-home" / "Library" / "LaunchAgents").resolve(),
        )
        self.assertNotIn("LaunchDaemons", str(locations.launch_agent))

    def test_runtime_paths_dispatches_to_macos_without_windows_environment(self) -> None:
        synthetic_home = self.root / "synthetic-home"
        with mock.patch(
            "atomizer_local_client.runtime.configuration.sys.platform", "darwin"
        ), mock.patch(
            "atomizer_local_client.platforms.macos.paths.Path.home",
            return_value=synthetic_home,
        ), mock.patch.dict(os.environ, {}, clear=True):
            paths = RuntimePaths.current_user()
        self.assertEqual(
            paths.app_data,
            (
                synthetic_home
                / "Library"
                / "Application Support"
                / "Context Atomizer"
            ).resolve(),
        )

    @unittest.skipIf(os.name == "nt", "POSIX modes are not enforced by Windows")
    def test_runtime_directory_is_user_private(self) -> None:
        directory = self.root / "Application Support" / "Context Atomizer"
        directory.mkdir(parents=True, mode=0o755)
        ensure_private_directory(directory)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    @unittest.skipIf(os.name == "nt", "POSIX modes are not enforced by Windows")
    def test_macos_runtime_json_is_user_private(self) -> None:
        path = self.root / "Application Support" / "runtime.json"
        with mock.patch(
            "atomizer_local_client.runtime.configuration.sys.platform", "darwin"
        ):
            write_json(path, {"content_free": True})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_keychain_store_is_stable_rotatable_separated_and_removable(self) -> None:
        backend = FakeKeychainBackend()
        management = MacOSKeychainCredentialStore(
            self.root / "management-credential.bin", backend=backend
        )
        extension = MacOSKeychainCredentialStore(
            self.root / "extension-pairing.bin",
            description="Context Atomizer Local extension pairing secret",
            backend=backend,
        )
        first = management.load_or_create()
        self.assertEqual(management.load_or_create(), first)
        self.assertNotEqual(extension.load_or_create(), first)
        second = management.rotate()
        self.assertNotEqual(second, first)
        self.assertEqual(management.load(), second)
        self.assertIn(Path(sys.executable).resolve(), backend.last_trusted_executables)
        management.remove()
        with self.assertRaises(FileNotFoundError):
            management.load()

    def test_keychain_acl_names_only_manager_and_runtime_siblings(self) -> None:
        backend = FakeKeychainBackend()
        executable_root = self.root / "runtime"
        executable_root.mkdir()
        manager = executable_root / "atomizer-local-manager"
        runtime = executable_root / "atomizer-local-runtime"
        manager.write_bytes(b"manager")
        runtime.write_bytes(b"runtime")
        with mock.patch(
            "atomizer_local_client.platforms.macos.keychain.sys.executable",
            str(manager),
        ):
            store = MacOSKeychainCredentialStore(
                self.root / "management-credential.bin", backend=backend
            )
        store.rotate()
        self.assertEqual(
            set(backend.last_trusted_executables),
            {manager.resolve(), runtime.resolve()},
        )

    def test_keychain_rotation_preserves_creation_time_access(self) -> None:
        backend = SecurityFrameworkKeychain.__new__(SecurityFrameworkKeychain)
        backend.security = mock.Mock()
        backend.security.SecKeychainItemModifyAttributesAndData.return_value = 0
        backend.core_foundation = mock.Mock()
        item = ctypes.c_void_p(42)
        backend._find = mock.Mock(return_value=(b"old", item))

        with mock.patch.object(
            backend,
            "_access",
            side_effect=AssertionError("rotation attempted to replace the Keychain ACL"),
        ) as access:
            backend.store("service", "account", b"new", (Path(sys.executable),))

        access.assert_not_called()
        call = backend.security.SecKeychainItemModifyAttributesAndData.call_args
        self.assertIs(call.args[0], item)
        self.assertIsNone(call.args[1])
        self.assertEqual(call.args[2], 3)
        self.assertEqual(ctypes.string_at(call.args[3], 3), b"new")
        backend.core_foundation.CFRelease.assert_called_once_with(item)

    def test_keychain_acl_array_retains_trusted_applications(self) -> None:
        backend = SecurityFrameworkKeychain.__new__(SecurityFrameworkKeychain)
        backend.security = mock.Mock()
        backend.core_foundation = mock.Mock()
        backend._cf_type_array_callbacks = ctypes.c_int(0)
        trusted_paths = []

        def create_trusted_application(path, reference) -> int:
            trusted_paths.append(path)
            reference._obj.value = 11
            return 0

        def create_access(description, applications, reference) -> int:
            del description, applications
            reference._obj.value = 44
            return 0

        backend.security.SecTrustedApplicationCreateFromPath.side_effect = (
            create_trusted_application
        )
        backend.security.SecAccessCreate.side_effect = create_access
        backend.core_foundation.CFArrayCreate.return_value = 22
        backend.core_foundation.CFStringCreateWithCString.return_value = 33

        with backend._access("account", (Path(sys.executable),)) as access:
            self.assertEqual(access.value, 44)

        callbacks = backend.core_foundation.CFArrayCreate.call_args.args[3]
        self.assertIsNotNone(callbacks)
        self.assertEqual(callbacks._obj.value, 0)
        self.assertEqual(trusted_paths, [None])

    def test_credential_selector_uses_keychain_only_for_macos(self) -> None:
        from atomizer_local_client.platforms.credentials import current_credential_store

        expected = object()
        with mock.patch(
            "atomizer_local_client.platforms.credentials.sys.platform", "darwin"
        ), mock.patch(
            "atomizer_local_client.platforms.macos.keychain.MacOSKeychainCredentialStore",
            return_value=expected,
        ) as keychain:
            selected = current_credential_store(self.root / "credential.bin")
        self.assertIs(selected, expected)
        keychain.assert_called_once()

    def test_launch_agent_install_reinstall_and_remove_are_exact(self) -> None:
        path = self.root / "home" / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        registration = MacOSLaunchAgentRegistration(path)
        executable = self.root / "Context Atomizer" / "atomizer-local-runtime"
        config = self.root / "Application Support" / "runtime.json"
        command = runtime_startup_command([str(executable)], config)

        registration.install(command)
        first = path.read_bytes()
        payload = plistlib.loads(first)
        self.assertEqual(payload["Label"], LABEL)
        self.assertEqual(
            payload["ProgramArguments"],
            [str(executable.resolve()), "--config", str(config.resolve())],
        )
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(registration.read(), command)
        registration.install(command)
        self.assertEqual(path.read_bytes(), first)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        registration.remove()
        self.assertFalse(path.exists())

    def test_launch_agent_ambiguous_ownership_fails_closed(self) -> None:
        path = self.root / "LaunchAgents" / f"{LABEL}.plist"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            plistlib.dumps(
                {
                    "Label": LABEL,
                    "ProgramArguments": ["/usr/bin/true"],
                    "RunAtLoad": True,
                }
            )
        )
        before = path.read_bytes()
        registration = MacOSLaunchAgentRegistration(path)
        command = runtime_startup_command(
            [
                str(self.root / "python3"),
                "-m",
                "atomizer_local_client.runtime.application",
            ],
            self.root / "runtime.json",
        )
        with self.assertRaises(RuntimeError):
            registration.install(command)
        with self.assertRaises(RuntimeError):
            registration.remove()
        self.assertEqual(path.read_bytes(), before)

    def test_macos_startup_command_rejects_unowned_launchers(self) -> None:
        config = self.root / "runtime.json"
        with self.assertRaises(ValueError):
            runtime_startup_command([str(self.root / "unrelated")], config)
        with self.assertRaises(ValueError):
            MacOSLaunchAgentRegistration(self.root / "LaunchAgents" / "owned.plist").install(
                "/usr/bin/true"
            )
        python = self.root / "python3"
        command = runtime_startup_command(
            [str(python), "-m", "atomizer_local_client.runtime.application"], config
        )
        self.assertIn("atomizer_local_client.runtime.application", command)

    def test_native_open_uses_argument_vector_without_a_shell(self) -> None:
        completed = mock.Mock(returncode=0)
        with mock.patch(
            "atomizer_local_client.platforms.macos.browser.subprocess.run",
            return_value=completed,
        ) as run:
            self.assertTrue(open_url("http://127.0.0.1:43118/?launch=fixture"))
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/open", "http://127.0.0.1:43118/?launch=fixture"],
        )
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_macos_artifact_is_thin_unsigned_and_user_level(self) -> None:
        builder = _release_module("build_macos")
        runtime = self.root / "runtime"
        output = self.root / "artifacts"
        runtime.mkdir()
        header = struct.pack("<II", builder.MACHO_64_MAGIC, builder.CPU_TYPES["arm64"])
        for name in builder.EXECUTABLES:
            (runtime / name).write_bytes(header + b"synthetic-mach-o")
        (runtime / "runtime-build-identity.json").write_text(
            '{"schema_version": 1, "runtime_build_fingerprint": "fixture"}\n',
            encoding="utf-8",
        )
        shutil.copytree(
            PACKAGE_ROOT / "src" / "atomizer_local_client" / "portable_plugin",
            runtime / "portable_plugin",
        )
        with mock.patch.object(builder.platform, "system", return_value="Darwin"), mock.patch.object(
            builder.platform, "machine", return_value="arm64"
        ):
            artifact = builder.build_macos_artifact(
                PACKAGE_ROOT, runtime, output, "arm64"
            )
        with tarfile.open(artifact, "r:gz") as archive:
            names = set(archive.getnames())
            metadata = json.load(
                io.TextIOWrapper(
                    archive.extractfile(
                        "ContextAtomizerLocal/artifact-metadata.json"
                    ),
                    encoding="utf-8",
                )
            )
            install = archive.extractfile("ContextAtomizerLocal/install.sh").read().decode()
        self.assertEqual(metadata["architecture"], "arm64")
        self.assertFalse(metadata["signed"])
        self.assertFalse(metadata["notarized"])
        self.assertEqual(metadata["support"], "experimental")
        self.assertEqual(
            metadata["validation"], "native-ci-required-before-distribution"
        )
        self.assertIn("ContextAtomizerLocal/bin/atomizer-local-runtime", names)
        self.assertIn("ContextAtomizerLocal/bin/atomizer-claude-hook", names)
        self.assertIn("ContextAtomizerLocal/bin/atomizer-local-mcp", names)
        self.assertIn("ContextAtomizerLocal/portable_plugin/plugin.json", names)
        self.assertNotIn("sudo", install)
        self.assertNotIn("LaunchDaemons", install)

    def test_macos_artifact_rejects_cross_compile_and_universal_input(self) -> None:
        builder = _release_module("build_macos")
        runtime = self.root / "runtime"
        runtime.mkdir()
        for name in builder.EXECUTABLES:
            (runtime / name).write_bytes(b"\xca\xfe\xba\xbe" + b"\0" * 4)
        (runtime / "runtime-build-identity.json").write_text("{}", encoding="utf-8")
        shutil.copytree(
            PACKAGE_ROOT / "src" / "atomizer_local_client" / "portable_plugin",
            runtime / "portable_plugin",
        )
        with mock.patch.object(builder.platform, "system", return_value="Darwin"), mock.patch.object(
            builder.platform, "machine", return_value="arm64"
        ):
            with self.assertRaises(ValueError):
                builder.build_macos_artifact(
                    PACKAGE_ROOT, runtime, self.root / "output", "arm64"
                )
        with mock.patch.object(builder.platform, "system", return_value="Windows"), mock.patch.object(
            builder.platform, "machine", return_value="AMD64"
        ):
            with self.assertRaises(RuntimeError):
                builder.build_macos_artifact(
                    PACKAGE_ROOT, runtime, self.root / "output", "arm64"
                )


class WindowsCompatibilitySeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_windows_runtime_paths_remain_byte_for_byte_shaped(self) -> None:
        local = self.root / "Local"
        roaming = self.root / "Roaming"
        with mock.patch(
            "atomizer_local_client.runtime.configuration.sys.platform", "win32"
        ), mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)},
            clear=False,
        ):
            paths = RuntimePaths.current_user()
        self.assertEqual(paths.app_data, (local / "ContextAtomizer").resolve())
        self.assertEqual(
            paths.library_shortcut,
            (
                roaming
                / "Microsoft"
                / "Windows"
                / "Start Menu"
                / "Programs"
                / "Context Atomizer Local"
                / "Context Atomizer Library.lnk"
            ).resolve(),
        )

    def test_windows_credential_selection_returns_original_dpapi_store(self) -> None:
        from atomizer_local_client.platforms.credentials import current_credential_store

        with mock.patch(
            "atomizer_local_client.platforms.credentials.sys.platform", "win32"
        ):
            store = current_credential_store(self.root / "credential.bin")
        self.assertIs(type(store), CredentialStore)
        self.assertEqual(store.path, self.root / "credential.bin")
        self.assertEqual(
            store.description, "Context Atomizer Local management credential"
        )

    def test_windows_hook_ownership_keeps_case_and_slash_normalization(self) -> None:
        from atomizer_local_client.runtime.codex_hook_ownership import (
            HookOwnership,
            classify_codex_hook,
        )

        current = (
            '"C:\\Program Files\\Context Atomizer\\atomizer-codex-hook.exe" '
            '--database "C:\\Users\\Synthetic\\AppData\\Local\\ContextAtomizer\\history.sqlite3"'
        )
        candidate = {
            "type": "command",
            "command": (
                '"C:/PROGRAM FILES/CONTEXT ATOMIZER/ATOMIZER-CODEX-HOOK.EXE" '
                '--database "C:/USERS/SYNTHETIC/APPDATA/LOCAL/CONTEXTATOMIZER/HISTORY.SQLITE3"'
            ),
        }
        with mock.patch(
            "atomizer_local_client.runtime.codex_hook_ownership.os.name", "nt"
        ):
            self.assertIs(
                classify_codex_hook("Stop", candidate, current),
                HookOwnership.CURRENT_ATOMIZER,
            )

    def test_windows_hook_command_remains_subprocess_list2cmdline(self) -> None:
        executable = self.root / "Program Files" / "atomizer-codex-hook.exe"
        database = self.root / "Local App Data" / "history.sqlite3"
        expected = subprocess.list2cmdline(
            [str(executable.resolve()), "--database", str(database.resolve())]
        )
        with mock.patch(
            "atomizer_local_client.runtime.codex_integration.os.name", "nt"
        ):
            self.assertEqual(hook_command(executable, database), expected)

    def test_windows_runtime_launcher_discovery_is_unchanged(self) -> None:
        from atomizer_local_client.runtime import lifecycle

        scripts = self.root / "Scripts"
        scripts.mkdir()
        python = scripts / "python.exe"
        pythonw = scripts / "pythonw.exe"
        python.write_bytes(b"")
        pythonw.write_bytes(b"")
        with mock.patch.object(lifecycle.sys, "platform", "win32"), mock.patch.object(
            lifecycle.sys, "executable", str(python)
        ):
            self.assertEqual(
                lifecycle._runtime_command_prefix(),
                [
                    str(pythonw.resolve()),
                    "-m",
                    "atomizer_local_client.runtime.application",
                ],
            )

    def test_macos_claude_hook_discovery_uses_the_native_executable_name(self) -> None:
        from atomizer_local_client.runtime import lifecycle

        scripts = self.root / "runtime"
        scripts.mkdir()
        manager = scripts / "atomizer-local-manager"
        hook = scripts / "atomizer-claude-hook"
        manager.write_bytes(b"")
        hook.write_bytes(b"")
        with mock.patch.object(lifecycle.sys, "platform", "darwin"), mock.patch.object(
            lifecycle.sys, "executable", str(manager)
        ):
            self.assertEqual(lifecycle._claude_hook_executable(), hook.resolve())

    def test_windows_lifecycle_composition_keeps_existing_adapters(self) -> None:
        from atomizer_local_client.runtime import lifecycle

        paths = RuntimePaths.for_root(self.root / "ContextAtomizer")
        startup = mock.Mock()
        credential = mock.Mock()
        prefix = [str(self.root / "atomizer-local-runtime.exe")]
        with mock.patch.object(lifecycle.sys, "platform", "win32"), mock.patch.object(
            lifecycle.RuntimePaths, "current_user", return_value=paths
        ), mock.patch.object(
            lifecycle, "WindowsRunRegistration", return_value=startup
        ) as registration, mock.patch.object(
            lifecycle, "_runtime_command_prefix", return_value=prefix
        ), mock.patch.object(
            lifecycle, "current_credential_store", return_value=credential
        ):
            manager = lifecycle._manager()
        registration.assert_called_once_with()
        self.assertIs(manager.paths, paths)
        self.assertIs(manager.startup, startup)
        self.assertEqual(manager.runtime_command_prefix, prefix)
        self.assertIs(manager.credential_store, credential)
        self.assertIs(
            manager.startup_command_builder, lifecycle.runtime_startup_command
        )
        self.assertIs(manager.url_opener, lifecycle.webbrowser.open)

    def test_macos_lifecycle_probe_detects_preservation(self) -> None:
        lifecycle_probe = _release_module("test_macos_lifecycle")
        database = self.root / "history.sqlite3"
        lifecycle_probe._write_probe(database)
        lifecycle_probe._verify_probe(database)
        connection = sqlite3.connect(database)
        try:
            connection.execute("DELETE FROM macos_lifecycle_probe")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(RuntimeError):
            lifecycle_probe._verify_probe(database)

    def test_windows_runtime_build_keeps_semicolon_and_windowless_flags(self) -> None:
        builder = _release_module("build_runtime")
        output = self.root / "runtime"
        calls: list[list[str]] = []

        def capture(command: list[str], **kwargs: object) -> None:
            del kwargs
            calls.append(command)

        with mock.patch.object(builder.sys, "platform", "win32"), mock.patch.object(
            builder.os, "pathsep", ";"
        ), mock.patch.object(builder.subprocess, "run", side_effect=capture):
            builder.build_runtime(PACKAGE_ROOT, output)
        self.assertEqual(len(calls), 6)
        for command in calls:
            data_values = [
                command[index + 1]
                for index, value in enumerate(command)
                if value == "--add-data"
            ]
            self.assertEqual(len(data_values), 2)
            self.assertTrue(all(";atomizer_local_client" in value for value in data_values))
        self.assertIn("--windowed", calls[0])
        self.assertIn("--console", calls[1])
        self.assertTrue((output / "portable_plugin" / "plugin.json").is_file())


@unittest.skipUnless(sys.platform == "darwin", "requires macOS Keychain Services")
class MacOSNativeKeychainTests(unittest.TestCase):
    def test_isolated_ci_keychain_round_trip(self) -> None:
        isolated = os.environ.get("ATOMIZER_MACOS_KEYCHAIN")
        if not isolated:
            self.skipTest("isolated CI keychain is not configured")
        self.assertTrue(Path(isolated).is_file())
        with tempfile.TemporaryDirectory() as temporary:
            stage_path = Path(temporary) / "keychain-stage.txt"
            stdout_path = Path(temporary) / "keychain-stdout.txt"
            stderr_path = Path(temporary) / "keychain-stderr.txt"
            probe = "\n".join(
                (
                    "import sys",
                    "from pathlib import Path",
                    "from atomizer_local_client.platforms.macos.keychain import (",
                    "    MacOSKeychainCredentialStore,",
                    ")",
                    "stage = Path(sys.argv[2])",
                    "stage.write_text('construct', encoding='utf-8')",
                    "store = MacOSKeychainCredentialStore(Path(sys.argv[1]) / 'credential.bin')",
                    "stage.write_text('load_or_create', encoding='utf-8')",
                    "first = store.load_or_create()",
                    "stage.write_text('load_existing', encoding='utf-8')",
                    "assert store.load() == first",
                    "stage.write_text('rotate', encoding='utf-8')",
                    "second = store.rotate()",
                    "assert first != second",
                    "stage.write_text('load_rotated', encoding='utf-8')",
                    "assert store.load() == second",
                    "stage.write_text('remove', encoding='utf-8')",
                    "store.remove()",
                    "stage.write_text('verify_removed', encoding='utf-8')",
                    "try:",
                    "    store.load()",
                    "except FileNotFoundError:",
                    "    pass",
                    "else:",
                    "    raise AssertionError('removed credential remained readable')",
                    "stage.write_text('complete', encoding='utf-8')",
                )
            )
            with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr:
                process = subprocess.Popen(
                    [sys.executable, "-B", "-c", probe, temporary, stage_path],
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                    text=True,
                )
                try:
                    returncode = process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                    stage = (
                        stage_path.read_text(encoding="utf-8")
                        if stage_path.is_file()
                        else "before-stage-receipt"
                    )
                    print(
                        f"KEYCHAIN_STAGE_TIMEOUT={stage}",
                        file=sys.stderr,
                        flush=True,
                    )
                    self.fail(
                        f"isolated Keychain round trip exceeded 20 seconds at {stage}"
                    )
            self.assertEqual(
                returncode,
                0,
                msg=(
                    f"stage={stage_path.read_text(encoding='utf-8')!r} "
                    f"stdout={stdout_path.read_text(encoding='utf-8')!r} "
                    f"stderr={stderr_path.read_text(encoding='utf-8')!r}"
                ),
            )


if __name__ == "__main__":
    unittest.main()
