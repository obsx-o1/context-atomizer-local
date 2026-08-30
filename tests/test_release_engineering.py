from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from test_support import PACKAGE_ROOT

from atomizer_local_client.history.migrations import registered_migration_ids
from atomizer_local_client.history.connection import database
from atomizer_local_client.runtime.codex_hook_ownership import (
    HOOK_OWNERSHIP_CONTRACT_VERSION,
)
from atomizer_local_client.runtime.codex_workspace import (
    WORKSPACE_DISCOVERY_CONTRACT_VERSION,
)
from atomizer_local_client.runtime_health import (
    BUILD_IDENTITY_FILENAME,
    RuntimeIdentity,
    runtime_build_fingerprint,
    runtime_executable_sha256,
    runtime_version,
)


class ReleaseEngineeringTests(unittest.TestCase):
    def _load_release_module(self, filename: str, module_name: str):
        module_path = PACKAGE_ROOT / "release" / filename
        specification = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(specification)
        assert specification.loader is not None
        with mock.patch.object(
            sys, "path", [str(PACKAGE_ROOT / "release"), *sys.path]
        ):
            specification.loader.exec_module(module)
        return module

    def test_installer_acceptance_waits_for_leaf_process_not_persistent_runtime_tree(self) -> None:
        script = (PACKAGE_ROOT / "release" / "test_installer.ps1").read_text(encoding="utf-8")
        common = (PACKAGE_ROOT / "release" / "test_installer_common.ps1").read_text(
            encoding="utf-8"
        )
        helper = (PACKAGE_ROOT / "release" / "windows_process.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("test_installer_common.ps1", script)
        self.assertIn("windows_process.ps1", common)
        self.assertIn("function Invoke-LeafProcess", helper)
        self.assertIn("[Diagnostics.Process]::new()", helper)
        self.assertIn("$process.WaitForExit($timeoutMilliseconds)", helper)
        self.assertNotIn("Start-Process", helper)
        self.assertNotIn("Start-Process -FilePath $Installer -ArgumentList $installArguments -Wait", script)
        installer = script.index("$installExit = Invoke-LeafProcess")
        receipt = script.index("stage = 'installer_completed'", installer)
        exit_check = script.index("if ($installExit -ne 0)", receipt)
        installed_files = script.index("$requiredFiles =", exit_check)
        self.assertLess(installer, receipt)
        self.assertLess(receipt, exit_check)
        self.assertLess(exit_check, installed_files)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows PowerShell 5.1")
    def test_leaf_process_exit_code_is_scalar_with_redirected_diagnostics(self) -> None:
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        command = r"""
$ErrorActionPreference = 'Stop'
. $env:ATOMIZER_TEST_INSTALLER_SCRIPT
$env:RUNNER_TEMP = $env:ATOMIZER_TEST_RUNNER_TEMP
$where = Join-Path $env:SystemRoot 'System32\where.exe'
$success = Invoke-LeafProcess -FilePath $where -ArgumentList 'cmd.exe' -TimeoutSeconds 10 -ReportFailure
$failure = Invoke-LeafProcess -FilePath $where -ArgumentList 'atomizer-definitely-absent.exe' -TimeoutSeconds 10 -ReportFailure
[ordered]@{
    success_type = $success.GetType().FullName
    success_count = @($success).Count
    success_value = [int]$success
    failure_type = $failure.GetType().FullName
    failure_count = @($failure).Count
    failure_value = [int]$failure
} | ConvertTo-Json -Compress
"""
        with tempfile.TemporaryDirectory() as temporary:
            environment = {
                key: value for key, value in os.environ.items() if key.lower() != "path"
            }
            environment["Path"] = os.environ.get("PATH", "")
            environment["ATOMIZER_TEST_INSTALLER_SCRIPT"] = str(
                PACKAGE_ROOT / "release" / "windows_process.ps1"
            )
            environment["ATOMIZER_TEST_RUNNER_TEMP"] = temporary
            result = subprocess.run(
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        payload = json.loads(lines[-1])
        self.assertIn("bounded stderr follows", result.stdout)
        self.assertEqual(payload["success_type"], "System.Int32")
        self.assertEqual(payload["success_count"], 1)
        self.assertEqual(payload["success_value"], 0)
        self.assertEqual(payload["failure_type"], "System.Int32")
        self.assertEqual(payload["failure_count"], 1)
        self.assertNotEqual(payload["failure_value"], 0)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows process trees")
    def test_leaf_process_wait_does_not_follow_detached_background_child(self) -> None:
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        command = r"""
$ErrorActionPreference = 'Stop'
. $env:ATOMIZER_TEST_INSTALLER_SCRIPT
$env:RUNNER_TEMP = $env:ATOMIZER_TEST_RUNNER_TEMP
$arguments = '"{0}" "{1}" "{2}"' -f (
    $env:ATOMIZER_TEST_PARENT_SCRIPT,
    $env:ATOMIZER_TEST_CHILD_SCRIPT,
    $env:ATOMIZER_TEST_CHILD_PID
)
$timer = [Diagnostics.Stopwatch]::StartNew()
$exitCode = Invoke-LeafProcess -FilePath $env:ATOMIZER_TEST_PYTHON -ArgumentList $arguments -TimeoutSeconds 10 -ReportFailure
$timer.Stop()
[ordered]@{
    exit_code = [int]$exitCode
    elapsed_milliseconds = [int64]$timer.ElapsedMilliseconds
} | ConvertTo-Json -Compress
"""
        parent_source = """\
import subprocess
import sys
from pathlib import Path

child = subprocess.Popen(
    [sys.executable, sys.argv[1]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
)
Path(sys.argv[2]).write_text(str(child.pid), encoding="ascii")
"""
        child_source = """\
import time

time.sleep(4)
"""
        child_pid = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_script = root / "parent.py"
            child_script = root / "child.py"
            child_pid_path = root / "child.pid"
            parent_script.write_text(parent_source, encoding="utf-8")
            child_script.write_text(child_source, encoding="utf-8")
            environment = {
                key: value for key, value in os.environ.items() if key.lower() != "path"
            }
            environment["Path"] = os.environ.get("PATH", "")
            environment["ATOMIZER_TEST_INSTALLER_SCRIPT"] = str(
                PACKAGE_ROOT / "release" / "windows_process.ps1"
            )
            environment["ATOMIZER_TEST_RUNNER_TEMP"] = temporary
            environment["ATOMIZER_TEST_PARENT_SCRIPT"] = str(parent_script)
            environment["ATOMIZER_TEST_CHILD_SCRIPT"] = str(child_script)
            environment["ATOMIZER_TEST_CHILD_PID"] = str(child_pid_path)
            environment["ATOMIZER_TEST_PYTHON"] = sys.executable
            started = time.monotonic()
            try:
                result = subprocess.run(
                    [
                        str(powershell),
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        command,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=environment,
                    timeout=12,
                )
                if child_pid_path.is_file():
                    child_pid = int(child_pid_path.read_text(encoding="ascii"))
            finally:
                if child_pid is None and child_pid_path.is_file():
                    child_pid = int(child_pid_path.read_text(encoding="ascii"))
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except OSError:
                        pass
            elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(payload["exit_code"], 0)
        self.assertLess(payload["elapsed_milliseconds"], 2_000)
        self.assertLess(elapsed, 3.0)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows PowerShell 5.1")
    def test_leaf_process_timeout_is_numeric_and_bounded(self) -> None:
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        command = r"""
$ErrorActionPreference = 'Stop'
. $env:ATOMIZER_TEST_PROCESS_SCRIPT
$timer = [Diagnostics.Stopwatch]::StartNew()
$exitCode = Invoke-LeafProcess -FilePath $env:ATOMIZER_TEST_PYTHON -ArgumentList ('"{0}"' -f $env:ATOMIZER_TEST_SLEEP_SCRIPT) -TimeoutSeconds 1 -ReportFailure
$timer.Stop()
[ordered]@{
    type = $exitCode.GetType().FullName
    count = @($exitCode).Count
    value = [int]$exitCode
    elapsed_milliseconds = [int64]$timer.ElapsedMilliseconds
} | ConvertTo-Json -Compress
"""
        with tempfile.TemporaryDirectory() as temporary:
            sleep_script = Path(temporary) / "sleep.py"
            sleep_script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
            environment = dict(os.environ)
            environment["ATOMIZER_TEST_PROCESS_SCRIPT"] = str(
                PACKAGE_ROOT / "release" / "windows_process.ps1"
            )
            environment["ATOMIZER_TEST_PYTHON"] = sys.executable
            environment["ATOMIZER_TEST_SLEEP_SCRIPT"] = str(sleep_script)
            result = subprocess.run(
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=8,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1-second leaf timeout", result.stdout)
        payload = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(payload["type"], "System.Int32")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["value"], 124)
        self.assertLess(payload["elapsed_milliseconds"], 4_000)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows PowerShell 5.1")
    def test_leaf_process_failure_diagnostics_are_bounded(self) -> None:
        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        command = r"""
$ErrorActionPreference = 'Stop'
. $env:ATOMIZER_TEST_PROCESS_SCRIPT
$exitCode = Invoke-LeafProcess -FilePath $env:ATOMIZER_TEST_PYTHON -ArgumentList ('"{0}"' -f $env:ATOMIZER_TEST_OUTPUT_SCRIPT) -TimeoutSeconds 10 -ReportFailure
[ordered]@{ value = [int]$exitCode; count = @($exitCode).Count } | ConvertTo-Json -Compress
"""
        with tempfile.TemporaryDirectory() as temporary:
            output_script = Path(temporary) / "output.py"
            output_script.write_text(
                "import sys\n"
                "sys.stdout.write('o' * 10000)\n"
                "sys.stderr.write('e' * 10000)\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            environment = dict(os.environ)
            environment["ATOMIZER_TEST_PROCESS_SCRIPT"] = str(
                PACKAGE_ROOT / "release" / "windows_process.ps1"
            )
            environment["ATOMIZER_TEST_PYTHON"] = sys.executable
            environment["ATOMIZER_TEST_OUTPUT_SCRIPT"] = str(output_script)
            result = subprocess.run(
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=8,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("[diagnostic truncated]"), 2)
        self.assertLess(len(result.stdout), 9_000)
        payload = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(payload, {"value": 7, "count": 1})

    def test_seed_waits_for_nonempty_derived_convergence_before_stop_and_snapshot(self) -> None:
        script = (PACKAGE_ROOT / "release" / "test_installer.ps1").read_text(
            encoding="utf-8"
        )
        seed = script.index("$capture | & $hook")
        wait = script.index(
            "Wait-RuntimeReady -StatePath $statePath -Manager $manager -MinimumUnitsIndexed 1",
            seed,
        )
        stop = script.index("Stop-DisposableRuntime -Manager $manager", wait)
        snapshot = script.index("$snapshotBefore = Get-LogicalSnapshot", stop)
        self.assertLess(seed, wait)
        self.assertLess(wait, stop)
        self.assertLess(stop, snapshot)

    def test_installer_snapshot_uses_disposable_complete_file_not_diagnostic_stdout(self) -> None:
        script = (PACKAGE_ROOT / "release" / "test_installer_common.ps1").read_text(
            encoding="utf-8"
        )
        helper = (PACKAGE_ROOT / "release" / "sqlite_logical_snapshot.py").read_text(
            encoding="utf-8"
        )
        function_start = script.index("function Get-LogicalSnapshot")
        function_end = script.index("\n}\n\nfunction Assert-SnapshotHealthy", function_start)
        contract = script[function_start:function_end]
        self.assertIn('--output "{2}"', contract)
        self.assertIn("Test-Path -LiteralPath $snapshotPath -PathType Leaf", contract)
        self.assertIn("Get-Content -LiteralPath $snapshotPath -Raw -Encoding UTF8", contract)
        self.assertIn("ConvertFrom-Json", contract)
        self.assertIn("finally", contract)
        self.assertIn("Remove-Item -LiteralPath $snapshotPath", contract)
        self.assertNotIn("$snapshotResult.StandardOutput | ConvertFrom-Json", contract)
        self.assertIn('parser.add_argument("--output", type=Path, required=True)', helper)
        self.assertIn("os.replace(temporary, output)", helper)

    def test_checkout_independence_quarantines_contents_without_renaming_runner_cwd(self) -> None:
        script = (PACKAGE_ROOT / "release" / "test_installer.ps1").read_text(encoding="utf-8")
        self.assertIn("Get-ChildItem -Force -LiteralPath $Checkout", script)
        self.assertIn("$movedCheckoutEntries += $entry.Name", script)
        self.assertNotIn("Move-Item -LiteralPath $Checkout -Destination $renamedCheckout", script)

    def test_installed_acceptance_records_all_codex_and_uninstall_cases(self) -> None:
        script = (PACKAGE_ROOT / "release" / "test_installer.ps1").read_text(encoding="utf-8")
        common = (PACKAGE_ROOT / "release" / "test_installer_common.ps1").read_text(
            encoding="utf-8"
        )
        for field in (
            "credentials_preserved_across_reinstall",
            "credentials_removed_on_uninstall",
            "owned_hooks_removed",
            "unrelated_hooks_preserved",
            "library_preserved",
            "checkout_independent",
        ):
            self.assertIn(f"{field} = $true", script)
        self.assertIn("Fresh Codex hooks were not installed exactly once.", script)
        self.assertIn(
            "Reinstall did not collapse duplicate current Atomizer hooks.", script
        )
        self.assertIn("ambiguous Codex hook fixture changed", script)
        self.assertIn("PSObject.Properties[$Event]", common)
        post_uninstall = script[script.index("$stage = 'uninstall_completed'") :]
        self.assertNotIn("$hooks.hooks.UserPromptSubmit -or", post_uninstall)

    def test_installer_acceptance_does_not_help_workspace_reconciliation(self) -> None:
        script = (PACKAGE_ROOT / "release" / "test_installer.ps1").read_text(
            encoding="utf-8"
        )
        fixture_position = script.index("$workspaceHooksPath =")
        config_position = script.index("$configBlocks -join")
        installer_position = script.index("$installExit = Invoke-LeafProcess")
        inspection_position = script.index(
            "$workspaceHooks = Get-Content -LiteralPath $workspaceHooksPath"
        )
        self.assertLess(fixture_position, config_position)
        self.assertLess(config_position, installer_position)
        self.assertLess(installer_position, inspection_position)
        self.assertNotIn("$workspaceArguments", script)
        self.assertNotIn("$workspaceUninstallArguments", script)
        self.assertIn("unrelated_hooks_preserved = $true", script)

    def test_installer_acceptance_compares_explicit_identity_domains(self) -> None:
        script = (PACKAGE_ROOT / "release" / "test_installer.ps1").read_text(
            encoding="utf-8"
        )
        workflow = (PACKAGE_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        candidate_builder = (
            PACKAGE_ROOT / "release" / "build_candidate_metadata.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "$statusResult = Invoke-BoundedProcess -FilePath $manager -ArgumentList 'status'",
            script,
        )
        self.assertIn("$status = $statusResult.StandardOutput | ConvertFrom-Json", script)
        self.assertIn("$runtimeHealth = $status.health.runtime", script)
        self.assertIn(
            "$runtimeHealth.runtime_executable_sha256 -ne $runtimeExecutableSha256",
            script,
        )
        self.assertIn(
            "$runtimeHealth.current_runtime_executable_sha256 -ne $runtimeExecutableSha256",
            script,
        )
        self.assertIn("if ($runtimeHealth.restart_required)", script)
        self.assertIn(
            "$runtimeHealth.runtime_build_fingerprint -ne $expectedRuntimeBuildFingerprint",
            script,
        )
        self.assertNotIn("$health.runtime.runtime_executable_sha256", script)
        self.assertIn("runtime_executable_sha256 = $runtimeExecutableSha256", script)
        self.assertIn("source_runtime_equal =", script)
        self.assertIn('"executable_sha256"', candidate_builder)
        self.assertIn('"build_fingerprint"', candidate_builder)
        self.assertIn("build_candidate_metadata.py", workflow)

    def test_installer_acceptance_keeps_public_health_minimal(self) -> None:
        script = (PACKAGE_ROOT / "release" / "test_installer.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$publicHealth = Invoke-RestMethod", script)
        self.assertIn(
            "$expectedPublicHealthProperties = @('ok', 'service', 'runtime_running')",
            script,
        )
        self.assertIn(
            "Unauthenticated Library health exposed fields beyond the minimal availability contract.",
            script,
        )
        self.assertNotIn("$health = Invoke-RestMethod", script)

    def test_installer_acceptance_binds_installed_build_to_checkout_source(self) -> None:
        script = (PACKAGE_ROOT / "release" / "test_installer.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$expectedRuntimeBuildFingerprint", script)
        self.assertIn("runtime_build_fingerprint", script)
        self.assertIn(
            "Installed runtime build fingerprint does not match validation checkout source closure.",
            script,
        )
        self.assertIn("source_runtime_equal =", script)

    def test_installer_acceptance_tracks_separated_packaged_credentials(self) -> None:
        script = (PACKAGE_ROOT / "release" / "test_installer.ps1").read_text(
            encoding="utf-8"
        )
        helper = (
            PACKAGE_ROOT / "release" / "packaged_security_acceptance.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("$credential = Join-Path $dataDirectory 'bridge-credential.bin'", script)
        self.assertIn("management-credential.bin", script)
        self.assertIn("extension-pairing.bin", script)
        self.assertIn("Fresh install created a pre-paired extension secret.", script)
        self.assertIn("post-reinstall --data-directory", script)
        self.assertIn("credentials_preserved_across_reinstall = $true", script)
        self.assertIn("credentials_removed_on_uninstall = $true", script)

        self.assertIn("RuntimePaths.current_user()", helper)
        self.assertIn("CredentialStore(paths.credential).load()", helper)
        self.assertIn("capture_request_material", helper)
        self.assertIn("runtime_proof", helper)
        self.assertIn('bridge + "/v1/bootstrap"', helper)
        self.assertIn('"/v1/runtime/stop"', helper)
        self.assertIn('library, "/extension/revoke"', helper)
        self.assertNotIn("print(management", helper)
        self.assertNotIn("print(extension", helper)

        fresh = script.index("fresh --data-directory")
        pre_snapshot = script.index("$snapshotBefore = Get-LogicalSnapshot")
        reinstall = script.index("$reinstallExit = Invoke-LeafProcess")
        post = script.index("post-reinstall --data-directory")
        post_snapshot = script.index("$snapshotAfter = Get-LogicalSnapshot")
        uninstall = script.index(
            "$quietUninstallSuccess = Invoke-RegisteredQuietUninstall"
        )
        self.assertLess(fresh, pre_snapshot)
        self.assertLess(pre_snapshot, reinstall)
        self.assertLess(reinstall, post)
        self.assertLess(post, post_snapshot)
        self.assertLess(post_snapshot, uninstall)

    def test_dpapi_store_has_no_bridge_specific_credential_semantics(self) -> None:
        source = (
            PACKAGE_ROOT
            / "src"
            / "atomizer_local_client"
            / "runtime"
            / "credentials.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("bridge credential", source.casefold())
        self.assertIn("CRYPTPROTECT_UI_FORBIDDEN", source)
        self.assertNotIn("CRYPTPROTECT_LOCAL_MACHINE", source)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows DPAPI")
    def test_packaged_security_probe_exercises_fresh_restart_revoke_and_repair(self) -> None:
        from atomizer_local_client.runtime.application import AtomizerLocalRuntime
        from atomizer_local_client.runtime.configuration import RuntimeConfig, RuntimePaths
        from atomizer_local_client.runtime.permissions import PermissionStore

        helper = self._load_release_module(
            "packaged_security_acceptance.py", "packaged_security_acceptance_test"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {
                "LOCALAPPDATA": str(root / "local"),
                "APPDATA": str(root / "roaming"),
                "USERPROFILE": str(root / "profile"),
            }
            with mock.patch.dict(os.environ, environment):
                paths = RuntimePaths.current_user()
                paths.app_data.mkdir(parents=True)
                config = RuntimeConfig()
                config.save(paths.config)
                PermissionStore(paths.permissions).set_enabled("chatgpt_web", True)
                receipt = root / "receipt.json"
                receipt.write_text("{}\n", encoding="utf-8")

                runtime = AtomizerLocalRuntime(paths, config, _test_bridge_port=0)
                runtime.start()
                try:
                    fresh = helper.fresh(paths.app_data, receipt)
                    management_before = paths.credential.read_bytes()
                    extension_before = paths.extension_credential.read_bytes()
                finally:
                    runtime.stop()

                runtime = AtomizerLocalRuntime(paths, config, _test_bridge_port=0)
                runtime.start()
                try:
                    self.assertEqual(paths.credential.read_bytes(), management_before)
                    self.assertEqual(paths.extension_credential.read_bytes(), extension_before)
                    post = helper.post_reinstall(paths.app_data, receipt)
                finally:
                    runtime.stop()

                self.assertTrue(fresh["management_initialized"])
                self.assertTrue(fresh["authority_separated"])
                self.assertTrue(post["extension_pairing_preserved"])
                self.assertTrue(post["revoke_invalidated_old_secret"])
                self.assertTrue(paths.extension_credential.is_file())

    def test_windows_builder_rejects_pre_bom_runtime_source_closure(self) -> None:
        builder = self._load_release_module(
            "build_windows.py", "release_build_windows_identity_test"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "project" / "src" / "atomizer_local_client"
            stale_package = root / "stale" / "atomizer_local_client"
            runtime = root / "runtime"
            for candidate in (package, stale_package):
                (candidate / "runtime").mkdir(parents=True)
            runtime.mkdir()
            for name in ("codex_workspace.py", "codex_integration.py"):
                (package / "runtime" / name).write_text(
                    'ENCODING = "utf-8-sig"\n', encoding="utf-8"
                )
                (stale_package / "runtime" / name).write_text(
                    'ENCODING = "utf-8"\n', encoding="utf-8"
                )
            stale_fingerprint = runtime_build_fingerprint(stale_package)
            expected_fingerprint = runtime_build_fingerprint(package)
            self.assertNotEqual(stale_fingerprint, expected_fingerprint)
            identity = runtime / BUILD_IDENTITY_FILENAME
            identity.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runtime_build_fingerprint": stale_fingerprint,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError, "runtime build identity does not match project source closure"
            ):
                builder.validate_runtime_source_closure(root / "project", runtime)
            identity.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runtime_build_fingerprint": expected_fingerprint,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                builder.validate_runtime_source_closure(root / "project", runtime),
                expected_fingerprint,
            )

    def test_installer_propagates_required_manager_failures(self) -> None:
        script = (PACKAGE_ROOT / "release" / "windows" / "ContextAtomizer.nsi").read_text(
            encoding="utf-8"
        )
        self.assertIn("RequestExecutionLevel user", script)
        self.assertIn("SetCompressor zlib", script)
        self.assertIn("Function RunManagerStep", script)
        self.assertIn("ExecWait", script)
        self.assertIn("IfErrors manager_launch_failed", script)
        self.assertIn('StrCmp $2 "0" manager_done', script)
        self.assertIn("Abort", script)
        self.assertIn('Section /o "Enable ChatGPT Web capture"', script)
        self.assertIn('Section /o "Enable Codex capture"', script)
        self.assertLess(
            script.index("Call StopExistingRuntime"),
            script.index('File "${SourceDir}\\atomizer-local-runtime.exe"'),
        )
        self.assertNotRegex(script, r"\b[A-Za-z0-9_]+::[A-Za-z0-9_]+")

    def test_installer_enforces_x64_compatible_architecture_before_sections(self) -> None:
        script = (PACKAGE_ROOT / "release" / "windows" / "ContextAtomizer.nsi").read_text(
            encoding="utf-8"
        )
        self.assertIn('!include "x64.nsh"', script)
        self.assertIn('!include "WinVer.nsh"', script)
        self.assertIn("Function RequireX64CompatibleWindows", script)
        self.assertIn("${IsNativeAMD64}", script)
        self.assertIn("${IsNativeARM64}", script)
        self.assertIn("${AtLeastWin11}", script)
        self.assertIn("SetErrorLevel 2", script)
        self.assertIn("IfSilent architecture_abort", script)
        self.assertIn("/SD IDOK", script)
        on_init = script.index("Function .onInit")
        gate = script.index("Call RequireX64CompatibleWindows", on_init)
        shell_context = script.index("SetShellVarContext current", on_init)
        self.assertLess(on_init, gate)
        self.assertLess(gate, shell_context)

    def test_silent_manager_failures_are_noninteractive_and_nonzero(self) -> None:
        script = (PACKAGE_ROOT / "release" / "windows" / "ContextAtomizer.nsi").read_text(
            encoding="utf-8"
        )
        for label in (
            "stop_abort",
            "stop_launch_abort",
            "manager_abort",
            "manager_launch_abort",
        ):
            self.assertIn(f"IfSilent {label}", script)
        self.assertGreaterEqual(script.count("SetErrorLevel 2"), 5)
        self.assertEqual(script.count("MessageBox MB_ICONSTOP|MB_OK"), 7)
        self.assertEqual(script.count("/SD IDOK"), 8)

        acceptance = (PACKAGE_ROOT / "release" / "test_installer_failure.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("System32\\net.exe", acceptance)
        self.assertIn("uninstall_exit = [int]$uninstall.ExitCode", acceptance)
        self.assertIn("manager_failure_did_not_claim_full_cleanup = $true", acceptance)

    def test_silent_uninstall_unexpected_failure_is_observable_via_nsis_direct_contract(self) -> None:
        script = (PACKAGE_ROOT / "release" / "windows" / "ContextAtomizer.nsi").read_text(
            encoding="utf-8"
        )
        for label in ("cleanup_error:", "cleanup_launch_failed:"):
            start = script.index(label)
            end = script.index("Goto cleanup_files", start)
            block = script[start:end]
            self.assertIn("SetErrorLevel 3", block)
            self.assertIn("IfSilent cleanup_files", block)
            self.assertIn("/SD IDOK", block)
        ambiguous = script[
            script.index("cleanup_ambiguous:") : script.index("cleanup_files:")
        ]
        self.assertIn("SetErrorLevel 2", ambiguous)

        acceptance = (PACKAGE_ROOT / "release" / "test_installer_failure.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("Invoke-RegisteredQuietUninstall", acceptance)
        self.assertIn("uninstall_exit = [int]$uninstall.ExitCode", acceptance)
        self.assertIn("library_preserved = $true", acceptance)

    def test_registered_quiet_uninstall_is_unique_safe_and_machine_observable(self) -> None:
        builder = self._load_release_module("build_windows.py", "release_quiet_uninstall")
        source = (
            PACKAGE_ROOT / "release" / "windows" / "quiet_uninstall.ps1"
        ).read_text(encoding="utf-8")
        encoded = builder.quiet_uninstall_command(PACKAGE_ROOT)
        repeated = builder.quiet_uninstall_command(PACKAGE_ROOT)
        compact_source = "".join(line.strip() for line in source.splitlines())
        decoded = base64.b64decode(encoded).decode("utf-16-le")
        self.assertEqual(decoded, compact_source)
        self.assertEqual(decoded, builder.normalized_quiet_uninstall_source(PACKAGE_ROOT))
        self.assertEqual(repeated, encoded)
        self.assertNotIn(str(PACKAGE_ROOT), decoded)
        for forbidden_state in ("credential", "secret", "token"):
            self.assertNotIn(forbidden_state, decoded.casefold())

        quiet_uninstall_string = builder.quiet_uninstall_registry_command(PACKAGE_ROOT)
        self.assertTrue(quiet_uninstall_string.endswith(encoded))
        self.assertLessEqual(len(quiet_uninstall_string), 1023)
        self.assertLess(len(quiet_uninstall_string), 8192)
        self.assertLess(len(quiet_uninstall_string), 32767)

        self.assertIn("gp 'HKCU:\\Software\\Microsoft\\Windows", source)
        self.assertIn(".InstallLocation", source)
        self.assertIn("$env:TEMP", source)
        self.assertIn("[guid]::NewGuid()", source)
        self.assertIn('[IO.File]::Copy("$d\\Uninstall.exe",$u)', source)
        self.assertIn('[Diagnostics.Process]::Start($u,"/S _?=$d")', source)
        self.assertIn("$p.WaitForExit()", source)
        self.assertIn("exit $p.ExitCode", source)
        self.assertIn("rm -LiteralPath $u -Force -EA 0", source)
        self.assertNotIn("Invoke-Expression", source)
        self.assertNotIn("Start-Process", source)
        self.assertNotIn("cmd.exe", source.casefold())
        self.assertNotIn("-ExecutionPolicy", source)
        self.assertNotIn("/NCRC", source)

        script = (PACKAGE_ROOT / "release" / "windows" / "ContextAtomizer.nsi").read_text(
            encoding="utf-8"
        )
        self.assertIn('"InstallLocation" "$INSTDIR"', script)
        self.assertIn('"QuietUninstallString"', script)
        self.assertIn("-NoProfile -NonInteractive -EncodedCommand", script)
        self.assertNotIn('"QuietUninstallString" "$\\\"$INSTDIR\\Uninstall.exe$\\\" /S"', script)
        self.assertNotIn("quiet_uninstall.ps1", script)
        self.assertNotRegex(script, r"\b[A-Za-z0-9_]+::[A-Za-z0-9_]+")

        runtime_builder = (PACKAGE_ROOT / "release" / "build_runtime.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("quiet_uninstall.ps1", runtime_builder)

        scenarios = "\n".join(
            (PACKAGE_ROOT / "release" / name).read_text(encoding="utf-8")
            for name in (
                "test_installer.ps1",
                "test_installer_ambiguous.ps1",
                "test_installer_failure.ps1",
            )
        )
        for receipt in (
            "uninstall_exit",
            "workspace_discovery_count",
            "manager_failure_did_not_claim_full_cleanup",
            "quiet_uninstall_stale_copy_preserved",
            "quiet_uninstall_temp_copy_removed",
        ):
            self.assertIn(receipt, scenarios)

    def test_installer_acceptance_uses_registered_quiet_contract_for_every_scenario(self) -> None:
        normal = (PACKAGE_ROOT / "release" / "test_installer.ps1").read_text(encoding="utf-8")
        ambiguous = (PACKAGE_ROOT / "release" / "test_installer_ambiguous.ps1").read_text(encoding="utf-8")
        failure = (PACKAGE_ROOT / "release" / "test_installer_failure.ps1").read_text(encoding="utf-8")
        scenarios = (normal, ambiguous, failure)
        helper = (PACKAGE_ROOT / "release" / "test_quiet_uninstall.ps1").read_text(
            encoding="utf-8"
        )

        for scenario in scenarios:
            self.assertIn("test_installer_common.ps1", scenario)
            self.assertEqual(scenario.count("Invoke-RegisteredQuietUninstall"), 1)
            self.assertEqual(scenario.count("Get-RegisteredQuietUninstall"), 1)
            self.assertNotIn("_?=", scenario)
        self.assertNotIn("_?=", helper)
        self.assertIn("Get-ItemProperty -LiteralPath $UninstallKey", helper)
        self.assertIn("$registration.InstallLocation -ne $ApplicationDirectory", helper)
        self.assertIn("$registration.QuietUninstallString", helper)
        self.assertIn("Invoke-BoundedProcess", helper)
        self.assertIn("Atomizer-Q-stale.exe", helper)
        self.assertIn("Get-FileHash -LiteralPath $stale", helper)
        self.assertIn("Registered quiet uninstall left its temporary executable behind.", helper)

        self.assertIn("Context Atomizer Acceptance", normal)
        self.assertIn("$applicationSiblingMarker", normal)
        self.assertIn("$diagnosticResult = Invoke-BoundedProcess", ambiguous)
        self.assertIn("@{ Label = 'workspace_one'; Class = 'CURRENT_ATOMIZER' }", ambiguous)
        self.assertIn("@{ Label = 'workspace_two'; Class = 'CURRENT_ATOMIZER' }", ambiguous)
        self.assertIn("@{ Label = 'unrelated'; Class = 'UNRELATED' }", ambiguous)
        self.assertIn('"$($case.Label)_discovered"', ambiguous)
        self.assertIn('"$($case.Label)_${eventLabel}_count"', ambiguous)
        self.assertIn('"$($case.Label)_${eventLabel}_class"', ambiguous)
        self.assertLess(ambiguous.index("$diagnosticResult ="), ambiguous.index("$ambiguousHook ="))
        self.assertIn("[IO.File]::ReadAllBytes($hooksPath)", ambiguous)
        self.assertIn("$globalShaBefore", ambiguous)
        self.assertIn("$unrelatedBytesBefore", ambiguous)
        self.assertIn("$configBytesAfter", ambiguous)
        self.assertIn("System32\\net.exe", failure)
        self.assertNotIn("System32\\net.exe", normal)
        self.assertNotIn("System32\\net.exe", ambiguous)

    def test_uninstaller_runs_core_cleanup_once_and_surfaces_partial_codex_state(self) -> None:
        script = (PACKAGE_ROOT / "release" / "windows" / "ContextAtomizer.nsi").read_text(
            encoding="utf-8"
        )
        cleanup = 'uninstall --codex-hooks "$PROFILE\\.codex\\hooks.json"'
        self.assertEqual(script.count(cleanup), 1)
        self.assertIn('StrCmp $0 "2" cleanup_ambiguous cleanup_error', script)
        self.assertIn("core runtime state was removed", script)
        self.assertIn("Library database was not deleted", script)
        self.assertLess(script.index(cleanup), script.index('Delete "$INSTDIR\\atomizer-local-runtime.exe"'))
        self.assertNotIn("context-atomizer.db", script)

    def test_one_canonical_pep440_version_drives_runtime_and_browser_metadata(self) -> None:
        project = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["version"], "0.1.0.dev0")
        with mock.patch(
            "atomizer_local_client.runtime_health.importlib.metadata.version",
            return_value=project["project"]["version"],
        ):
            self.assertEqual(runtime_version(), "0.1.0.dev0")
        self.assertEqual(
            HOOK_OWNERSHIP_CONTRACT_VERSION, "codex-hook-ownership-v1"
        )
        self.assertEqual(
            WORKSPACE_DISCOVERY_CONTRACT_VERSION,
            "codex-hook-state-workspaces-v1",
        )
        module_path = PACKAGE_ROOT / "browser_extension" / "package_extension.py"
        specification = importlib.util.spec_from_file_location("release_package_extension", module_path)
        module = importlib.util.module_from_spec(specification)
        assert specification.loader is not None
        specification.loader.exec_module(module)
        self.assertEqual(module.release_versions(), ("0.1.0.0", "0.1.0-dev0"))
        for browser in ("chromium", "firefox"):
            source = json.loads(
                (PACKAGE_ROOT / "browser_extension" / "browsers" / browser / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(source["version"], "0.1.0.0")
            self.assertEqual(source["version_name"], "0.1.0-dev0")

    def test_release_migration_boundary_is_exactly_001_through_007(self) -> None:
        identifiers = registered_migration_ids()
        self.assertEqual(len(identifiers), 7)
        self.assertEqual(identifiers[0], "001_initial")
        self.assertEqual(identifiers[-1], "007_temporal_governance")

    def test_ci_manifest_uses_discovered_python_test_total(self) -> None:
        workflow = (PACKAGE_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("defaultTestLoader.discover('tests').countTestCases()", workflow)
        self.assertIn("--python-tests $pythonTests", workflow)
        self.assertNotIn("--python-tests 184", workflow)

    def test_ci_provisions_exact_nsis_release_without_machine_install(self) -> None:
        workflow = (PACKAGE_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Provision pinned NSIS", workflow)
        self.assertIn("NSIS%203/3.12/nsis-3.12.zip?download", workflow)
        self.assertIn("--retry-all-errors --continue-at -", workflow)
        self.assertIn(
            "56581f90db321581c5381193d796fffcf2d24b2f8fed2160a6c6a3baa67f2c4f",
            workflow,
        )
        self.assertIn("$reportedVersion -ne 'v3.12'", workflow)
        self.assertIn("--compiler $env:ATOMIZER_NSIS_COMPILER", workflow)
        self.assertIn("--nsis-version 3.12", workflow)
        self.assertIn("--nsis-archive-sha256 $env:ATOMIZER_NSIS_ARCHIVE_SHA256", workflow)
        self.assertNotIn("winget install", workflow.lower())
        self.assertNotIn("choco install", workflow.lower())

    def test_manifest_uses_generic_installer_and_signing_metadata(self) -> None:
        manifest_builder = (PACKAGE_ROOT / "release" / "build_manifest.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"installer_builder": "NSIS"', manifest_builder)
        self.assertIn('"installer_builder_version": installer_builder_version', manifest_builder)
        self.assertIn('"windows_installer": "unsigned-development-build"', manifest_builder)
        self.assertIn('"broad_commercial_release_allowed": False', manifest_builder)
        self.assertIn(
            '"broad_commercial_release_requirement": "authenticode-signing"',
            manifest_builder,
        )

    def test_runtime_identity_domains_are_explicit_and_like_for_like(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "atomizer_local_client"
            package.mkdir()
            (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            source_fingerprint = runtime_build_fingerprint(package)
            self.assertEqual(source_fingerprint, runtime_build_fingerprint(package))
            (package / BUILD_IDENTITY_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runtime_build_fingerprint": source_fingerprint,
                    }
                ),
                encoding="utf-8",
            )
            executable = Path(temporary) / "atomizer-local-runtime.exe"
            executable.write_bytes(b"release-runtime")
            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(
                sys, "executable", str(executable)
            ):
                snapshot = RuntimeIdentity(package).snapshot()
            executable_sha = "3c198d7baf6c151d51bde04ce7947cf59233f9f8d9127766c9ee6f9d70dce46e"
            self.assertEqual(snapshot["runtime_build_fingerprint"], source_fingerprint)
            self.assertEqual(snapshot["runtime_executable_sha256"], executable_sha)
            self.assertEqual(runtime_executable_sha256(executable), executable_sha)
            self.assertFalse(snapshot["restart_required"])

    def test_manifest_records_build_and_executable_identity_separately(self) -> None:
        manifest_builder = (PACKAGE_ROOT / "release" / "build_manifest.py").read_text(
            encoding="utf-8"
        )
        runtime_builder = (PACKAGE_ROOT / "release" / "build_runtime.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"runtime_build_fingerprint"', manifest_builder)
        self.assertIn('"runtime_executable_sha256"', manifest_builder)
        self.assertIn("BUILD_IDENTITY_FILENAME", runtime_builder)

    def test_ci_is_a_build_once_five_job_lifecycle_dag(self) -> None:
        workflow = (PACKAGE_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        jobs = re.findall(
            r"^  ([a-z_]+):\n", workflow[workflow.index("jobs:\n") :], flags=re.MULTILINE
        )
        self.assertEqual(
            jobs,
            [
                "build_validate",
                "lifecycle_normal",
                "lifecycle_ambiguous",
                "lifecycle_failure",
                "finalize_artifact",
            ],
        )
        self.assertEqual(workflow.count("release/build_runtime.py"), 1)
        self.assertEqual(workflow.count("release/build_windows.py"), 1)
        self.assertIn("needs: build_validate", workflow)
        self.assertEqual(workflow.count("needs: build_validate"), 3)
        self.assertIn(
            "needs: [build_validate, lifecycle_normal, lifecycle_ambiguous, lifecycle_failure]",
            workflow,
        )
        download = "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
        self.assertEqual(workflow.count(download), 7)
        self.assertEqual(workflow.count("if: always()"), 3)
        self.assertIn("# v4, MIT", workflow)
        finalize = workflow[workflow.index("  finalize_artifact:") :]
        self.assertNotIn("build_runtime.py", finalize)
        self.assertNotIn("build_windows.py", finalize)
        self.assertIn("finalize_candidate.py", finalize)

    def test_lifecycle_receipts_are_candidate_bound_and_content_safe(self) -> None:
        common = (PACKAGE_ROOT / "release" / "test_installer_common.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("git_commit_sha", common)
        self.assertIn("installer_sha256", common)
        self.assertIn("installer_size_bytes", common)
        for name, scenario in (
            ("test_installer.ps1", "normal"),
            ("test_installer_ambiguous.ps1", "ambiguous"),
            ("test_installer_failure.ps1", "failure"),
        ):
            script = (PACKAGE_ROOT / "release" / name).read_text(encoding="utf-8")
            self.assertIn("trap {", script)
            self.assertIn(
                f"Write-LifecycleReceipt -Path $Receipt -Scenario '{scenario}' -Passed $false",
                script,
            )
            self.assertIn(
                f"Write-LifecycleReceipt -Path $Receipt -Scenario '{scenario}' -Passed $true",
                script,
            )
        diagnostic = (
            PACKAGE_ROOT / "release" / "codex_workspace_diagnostic.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"label": label', diagnostic)
        self.assertIn('"classes"', diagnostic)
        self.assertNotIn('"hooks_path"', diagnostic)
        self.assertNotIn('"command"', diagnostic)

    def test_workspace_diagnostic_reports_each_event_ownership_independently(self) -> None:
        helper = self._load_release_module(
            "codex_workspace_diagnostic.py", "release_workspace_diagnostic_test"
        )
        with tempfile.TemporaryDirectory() as temporary:
            # Hosted Windows temp paths can arrive in 8.3 spelling; construct the
            # registered hook paths from their filesystem-resolved identity.
            root = Path(temporary).resolve()
            global_hooks = root / "profile" / ".codex" / "hooks.json"
            workspace = root / "workspace" / ".codex" / "hooks.json"
            unrelated = root / "unrelated" / ".codex" / "hooks.json"
            config = root / "profile" / ".codex" / "config.toml"
            for path in (global_hooks, workspace, unrelated):
                path.parent.mkdir(parents=True, exist_ok=True)
            global_hooks.write_text('{"hooks": {}}\n', encoding="utf-8")
            current = r'"C:\Program Files\Context Atomizer\atomizer-codex-hook.exe" --database "C:\Data\history.sqlite3"'
            current_entry = {"hooks": [{"type": "command", "command": current}]}
            workspace.write_text(
                json.dumps({"hooks": {"UserPromptSubmit": [current_entry], "Stop": [current_entry]}}),
                encoding="utf-8",
            )
            unrelated_entry = {"hooks": [{"type": "command", "command": "other-tool"}]}
            unrelated.write_text(
                json.dumps({"hooks": {"UserPromptSubmit": [unrelated_entry], "Stop": [unrelated_entry]}}),
                encoding="utf-8",
            )
            registrations = (
                f"[hooks.state.'{workspace}:user_prompt_submit:0:0']\ntrusted_hash='x'\n"
                f"[hooks.state.'{unrelated}:stop:0:0']\ntrusted_hash='x'\n"
            )
            config.write_text(registrations, encoding="utf-8")
            payload = helper.diagnose(
                config,
                global_hooks,
                current,
                {workspace.resolve(): "workspace", unrelated.resolve(): "unrelated"},
            )
            self.assertEqual(payload["discovered_count"], 2)
            by_label = {item["label"]: item["events"] for item in payload["targets"]}
            for event in ("UserPromptSubmit", "Stop"):
                self.assertEqual(by_label["workspace"][event], {"count": 1, "classes": ["CURRENT_ATOMIZER"]})
                self.assertEqual(by_label["unrelated"][event], {"count": 1, "classes": ["UNRELATED"]})

    def test_finalizer_rejects_identity_drift_and_emits_one_bound_manifest(self) -> None:
        finalizer = self._load_release_module(
            "finalize_candidate.py", "release_finalize_candidate_test"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = root / "ContextAtomizer-Setup-v0.1.0-dev0.exe"
            chromium = root / "ContextAtomizer-Chromium-v0.1.0-dev0.zip"
            installer.write_bytes(b"exact-installer")
            chromium.write_bytes(b"exact-chromium")
            candidate = {
                "schema_version": 1,
                "git_commit_sha": "a" * 40,
                "installer": {
                    "name": installer.name,
                    "sha256": finalizer.sha256(installer),
                    "size_bytes": installer.stat().st_size,
                },
                "chromium": {
                    "name": chromium.name,
                    "sha256": finalizer.sha256(chromium),
                    "size_bytes": chromium.stat().st_size,
                },
                "source_fingerprint": "b" * 64,
                "runtime": {"build_fingerprint": "b" * 64},
                "source_runtime_equal": True,
                "validation": {"python_tests": 226, "browser_tests": 50},
                "nsis": {"version": "3.12", "archive_sha256": "c" * 64},
                "signing": {"status": "unsigned-development-build"},
            }
            candidate_path = root / "candidate-metadata.json"
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            receipts = []
            for scenario in ("normal", "ambiguous", "failure"):
                receipt = root / f"{scenario}.json"
                receipt.write_text(
                    json.dumps(
                        {
                            "scenario": scenario,
                            "passed": True,
                            "git_commit_sha": candidate["git_commit_sha"],
                            "installer_sha256": candidate["installer"]["sha256"],
                            "installer_size_bytes": candidate["installer"]["size_bytes"],
                            "evidence": {"bounded": True},
                        }
                    ),
                    encoding="utf-8",
                )
                receipts.append(receipt)
            output = root / "final"
            arguments = [
                "finalize_candidate.py",
                "--candidate",
                str(candidate_path),
                *[item for receipt in receipts for item in ("--receipt", str(receipt))],
                "--output",
                str(output),
            ]
            with mock.patch.object(sys, "argv", arguments):
                self.assertEqual(finalizer.main(), 0)
            manifest = json.loads(
                (output / "ContextAtomizer-v0.1.0-dev0-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(set(manifest["lifecycle"]), {"normal", "ambiguous", "failure"})
            self.assertTrue(all(item["passed"] for item in manifest["lifecycle"].values()))
            self.assertEqual(finalizer.sha256(output / installer.name), candidate["installer"]["sha256"])

            drifted = json.loads(receipts[0].read_text(encoding="utf-8"))
            drifted["installer_sha256"] = "0" * 64
            receipts[0].write_text(json.dumps(drifted), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "installer mismatch"):
                with mock.patch.object(
                    sys,
                    "argv",
                    [*arguments[:-1], str(root / "rejected")],
                ):
                    finalizer.main()


if __name__ == "__main__":
    unittest.main()
