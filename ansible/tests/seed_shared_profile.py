"""Seed the mock panel with a Config Profile that already carries routing.

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
# Named nothing like the Xray JSON template below: a test that confuses a
# Config Profile with a subscription template must not be able to pass by
# accident because the two fixtures happen to share a word.
PROFILE_NAME = "Mock-Profile"
# A Subscription Template of type XRAY_JSON - the panel's "Xray JSON template".
# Deliberately named nothing like a Config Profile, so a test that confuses the
# two cannot accidentally pass.
XRAY_TEMPLATE_UUID = "33333333-3333-3333-3333-333333333333"
XRAY_TEMPLATE_NAME = "Mock Xray Template"

STATE = {
    "profiles": [
        {
            "uuid": PROFILE_UUID,
            "name": PROFILE_NAME,
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
    "templates": [
        {
            "uuid": XRAY_TEMPLATE_UUID,
            "viewPosition": 1,
            "name": XRAY_TEMPLATE_NAME,
            "templateType": "XRAY_JSON",
            "templateJson": {"outbounds": []},
            "encodedTemplateYaml": None,
        },
        {
            # Same name, different type: the lookup must not accept it.
            "uuid": "44444444-4444-4444-4444-444444444444",
            "viewPosition": 2,
            "name": XRAY_TEMPLATE_NAME,
            "templateType": "MIHOMO",
            "templateJson": None,
            "encodedTemplateYaml": "",
        },
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
