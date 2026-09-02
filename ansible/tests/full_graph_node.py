#!/usr/bin/env python3
"""The node side of a full acceptance run, faked at the network boundary only.

Acceptance reaches the node over three ports: 443 for the public selfsteal site
(TLS, real certificate, the real generated decoy), the RemnaNode port, and - for
the end-to-end probe - a destination to fetch.  Everything the acceptance role
does with those responses is real; only the services answering them are local.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import socket
import socketserver
import ssl
import threading


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return


class NoContent(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


class Reuse(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve_https(root: str, port: int, cert: str, key: str) -> None:
    handler = functools.partial(Quiet, directory=root)
    httpd = Reuse(("0.0.0.0", port), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    httpd.serve_forever()


def serve_204(port: int) -> None:
    Reuse(("127.0.0.1", port), NoContent).serve_forever()


def serve_plain(port: int) -> None:
    """The RemnaNode port: acceptance only requires that something listens."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", port))
    listener.listen(16)
    while True:
        connection, _ = listener.accept()
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--webroot", required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--https-port", type=int, default=443)
    parser.add_argument("--node-port", type=int, required=True)
    parser.add_argument("--probe-target-port", type=int, required=True)
    args = parser.parse_args()

    for target, arguments in (
        (serve_https, (args.webroot, args.https_port, args.cert, args.key)),
        (serve_204, (args.probe_target_port,)),
        (serve_plain, (args.node_port,)),
    ):
        threading.Thread(target=target, args=arguments, daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    main()
