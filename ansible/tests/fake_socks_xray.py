#!/usr/bin/env python3
"""Stand-in for the Xray client, so the probe's plumbing can be tested offline.

It takes the same arguments the real client takes (`run -c <config>`), reads the
SOCKS port out of that config - which also proves the rendered configuration puts
the listener where the role expects it - and serves a minimal SOCKS5 CONNECT
proxy on it. Everything around the tunnel is therefore exercised for real:
rendering, startup, waiting for the port, the request through the proxy, the
status assertion and the teardown. Only the Reality handshake itself is absent,
because that needs a node.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import selectors
import socket
import struct
import threading


def pipe(source: socket.socket, sink: socket.socket) -> None:
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            sink.sendall(data)
    except OSError:
        pass
    finally:
        for handle in (source, sink):
            try:
                handle.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            handle.close()


def handle(client: socket.socket) -> None:
    try:
        greeting = client.recv(262)
        if len(greeting) < 2 or greeting[0] != 0x05:
            client.close()
            return
        client.sendall(b"\x05\x00")

        header = client.recv(4)
        if len(header) < 4 or header[1] != 0x01:  # CONNECT only
            client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
            client.close()
            return
        address_type = header[3]
        if address_type == 0x01:
            host = socket.inet_ntoa(client.recv(4))
        elif address_type == 0x03:
            length = client.recv(1)[0]
            host = client.recv(length).decode()
        else:
            client.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
            client.close()
            return
        port = struct.unpack("!H", client.recv(2))[0]

        upstream = socket.create_connection((host, port), timeout=10)
        client.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
    except OSError:
        try:
            client.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
        except OSError:
            pass
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", default="run")
    parser.add_argument("-c", "--config", type=pathlib.Path, required=True)
    arguments = parser.parse_args()

    config = json.loads(arguments.config.read_text(encoding="utf-8"))
    port = int(config["inbounds"][0]["port"])

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(16)

    selector = selectors.DefaultSelector()
    selector.register(listener, selectors.EVENT_READ)
    while True:
        for _key, _mask in selector.select(timeout=60):
            client, _address = listener.accept()
            threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
