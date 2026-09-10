#!/usr/bin/env python3
"""Stands in for the Xray client the end-to-end probe starts.

The transport is the one thing a sandbox cannot reproduce: carrying real
VLESS/Reality traffic needs a real node.  Everything else about the probe is
real and is checked here rather than assumed - this stub refuses to start
unless the configuration acceptance rendered carries a complete, non-placeholder
identity, and it writes what it was given so the test can compare it against
what the panel published.  Then it opens the SOCKS port the probe expects and
forwards, so the request, its status code and every assertion around it run for
real.
"""
from __future__ import annotations

import json
import os
import pathlib
import select
import socket
import socketserver
import sys
import threading

REQUIRED = ("publicKey", "shortId", "serverName", "fingerprint")


def validate(config: dict) -> dict:
    outbound = config["outbounds"][0]
    vnext = outbound["settings"]["vnext"][0]
    reality = outbound["streamSettings"]["realitySettings"]
    missing = [field for field in REQUIRED if not str(reality.get(field, "")).strip()]
    if not str(vnext["users"][0].get("id", "")).strip():
        missing.append("id")
    if not str(vnext.get("address", "")).strip():
        missing.append("address")
    if not int(vnext.get("port", 0)):
        missing.append("port")
    if missing:
        sys.stderr.write(f"probe configuration is incomplete: {', '.join(missing)}\n")
        raise SystemExit(2)
    return {
        "address": vnext["address"],
        "port": vnext["port"],
        "vlessUuid": vnext["users"][0]["id"],
        "flow": vnext["users"][0].get("flow", ""),
        "serverName": reality["serverName"],
        "publicKey": reality["publicKey"],
        "shortId": reality["shortId"],
        "fingerprint": reality["fingerprint"],
        "socksPort": config["inbounds"][0]["port"],
    }


class Socks5(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client = self.request
        greeting = client.recv(262)
        if not greeting or greeting[0] != 5:
            return
        client.sendall(b"\x05\x00")
        header = client.recv(4)
        if len(header) < 4 or header[1] != 1:
            return
        atyp = header[3]
        if atyp == 1:
            host = socket.inet_ntoa(client.recv(4))
        elif atyp == 3:
            host = client.recv(client.recv(1)[0]).decode()
        else:
            client.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
            return
        port = int.from_bytes(client.recv(2), "big")
        try:
            upstream = socket.create_connection((host, port), 10)
        except OSError:
            client.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
            return
        client.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        sockets = [client, upstream]
        while True:
            readable, _, _ = select.select(sockets, [], [], 30)
            if not readable:
                break
            for source in readable:
                other = upstream if source is client else client
                data = source.recv(65536)
                if not data:
                    return
                other.sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    arguments = sys.argv[1:]
    if not arguments or arguments[0] != "run" or "-c" not in arguments:
        sys.stderr.write("usage: xray run -c <config>\n")
        raise SystemExit(64)
    config_path = pathlib.Path(arguments[arguments.index("-c") + 1])
    identity = validate(json.loads(config_path.read_text()))
    record = os.environ.get("FULL_GRAPH_PROBE_RECORD")
    if record:
        pathlib.Path(record).write_text(json.dumps(identity, indent=2))
    server = Server(("127.0.0.1", int(identity["socksPort"])), Socks5)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    main()
