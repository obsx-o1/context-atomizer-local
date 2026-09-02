"""One normal local process hosting the existing bridge and Library components."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from atomizer_local_client.bridge.local_ingress import LocalIngressServer
from atomizer_local_client.history.connection import database
from atomizer_local_client.local_auth.library_session import LibrarySessionAuthority
from atomizer_local_client.local_auth.pairing import ExtensionPairingAuthority
from atomizer_local_client.runtime.configuration import (
    RuntimeConfig,
    RuntimePaths,
    remove_state,
    write_json,
)
from atomizer_local_client.platforms.credentials import current_credential_store
from atomizer_local_client.runtime.logging_setup import (
    close_runtime_logging,
    configure_runtime_logging,
)
from atomizer_local_client.runtime_health import RuntimeIdentity
from atomizer_local_client.managed_access.authority import ManagedAuthorityRegistry
from atomizer_local_client.managed_access.broker import ManagedContextBroker
from atomizer_local_client.managed_access.capability import (
    AuthenticatedManagedAssertionVerifier,
)
from atomizer_local_client.managed_access.ingress import ManagedIngress
from atomizer_local_client.managed_access.policy import (
    LibraryAccessPolicyStore,
    PolicyBackedAccessGate,
)
from atomizer_local_client.managed_access.reader import ManagedLibraryReader
from atomizer_local_client.memory_access.query_service import LibraryQueryService
from atomizer_local_client.runtime.permissions import PermissionStore
from atomizer_local_client.ui.library_server import LibraryViewServer


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class CredentialProvider(Protocol):
    def load_or_create(self) -> str: ...


class RuntimeAlreadyRunning(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._stream: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        stream.seek(0)
        if stream.read(1) == b"":
            stream.seek(0)
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeAlreadyRunning("Atomizer Local is already running") from exc
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._stream = None


class ExtensionConnectionState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._protocol_version: str | None = None
        self._last_seen_at: str | None = None

    def seen(self, protocol_version: str) -> None:
        with self._lock:
            self._protocol_version = protocol_version[:32] or None
            self._last_seen_at = _utc_now()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "state": "connected" if self._last_seen_at else "not_seen",
                "protocol_version": self._protocol_version,
                "last_seen_at": self._last_seen_at,
            }


class AtomizerLocalRuntime:
    def __init__(
        self,
        paths: RuntimePaths,
        config: RuntimeConfig,
        *,
        credential_store: CredentialProvider | None = None,
        runtime_identity: RuntimeIdentity | None = None,
        _test_bridge_port: int | None = None,
    ) -> None:
        config.validate()
        self.paths = paths
        self.config = config
        self.credential_store = credential_store or current_credential_store(
            paths.credential
        )
        self.runtime_identity = runtime_identity or RuntimeIdentity()
        self._test_bridge_port = _test_bridge_port
        self.extension_state = ExtensionConnectionState()
        self.permission_store = PermissionStore(paths.permissions)
        self.pairing_authority = ExtensionPairingAuthority(
            current_credential_store(
                paths.extension_credential,
                description="Context Atomizer Local extension pairing secret",
            )
        )
        self.managed_pairing_authority = ExtensionPairingAuthority(
            current_credential_store(
                paths.managed_credential,
                description="Context Atomizer Local managed connector secret",
            )
        )
        self.library_sessions = LibrarySessionAuthority()
        self.access_policy = LibraryAccessPolicyStore(paths.access_policy)
        self.managed_authority = ManagedAuthorityRegistry(
            self.runtime_identity.startup_build_sha256
        )
        self.managed_broker = ManagedContextBroker(self.managed_authority)
        managed_gate = PolicyBackedAccessGate(
            self.access_policy, manager=self.managed_authority
        )
        managed_queries = LibraryQueryService(paths.database, gate=managed_gate)
        self.managed_ingress = ManagedIngress(
            policy=self.access_policy,
            authority=self.managed_authority,
            broker=self.managed_broker,
            reader=ManagedLibraryReader(
                paths.database, managed_queries, self.managed_authority
            ),
            verifier=AuthenticatedManagedAssertionVerifier(
                self.managed_pairing_authority.secret,
                runtime_instance_reference=(
                    self.managed_authority.runtime_instance_reference
                ),
            ),
        )
        self.bridge_server: LocalIngressServer | None = None
        self.library_server: LibraryViewServer | None = None
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._instance_lock = SingleInstanceLock(paths.lock)
        self._token: str | None = None
        self._owns_instance = False
        self.logger = configure_runtime_logging(
            paths.log,
            max_bytes=config.log_max_bytes,
            backup_count=config.log_backup_count,
        )

    @staticmethod
    def _port_candidates(preferred: int, span: int) -> tuple[int, ...]:
        return tuple(range(preferred, min(65535, preferred + span) + 1))

    def _bind_bridge(self) -> LocalIngressServer:
        server_type = LocalIngressServer
        if sys.platform == "darwin":
            from atomizer_local_client.platforms.macos.loopback_servers import (
                MacOSLocalIngressServer,
            )

            server_type = MacOSLocalIngressServer
        try:
            return server_type(
                self.paths.database,
                str(self._token),
                self.pairing_authority,
                self.config.bridge_port,
                runtime_identity=self.runtime_identity,
                runtime_stop_callback=self.request_stop,
                library_launch_provider=self._library_launch_url,
                management_status_provider=self._management_status,
                extension_seen_callback=self.extension_state.seen,
                integration_enabled=self.permission_store.is_enabled,
                managed_ingress=self.managed_ingress,
                managed_pairing_authority=self.managed_pairing_authority,
                _test_port=self._test_bridge_port,
            )
        except OSError as exc:
            raise RuntimeError(
                "capture bridge port 43117 is unavailable; startup failed closed"
            ) from exc

    def _bind_library(self, bridge_port: int) -> LibraryViewServer:
        server_type = LibraryViewServer
        if sys.platform == "darwin":
            from atomizer_local_client.platforms.macos.loopback_servers import (
                MacOSLibraryViewServer,
            )

            server_type = MacOSLibraryViewServer
        last_error: OSError | None = None
        for port in self._port_candidates(self.config.library_port, self.config.port_span):
            if port == bridge_port:
                continue
            try:
                return server_type(
                    self.paths.database,
                    port,
                    maintenance_interval_seconds=self.config.maintenance_interval_seconds,
                    bridge_port=bridge_port,
                    runtime_identity=self.runtime_identity,
                    extension_status_provider=self._extension_status,
                    permission_store=self.permission_store,
                    session_authority=self.library_sessions,
                    pairing_code_provider=self.pairing_authority.issue_code,
                    pairing_revoke_callback=self.pairing_authority.revoke,
                    access_policy=self.access_policy,
                    managed_status_provider=self._managed_status,
                    managed_pairing_code_provider=(
                        self.managed_pairing_authority.issue_code
                    ),
                    managed_pairing_revoke_callback=self._revoke_managed_pairing,
                    access_mode_setter=self._set_access_mode,
                )
            except OSError as exc:
                last_error = exc
        raise RuntimeError("no Library port is available in the configured bounded range") from last_error

    def _extension_status(self) -> dict[str, object]:
        return {
            **self.extension_state.snapshot(),
            "paired": self.pairing_authority.paired,
        }

    def _managed_status(self) -> dict[str, object]:
        return {
            **self.managed_authority.status(),
            "paired": self.managed_pairing_authority.paired,
        }

    def _set_access_mode(self, mode: str):  # type: ignore[no-untyped-def]
        self.managed_authority.revoke()
        self.managed_broker.revoke()
        return self.access_policy.set_mode(mode)

    def _revoke_managed_pairing(self) -> None:
        self.managed_pairing_authority.revoke()
        self.managed_authority.revoke()
        self.managed_broker.revoke()

    def _library_launch_url(self) -> str:
        if self.library_server is None:
            raise RuntimeError("Library is not running")
        capability = self.library_sessions.issue_launch()
        port = int(self.library_server.server_address[1])
        return f"http://127.0.0.1:{port}/?launch={capability}"

    def _management_status(self) -> dict[str, object]:
        if self.library_server is None:
            return {"ok": False, "runtime_running": False}
        return self.library_server.health_snapshot()

    def start(self) -> dict[str, object]:
        self._instance_lock.acquire()
        self._owns_instance = True
        try:
            if sys.platform == "darwin":
                from atomizer_local_client.platforms.macos.permissions import (
                    ensure_private_directory,
                )

                ensure_private_directory(self.paths.app_data)
            else:
                self.paths.app_data.mkdir(parents=True, exist_ok=True)
            self._token = self.credential_store.load_or_create()
            with database(self.paths.database):
                pass
            self.bridge_server = self._bind_bridge()
            bridge_port = int(self.bridge_server.server_address[1])
            self.library_server = self._bind_library(bridge_port)
            for name, server in (
                ("bridge", self.bridge_server),
                ("library", self.library_server),
            ):
                thread = threading.Thread(
                    target=server.serve_forever,
                    kwargs={"poll_interval": 0.25},
                    name=f"atomizer-local-{name}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
            library_port = int(self.library_server.server_address[1])
            state = {
                "pid": os.getpid(),
                "started_at": _utc_now(),
                "bridge_port": bridge_port,
                "library_port": library_port,
                "runtime_version": self.runtime_identity.snapshot()["runtime_version"],
                "protocol_version": self.runtime_identity.snapshot()["protocol_version"],
                "runtime_build": self.runtime_identity.startup_build_sha256,
            }
            write_json(self.paths.state, state)
            self.logger.info(
                "runtime_started bridge_port=%d library_port=%d build=%s",
                bridge_port,
                library_port,
                self.runtime_identity.startup_build_sha256,
            )
            return state
        except BaseException as exc:
            self.logger.error("runtime_start_failed error_class=%s", type(exc).__name__)
            self.stop()
            raise

    def request_stop(self) -> None:
        self._stop_event.set()

    def wait(self) -> None:
        while not self._stop_event.wait(0.5):
            continue

    def stop(self) -> None:
        if not self._owns_instance:
            return
        self._stop_event.set()
        self.managed_authority.revoke()
        self.managed_broker.revoke()
        serving = bool(self._threads)
        for server in (self.library_server, self.bridge_server):
            if server is not None:
                if serving:
                    try:
                        server.shutdown()
                    except BaseException:
                        pass
                server.server_close()
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads.clear()
        self.library_server = None
        self.bridge_server = None
        try:
            remove_state(self.paths.state)
        except OSError as exc:
            self.logger.warning(
                "runtime_state_cleanup_deferred error_class=%s", type(exc).__name__
            )
        self._instance_lock.release()
        self._owns_instance = False
        self.logger.info("runtime_stopped")
        close_runtime_logging(self.logger)


def _paths_for_config(config_path: Path) -> RuntimePaths:
    candidate = Path(config_path).resolve()
    try:
        standard = RuntimePaths.current_user()
    except RuntimeError:
        standard = None
    if standard is not None and candidate == standard.config:
        return standard
    return RuntimePaths.for_root(candidate.parent)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Context Atomizer Local without a console")
    parser.add_argument("--config", required=True, type=Path)
    arguments = parser.parse_args()
    paths = _paths_for_config(arguments.config)
    config = RuntimeConfig.load(arguments.config)
    runtime = AtomizerLocalRuntime(paths, config)

    def stop_handler(signum: int, frame: object) -> None:
        del signum, frame
        runtime.request_stop()

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)
    try:
        runtime.start()
        runtime.wait()
    except RuntimeAlreadyRunning:
        return 0
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
