"""macOS loopback HTTP servers that do not perform reverse DNS at bind time."""

from __future__ import annotations

import socketserver

from atomizer_local_client.bridge.local_ingress import LocalIngressServer
from atomizer_local_client.ui.library_server import LibraryViewServer


def _bind_numeric_loopback(server: LocalIngressServer | LibraryViewServer) -> None:
    socketserver.TCPServer.server_bind(server)
    host, port = server.server_address[:2]
    server.server_name = str(host)
    server.server_port = int(port)


class MacOSLocalIngressServer(LocalIngressServer):
    """Bind the fixed loopback bridge without a blocking hostname lookup."""

    def server_bind(self) -> None:
        _bind_numeric_loopback(self)


class MacOSLibraryViewServer(LibraryViewServer):
    """Bind the loopback Library without a blocking hostname lookup."""

    def server_bind(self) -> None:
        _bind_numeric_loopback(self)
