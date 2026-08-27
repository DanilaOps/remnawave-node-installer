from __future__ import annotations

import argparse
import json
import pathlib
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class Store:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {
                "profiles": [],
                "nodes": [],
                "hosts": [],
                "squads": [],
                "users": [],
                "keygen_calls": 0,
            }
        self.data.setdefault("users", [])
        self.data.setdefault("keygen_calls", 0)

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    @staticmethod
    def new_uuid() -> str:
        return str(uuid.uuid4())

    def profile_response(self, profile: dict[str, Any]) -> dict[str, Any]:
        config = json.loads(json.dumps(profile["config"]))
        for inbound in config.get("inbounds", []):
            inbound.setdefault("uuid", self.new_uuid())
        # Persist generated inbound UUIDs so a second run sees stable identity.
        profile["config"] = config
        return {
            "uuid": profile["uuid"],
            "name": profile["name"],
            "config": config,
            "inbounds": config.get("inbounds", []),
        }


class Handler(BaseHTTPRequestHandler):
    store: Store

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def send_json(self, code: int, payload: Any) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except BrokenPipeError:
            pass

    def inject_failure(self) -> bool:
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if token == "mock-timeout":
            time.sleep(2)
        status = {"mock-401": 401, "mock-403": 403, "mock-500": 500}.get(token)
        if status:
            self.send_json(status, {"message": f"injected HTTP {status}"})
            return True
        return False

    def find(self, collection: str, resource_uuid: str) -> dict[str, Any] | None:
        return next(
            (item for item in self.store.data[collection] if item["uuid"] == resource_uuid),
            None,
        )

    def do_GET(self) -> None:  # noqa: N802
        if self.inject_failure():
            return
        path = self.path.rstrip("/")
        if path == "/api/keygen":
            self.store.data["keygen_calls"] += 1
            secret = f"mock-node-secret-{self.store.data['keygen_calls']}"
            self.store.save()
            self.send_json(200, {"response": {"secretKey": secret}})
            return
        if path == "/api/config-profiles":
            profiles = [self.store.profile_response(item) for item in self.store.data["profiles"]]
            self.store.save()
            self.send_json(200, {"response": {"configProfiles": profiles}})
            return
        if path.startswith("/api/config-profiles/"):
            item = self.find("profiles", path.rsplit("/", 1)[-1])
            if item:
                response = self.store.profile_response(item)
                self.store.save()
                self.send_json(200, {"response": response})
            else:
                self.send_json(404, {"message": "not found"})
            return
        if path == "/api/nodes":
            self.send_json(200, {"response": self.store.data["nodes"]})
            return
        if path.startswith("/api/nodes/"):
            item = self.find("nodes", path.rsplit("/", 1)[-1])
            self.send_json(200 if item else 404, {"response": item} if item else {"message": "not found"})
            return
        if path == "/api/hosts":
            self.send_json(200, {"response": self.store.data["hosts"]})
            return
        if path == "/api/internal-squads":
            self.send_json(200, {"response": {"internalSquads": self.store.data["squads"]}})
            return
        if path.startswith("/api/internal-squads/"):
            item = self.find("squads", path.rsplit("/", 1)[-1])
            self.send_json(200 if item else 404, {"response": item} if item else {"message": "not found"})
            return
        if path.startswith("/api/users/by-username/"):
            username = path.rsplit("/", 1)[-1]
            item = next(
                (user for user in self.store.data["users"] if user["username"] == username),
                None,
            )
            self.send_json(200 if item else 404, {"response": item} if item else {"message": "not found"})
            return
        self.send_json(404, {"message": "not found"})

    def create(self, collection: str, body: dict[str, Any]) -> dict[str, Any]:
        item = {"uuid": self.store.new_uuid(), **body}
        if collection == "users":
            item["id"] = len(self.store.data["users"]) + 1
        if collection == "profiles":
            response = self.store.profile_response(item)
        else:
            response = item
        self.store.data[collection].append(item)
        self.store.save()
        return response

    def update(self, collection: str, body: dict[str, Any]) -> dict[str, Any] | None:
        item = self.find(collection, body.get("uuid", ""))
        if not item:
            return None
        item.update({key: value for key, value in body.items() if key != "uuid"})
        self.store.save()
        return self.store.profile_response(item) if collection == "profiles" else item

    def do_POST(self) -> None:  # noqa: N802
        if self.inject_failure():
            return
        body = self.body()
        if self.path.rstrip("/") == "/api/config-profiles" and body.get("name") == "CONFLICT":
            self.send_json(409, {"message": "injected profile conflict"})
            return
        if self.path.rstrip("/") == "/api/hosts" and any(
            not re.fullmatch(r"[A-Z0-9_:]+", tag) for tag in body.get("tags", [])
        ):
            self.send_json(400, {"message": "Host tags must be uppercase"})
            return
        if self.path.rstrip("/") == "/api/users":
            if not body.get("username") or not body.get("ssPassword"):
                self.send_json(400, {"message": "username and ssPassword are required"})
                return
            self.send_json(201, {"response": self.create("users", body)})
            return
        mapping = {
            "/api/config-profiles": "profiles",
            "/api/nodes": "nodes",
            "/api/hosts": "hosts",
            "/api/internal-squads": "squads",
        }
        collection = mapping.get(self.path.rstrip("/"))
        if not collection:
            self.send_json(404, {"message": "not found"})
            return
        self.send_json(201, {"response": self.create(collection, body)})

    def do_PATCH(self) -> None:  # noqa: N802
        if self.inject_failure():
            return
        body = self.body()
        if self.path.rstrip("/") == "/api/users":
            if "username" not in body and "id" not in body:
                self.send_json(400, {"message": "username or numeric id is required"})
                return
            user = next(
                (
                    item
                    for item in self.store.data["users"]
                    if item.get("username") == body.get("username")
                    or item.get("id") == body.get("id")
                ),
                None,
            )
            if not user:
                self.send_json(404, {"message": "not found"})
                return
            user.update({key: value for key, value in body.items() if key not in {"username", "id"}})
            self.store.save()
            self.send_json(200, {"response": user})
            return
        if self.path.rstrip("/") == "/api/hosts" and any(
            not re.fullmatch(r"[A-Z0-9_:]+", tag) for tag in body.get("tags", [])
        ):
            self.send_json(400, {"message": "Host tags must be uppercase"})
            return
        mapping = {
            "/api/config-profiles": "profiles",
            "/api/nodes": "nodes",
            "/api/hosts": "hosts",
            "/api/internal-squads": "squads",
        }
        collection = mapping.get(self.path.rstrip("/"))
        if not collection:
            self.send_json(404, {"message": "not found"})
            return
        response = self.update(collection, body)
        self.send_json(200 if response else 404, {"response": response})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--state", type=pathlib.Path, required=True)
    args = parser.parse_args()
    Handler.store = Store(args.state)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
