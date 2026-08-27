"""Seed the mock panel with a shared Config Profile that already carries routing.

The shared profile mirrors production: it holds another node's inbound (with its
own Reality key) plus the routing rules every published Host depends on.  A node
must add its own inbound to it and leave everything else untouched.
"""

from __future__ import annotations

import json
import pathlib
import sys

PROFILE_UUID = "11111111-1111-1111-1111-111111111111"
FOREIGN_INBOUND_UUID = "22222222-2222-2222-2222-222222222222"
FOREIGN_PRIVATE_KEY = "FOREIGN-NODE-PRIVATE-KEY-MUST-NOT-BE-REUSED"

STATE = {
    "profiles": [
        {
            "uuid": PROFILE_UUID,
            "name": "Default August",
            "config": {
                "log": {"loglevel": "warning"},
                "inbounds": [
                    {
                        "uuid": FOREIGN_INBOUND_UUID,
                        "tag": "OTHER_NODE_REALITY",
                        "port": 443,
                        "listen": "0.0.0.0",
                        "protocol": "vless",
                        "settings": {"clients": [], "decryption": "none"},
                        "streamSettings": {
                            "network": "raw",
                            "security": "reality",
                            "realitySettings": {
                                "privateKey": FOREIGN_PRIVATE_KEY,
                                "shortIds": ["aabbccdd"],
                                "serverNames": ["other.example.test"],
                            },
                        },
                    }
                ],
                "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
                "routing": {
                    "rules": [
                        {
                            "type": "field",
                            "protocol": ["bittorrent"],
                            "outboundTag": "BLOCK",
                        },
                        {"type": "field", "network": "tcp,udp", "outboundTag": "DIRECT"},
                    ]
                },
            },
        }
    ],
    "nodes": [],
    "hosts": [],
    "squads": [],
    "users": [],
    "keygen_calls": 0,
}


def main() -> None:
    pathlib.Path(sys.argv[1]).write_text(json.dumps(STATE, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
