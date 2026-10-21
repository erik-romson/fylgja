"""A loopback HTTP server for tests that stand in for the daemon or another HTTP peer."""

from __future__ import annotations

import socketserver
from http.server import ThreadingHTTPServer


class LoopbackHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` without the reverse DNS lookup of its own address.

    ``HTTPServer.server_bind`` calls ``socket.getfqdn`` on the bound host, which can block for
    longer than the test timeout on a CI runner. The tests bind ``127.0.0.1`` and never read the
    name, so the host string serves as it.
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port
