"""Minimal stateful stand-in for the REG.RU API v2 DNS endpoints.

Speaks the documented shape: POST <base>/<method> with an `input_data` form
field carrying a JSON document, and a `{"result": ..., "answer": ...}` envelope
in return.  It exists so the reconciliation logic - create, retarget, leave
alone, refuse to guess - can be tested without touching a registrar.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class Store:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"zones": {}}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def zone(self, dname: str) -> list[dict[str, Any]] | None:
        return self.data["zones"].get(dname)


class Handler(BaseHTTPRequestHandler):
    store: Store

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def send_json(self, payload: Any, code: int = 200) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def request_data(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        return json.loads(form.get("input_data", ["{}"])[0])

    def do_POST(self) -> None:  # noqa: N802
        data = self.request_data()
        if not data.get("username") or not data.get("password"):
            self.send_json({"result": "error", "error_code": "NO_AUTH"})
            return
        dname = (data.get("domains") or [{}])[0].get("dname", "")
        records = self.store.zone(dname)
        if records is None:
            self.send_json({"result": "error", "error_code": "DOMAIN_NOT_FOUND"})
            return

        method = self.path.rstrip("/").rsplit("/", 1)[-1]
        if method == "get_resource_records":
            self.send_json(
                {
                    "result": "success",
                    "answer": {
                        "domains": [
                            {"dname": dname, "result": "success", "rrs": records}
                        ]
                    },
                }
            )
            return
        if method == "add_alias":
            records.append(
                {
                    "rectype": "A",
                    "subname": data.get("subdomain", "@"),
                    "content": data.get("ipaddr", ""),
                    "state": "verified",
                }
            )
            self.store.save()
            self.send_json({"result": "success", "answer": {"domains": [{"dname": dname, "result": "success"}]}})
            return
        if method == "remove_record":
            wanted = (
                data.get("subdomain", "@"),
                data.get("record_type", "A"),
                data.get("content", ""),
            )
            remaining = [
                record
                for record in records
                if (record["subname"], record["rectype"], record["content"]) != wanted
            ]
            if len(remaining) == len(records):
                self.send_json({"result": "error", "error_code": "RR_NOT_FOUND"})
                return
            self.store.data["zones"][dname] = remaining
            self.store.save()
            self.send_json({"result": "success", "answer": {"domains": [{"dname": dname, "result": "success"}]}})
            return
        self.send_json({"result": "error", "error_code": "NO_SUCH_COMMAND"}, code=404)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18083)
    parser.add_argument("--state", type=pathlib.Path, required=True)
    args = parser.parse_args()
    Handler.store = Store(args.state)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
