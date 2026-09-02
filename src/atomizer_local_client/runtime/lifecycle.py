"""Repeatable install, update, restart, open, and uninstall lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
import ctypes
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from atomizer_local_client.platforms.credentials import current_credential_store
from atomizer_local_client.runtime.claude_integration import (
    install_claude_hooks,
    remove_claude_hooks,
)
from atomizer_local_client.runtime.codex_integration import (
    codex_hook_file_has_atomizer_entries,
    codex_hook_file_is_empty,
    hook_command,
    reconcile_codex_hook_targets,
)
from atomizer_local_client.runtime.codex_workspace import CodexWorkspaceSource
from atomizer_local_client.runtime.configuration import (
    RuntimeConfig,
    RuntimePaths,
    read_state,
    remove_state,
)
from atomizer_local_client.runtime.windows_startup import (
    WindowsRunRegistration,
    runtime_startup_command,
)
from atomizer_local_client.runtime_health import RuntimeIdentity
from atomizer_local_client.runtime.permissions import PermissionStore


class StartupRegistration(Protocol):
    def install(self, command: str) -> None: ...
    def read(self) -> str | None: ...
    def remove(self) -> None: ...


def _runtime_command_prefix() -> list[str]:
    scripts = Path(sys.executable).resolve().parent
    if sys.platform == "darwin":
        packaged = scripts / "atomizer-local-runtime"
        if packaged.is_file():
            return [str(packaged)]
        return [
            str(Path(sys.executable).resolve()),
            "-m",
            "atomizer_local_client.runtime.application",
        ]
    packaged = scripts / "atomizer-local-runtime.exe"
    if packaged.is_file():
        return [str(packaged)]
    pythonw = scripts / "pythonw.exe"
    if not pythonw.is_file():
        raise RuntimeError("the windowless runtime launcher is not installed")
    return [str(pythonw), "-m", "atomizer_local_client.runtime.application"]


def _hook_executable() -> Path:
    executable_name = (
        "atomizer-codex-hook" if sys.platform == "darwin" else "atomizer-codex-hook.exe"
    )
    executable = Path(sys.executable).resolve().parent / executable_name
    if not executable.is_file():
        raise RuntimeError("the packaged Codex hook executable is not installed")
    return executable


def _claude_hook_executable() -> Path:
    executable_name = (
        "atomizer-claude-hook"
        if sys.platform == "darwin"
        else "atomizer-claude-hook.exe"
    )
    executable = Path(sys.executable).resolve().parent / executable_name
    if not executable.is_file():
        raise RuntimeError("the packaged Claude hook executable is not installed")
    return executable


class RuntimeProcessLauncher:
    def launch(self, command: list[str]) -> subprocess.Popen[bytes]:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


class LifecycleManager:
    def __init__(
        self,
        paths: RuntimePaths,
        *,
        startup: StartupRegistration,
        runtime_command_prefix: list[str],
        credential_store: Any | None = None,
        process_launcher: RuntimeProcessLauncher | None = None,
        runtime_identity: RuntimeIdentity | None = None,
        startup_command_builder: Callable[[list[str], Path], str] = runtime_startup_command,
        url_opener: Callable[[str], bool] = webbrowser.open,
    ) -> None:
        self.paths = paths
        self.startup = startup
        self.runtime_command_prefix = list(runtime_command_prefix)
        self.credential_store = credential_store or current_credential_store(
            paths.credential
        )
        self.process_launcher = process_launcher or RuntimeProcessLauncher()
        self.runtime_identity = runtime_identity or RuntimeIdentity()
        self.startup_command_builder = startup_command_builder
        self.url_opener = url_opener
        self.permission_store = PermissionStore(paths.permissions)

    def _command(self) -> list[str]:
        return [*self.runtime_command_prefix, "--config", str(self.paths.config)]

    def _reconcile_codex(
        self,
        global_hooks: Path,
        *,
        codex_config: Path | None,
        hook_executable: Path,
        enabled: bool,
    ) -> tuple[bool, int, int]:
        command = hook_command(hook_executable, self.paths.database)
        targets = [Path(global_hooks)]
        workspace_count = 0
        if codex_config is not None:
            discovered = CodexWorkspaceSource(
                codex_config, global_hooks_path=global_hooks
            ).discover()
            for target in discovered:
                if codex_hook_file_has_atomizer_entries(
                    target.hooks_path, command
                ) or (enabled and codex_hook_file_is_empty(target.hooks_path)):
                    targets.append(target.hooks_path)
                    workspace_count += 1
        reconciliation = reconcile_codex_hook_targets(
            tuple(targets), command, enabled=enabled
        )
        return (
            bool(reconciliation.changed_paths),
            workspace_count,
            reconciliation.changed_paths,
        )

    def install(
        self,
        *,
        config: RuntimeConfig | None = None,
        start: bool = True,
        codex_hooks: Path | None = None,
        codex_config: Path | None = None,
        codex_hook_executable: Path | None = None,
        claude_settings: Path | None = None,
        claude_hook_executable: Path | None = None,
        chatgpt_enabled: bool | None = None,
    ) -> dict[str, object]:
        if sys.platform == "darwin":
            from atomizer_local_client.platforms.macos.permissions import (
                ensure_private_directory,
            )

            ensure_private_directory(self.paths.app_data)
        else:
            self.paths.app_data.mkdir(parents=True, exist_ok=True)
        effective = config
        if effective is None and self.paths.config.exists():
            effective = RuntimeConfig.load(self.paths.config)
        effective = effective or RuntimeConfig()
        effective.save(self.paths.config)
        self.credential_store.load_or_create()
        command = self.startup_command_builder(
            self.runtime_command_prefix, self.paths.config
        )
        self.startup.install(command)
        if chatgpt_enabled is not None:
            self.permission_store.set_enabled("chatgpt_web", chatgpt_enabled)
        codex_changed = False
        codex_workspace_targets = 0
        codex_changed_paths = 0
        if codex_hooks is not None:
            hook_executable = codex_hook_executable or _hook_executable()
            (
                codex_changed,
                codex_workspace_targets,
                codex_changed_paths,
            ) = self._reconcile_codex(
                codex_hooks,
                codex_config=codex_config,
                hook_executable=hook_executable,
                enabled=True,
            )
            self.permission_store.set_codex_installed(True)
            self.permission_store.set_enabled("codex", True)
        claude_changed = False
        if claude_settings is not None:
            claude_changed = install_claude_hooks(
                claude_settings,
                claude_hook_executable or _claude_hook_executable(),
                self.paths.database,
                self.paths.permissions,
            )
            self.permission_store.set_claude_code_installed(True)
            self.permission_store.set_enabled("claude_code", True)
        if start and not self.status()["running"]:
            self.process_launcher.launch(self._command())
            self.wait_for_running()
        return {
            "installed": True,
            "startup_registered": self.startup.read() == command,
            "running": self.status()["running"],
            "codex_hooks_changed": codex_changed,
            "codex_workspace_targets": codex_workspace_targets,
            "codex_hook_changed_paths": codex_changed_paths,
            "claude_hooks_changed": claude_changed,
            "database_preserved": self.paths.database.exists(),
            "permissions": {
                name: permission.enabled
                for name, permission in self.permission_store.snapshot().items()
            },
        }

    def wait_for_running(self, *, timeout: float = 8.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            if status["running"]:
                return status
            time.sleep(0.05)
        raise RuntimeError("Atomizer Local did not become healthy before timeout")

    def status(self) -> dict[str, Any]:
        state = read_state(self.paths.state)
        if state is None:
            return {
                "installed": self.paths.config.is_file() and self.startup.read() is not None,
                "running": False,
                "update_required": False,
                "runtime_build": None,
            }
        port = state.get("bridge_port")
        healthy = False
        runtime_health: dict[str, Any] | None = None
        if isinstance(port, int):
            try:
                token = self.credential_store.load()
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/management/status",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(request, timeout=0.5) as response:
                    runtime_health = json.loads(response.read())
                healthy = bool(runtime_health.get("runtime_running"))
            except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
                healthy = False
        current_build = self.runtime_identity.startup_build_sha256
        return {
            "installed": self.paths.config.is_file() and self.startup.read() is not None,
            "running": healthy,
            "update_required": state.get("runtime_build") != current_build,
            "runtime_build": state.get("runtime_build"),
            "bridge_port": state.get("bridge_port"),
            "library_port": state.get("library_port"),
            "health": runtime_health,
        }

    def stop(self, *, timeout: float = 8.0) -> bool:
        state = read_state(self.paths.state)
        if state is None:
            return False
        port = state.get("bridge_port")
        if not isinstance(port, int):
            raise RuntimeError("runtime state does not contain a valid bridge endpoint")
        pid = state.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise RuntimeError("runtime state does not contain a valid process identifier")
        token = self.credential_store.load()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/runtime/stop",
            data=b"{}",
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status != 202:
                    raise RuntimeError("runtime refused the authenticated stop request")
        except urllib.error.URLError as exc:
            if not _process_is_running(pid):
                remove_state(self.paths.state)
                return False
            raise RuntimeError("runtime stop endpoint is unavailable; no unknown process was terminated") from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _process_is_running(pid):
                remove_state(self.paths.state)
                return True
            time.sleep(0.05)
        raise RuntimeError("runtime did not stop before timeout")

    def start(self) -> dict[str, Any]:
        status = self.status()
        if status["running"]:
            return status
        if not self.paths.config.is_file():
            raise RuntimeError("Atomizer Local is not installed")
        self.process_launcher.launch(self._command())
        return self.wait_for_running()

    def restart(self) -> dict[str, Any]:
        if read_state(self.paths.state) is not None:
            self.stop()
        return self.start()

    def update(
        self,
        *,
        codex_hooks: Path | None = None,
        codex_config: Path | None = None,
        codex_hook_executable: Path | None = None,
        claude_settings: Path | None = None,
        claude_hook_executable: Path | None = None,
    ) -> dict[str, Any]:
        codex_changed = False
        codex_workspace_targets = 0
        codex_changed_paths = 0
        if codex_hooks is not None:
            hook_executable = codex_hook_executable or _hook_executable()
            (
                codex_changed,
                codex_workspace_targets,
                codex_changed_paths,
            ) = self._reconcile_codex(
                codex_hooks,
                codex_config=codex_config,
                hook_executable=hook_executable,
                enabled=True,
            )
            self.permission_store.set_codex_installed(True)
            self.permission_store.set_enabled("codex", True)
        claude_changed = False
        if claude_settings is not None:
            claude_changed = install_claude_hooks(
                claude_settings,
                claude_hook_executable or _claude_hook_executable(),
                self.paths.database,
                self.paths.permissions,
            )
            self.permission_store.set_claude_code_installed(True)
            self.permission_store.set_enabled("claude_code", True)
        if read_state(self.paths.state) is not None:
            self.stop()
        command = self.startup_command_builder(
            self.runtime_command_prefix, self.paths.config
        )
        self.startup.install(command)
        status = self.start()
        return {
            "updated": True,
            "running": status["running"],
            "runtime_build": self.runtime_identity.startup_build_sha256,
            "database_preserved": self.paths.database.exists(),
            "codex_hooks_changed": codex_changed,
            "codex_workspace_targets": codex_workspace_targets,
            "codex_hook_changed_paths": codex_changed_paths,
            "claude_hooks_changed": claude_changed,
        }

    def rotate_credential(self) -> dict[str, object]:
        if read_state(self.paths.state) is not None:
            self.stop()
        self.credential_store.rotate()
        status = self.start()
        return {"rotated": True, "running": status["running"]}

    def uninstall(
        self,
        *,
        codex_hooks: Path | None = None,
        codex_config: Path | None = None,
        codex_hook_executable: Path | None = None,
        claude_settings: Path | None = None,
        claude_hook_executable: Path | None = None,
    ) -> dict[str, object]:
        if read_state(self.paths.state) is not None:
            self.stop()
        codex_changed = False
        codex_workspace_targets = 0
        codex_changed_paths = 0
        codex_cleanup_warnings: list[str] = []
        if codex_hooks is not None:
            hook_executable = codex_hook_executable or _hook_executable()
            command = hook_command(hook_executable, self.paths.database)
            targets = [Path(codex_hooks)]
            if codex_config is not None:
                try:
                    discovered = CodexWorkspaceSource(
                        codex_config, global_hooks_path=codex_hooks
                    ).discover()
                except Exception as error:
                    codex_cleanup_warnings.append(
                        f"workspace_discovery:{type(error).__name__}"
                    )
                    discovered = ()
                for target in discovered:
                    try:
                        if codex_hook_file_has_atomizer_entries(
                            target.hooks_path, command
                        ):
                            targets.append(target.hooks_path)
                            codex_workspace_targets += 1
                    except Exception as error:
                        codex_cleanup_warnings.append(
                            f"workspace_target:{type(error).__name__}"
                        )
            unique_targets = {
                os.path.normcase(os.path.normpath(str(path.absolute()))): path
                for path in targets
            }
            for target in (unique_targets[key] for key in sorted(unique_targets)):
                try:
                    result = reconcile_codex_hook_targets(
                        (target,), command, enabled=False
                    )
                    codex_changed_paths += result.changed_paths
                except Exception as error:
                    codex_cleanup_warnings.append(
                        f"hook_target:{type(error).__name__}"
                    )
            codex_changed = codex_changed_paths > 0
        claude_changed = False
        claude_cleanup_warnings: list[str] = []
        if claude_settings is not None:
            try:
                claude_changed = remove_claude_hooks(
                    claude_settings,
                    claude_hook_executable or _claude_hook_executable(),
                    self.paths.database,
                    self.paths.permissions,
                )
            except Exception as error:
                claude_cleanup_warnings.append(
                    f"hook_target:{type(error).__name__}"
                )
        self.startup.remove()
        self.credential_store.remove()
        current_credential_store(
            self.paths.extension_credential,
            description="Context Atomizer Local extension pairing secret",
        ).remove()
        current_credential_store(
            self.paths.managed_credential,
            description="Context Atomizer Local managed connector secret",
        ).remove()
        self.paths.config.unlink(missing_ok=True)
        self.paths.permissions.unlink(missing_ok=True)
        self.paths.access_policy.unlink(missing_ok=True)
        self.paths.state.unlink(missing_ok=True)
        self.paths.lock.unlink(missing_ok=True)
        self.paths.library_shortcut.unlink(missing_ok=True)
        (self.paths.app_data / "capture-errors.log").unlink(missing_ok=True)
        for index in range(0, 11):
            candidate = self.paths.log if index == 0 else Path(str(self.paths.log) + f".{index}")
            candidate.unlink(missing_ok=True)
        try:
            self.paths.log.parent.rmdir()
        except FileNotFoundError:
            pass
        return {
            "uninstalled": True,
            "startup_registered": self.startup.read() is not None,
            "database_preserved": self.paths.database.exists(),
            "codex_hooks_changed": codex_changed,
            "codex_workspace_targets": codex_workspace_targets,
            "codex_hook_changed_paths": codex_changed_paths,
            "codex_cleanup_complete": not codex_cleanup_warnings,
            "codex_cleanup_warnings": tuple(codex_cleanup_warnings),
            "claude_hooks_changed": claude_changed,
            "claude_cleanup_complete": not claude_cleanup_warnings,
            "claude_cleanup_warnings": tuple(claude_cleanup_warnings),
        }

    def open_library(self) -> bool:
        status = self.status()
        if not status["running"]:
            status = self.start()
        token = self.credential_store.load()
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(status['bridge_port'])}/v1/library/launch",
            data=b"{}",
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read())
        url = payload.get("url")
        if not isinstance(url, str) or not url.startswith("http://127.0.0.1:"):
            raise RuntimeError("runtime returned an invalid Library launch capability")
        return self.url_opener(url)


def _manager() -> LifecycleManager:
    if sys.platform == "darwin":
        from atomizer_local_client.platforms.macos.browser import open_url
        from atomizer_local_client.platforms.macos.launch_agent import (
            MacOSLaunchAgentRegistration,
            runtime_startup_command as macos_runtime_startup_command,
        )

        return LifecycleManager(
            RuntimePaths.current_user(),
            startup=MacOSLaunchAgentRegistration(),
            runtime_command_prefix=_runtime_command_prefix(),
            startup_command_builder=macos_runtime_startup_command,
            url_opener=open_url,
        )
    return LifecycleManager(
        RuntimePaths.current_user(),
        startup=WindowsRunRegistration(),
        runtime_command_prefix=_runtime_command_prefix(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Context Atomizer Local")
    parser.add_argument(
        "command",
        choices=(
            "install",
            "update",
            "uninstall",
            "start",
            "stop",
            "restart",
            "rotate-credential",
            "status",
            "open-library",
        ),
    )
    parser.add_argument("--enable-codex", action="store_true")
    parser.add_argument("--enable-claude", action="store_true")
    parser.add_argument("--enable-chatgpt", action="store_true")
    parser.add_argument("--codex-hooks", type=Path, default=None)
    parser.add_argument("--codex-config", type=Path, default=None)
    parser.add_argument("--claude-settings", type=Path, default=None)
    arguments = parser.parse_args()
    manager = _manager()
    codex_path = arguments.codex_hooks
    if arguments.enable_codex and codex_path is None:
        codex_path = Path.cwd() / ".codex" / "hooks.json"
    codex_config = arguments.codex_config
    if codex_path is not None and codex_config is None:
        codex_config = codex_path.parent / "config.toml"
    claude_settings = arguments.claude_settings
    if arguments.enable_claude and claude_settings is None:
        claude_settings = Path.home() / ".claude" / "settings.json"
    if arguments.command == "install":
        result = manager.install(
            codex_hooks=codex_path,
            codex_config=codex_config,
            claude_settings=claude_settings,
            chatgpt_enabled=True if arguments.enable_chatgpt else None,
        )
    elif arguments.command == "update":
        result = manager.update(
            codex_hooks=codex_path,
            codex_config=codex_config,
            claude_settings=claude_settings,
        )
    elif arguments.command == "uninstall":
        result = manager.uninstall(
            codex_hooks=codex_path,
            codex_config=codex_config,
            claude_settings=claude_settings,
        )
    elif arguments.command == "start":
        result = manager.start()
    elif arguments.command == "stop":
        result = {"stopped": manager.stop()}
    elif arguments.command == "restart":
        result = manager.restart()
    elif arguments.command == "rotate-credential":
        result = manager.rotate_credential()
    elif arguments.command == "status":
        result = manager.status()
    else:
        result = {"opened": manager.open_library()}
    print(json.dumps(result, indent=2, sort_keys=True))
    if arguments.command == "uninstall" and not result.get("codex_cleanup_complete", True):
        return 2
    if arguments.command == "uninstall" and not result.get("claude_cleanup_complete", True):
        return 2
    return 0


def open_library_main() -> int:
    _manager().open_library()
    return 0


def _message_box(message: str, *, error: bool = False) -> None:
    if os.name == "nt":
        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(None, message, "Context Atomizer Local", flags)


def setup_main() -> int:
    try:
        _manager().install()
    except Exception:
        _message_box("Installation could not be completed. See the bounded local runtime log.", error=True)
        return 1
    _message_box("Context Atomizer Local is installed and running.")
    return 0


def uninstall_main() -> int:
    try:
        result = _manager().uninstall()
    except Exception:
        _message_box("Uninstall could not be completed. The Library database was not deleted.", error=True)
        return 1
    if not result.get("codex_cleanup_complete", True):
        _message_box(
            "Core runtime state was removed and the Library database was preserved, "
            "but one ambiguous Codex hook was left unchanged.",
            error=True,
        )
        return 2
    _message_box("Runtime integration was removed. Your Library database was preserved.")
    return 0


def restart_main() -> int:
    try:
        _manager().restart()
    except Exception:
        _message_box("Restart could not be completed. See the bounded local runtime log.", error=True)
        return 1
    _message_box("Context Atomizer Local restarted successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
