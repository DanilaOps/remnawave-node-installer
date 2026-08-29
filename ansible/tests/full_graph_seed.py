#!/usr/bin/env python3
"""Seeds the mock panel with the state a reconciled node leaves behind.

Shaped like the live panel on purpose: the Config Profile carries the Xray JSON
under "config", and the panel's inbound index - which is what the mock derives
from it - nests the same inbound under rawInbound.  Acceptance has to resolve
its Reality settings out of that, which is exactly what a flat fixture stopped
CI from ever exercising.
"""
from __future__ import annotations

import json
import pathlib
import sys

PROFILE_UUID = "11111111-1111-4111-8111-111111111111"
INBOUND_UUID = "22222222-2222-4222-8222-222222222222"
NODE_UUID = "33333333-3333-4333-8333-333333333333"
HOST_UUID = "44444444-4444-4444-8444-444444444444"
SQUAD_UUID = "55555555-5555-4555-8555-555555555555"
TEMPLATE_UUID = "66666666-6666-4666-8666-666666666666"
PROBE_VLESS_UUID = "77777777-7777-4777-8777-777777777777"
PRIVATE_KEY = "wJlBBQBhBzWJqPqzHJDF3fRhH0dGZ8VqDsxlHXbW0Fo"
SHORT_ID = "0011223344556677"


def state(domain: str, address: str, remark: str, xray_version: str) -> dict:
    inbound = {
        "uuid": INBOUND_UUID,
        "tag": "DE_01_REALITY",
        "port": 443,
        "listen": "0.0.0.0",
        "protocol": "vless",
        "settings": {"clients": [], "decryption": "none"},
        "streamSettings": {
            "network": "raw",
            "security": "reality",
            "realitySettings": {
                "privateKey": PRIVATE_KEY,
                "shortIds": [SHORT_ID],
                "serverNames": [domain],
                "dest": f"{domain}:443",
                "spiderX": "/",
            },
        },
    }
    return {
        "profiles": [
            {
                "uuid": PROFILE_UUID,
                "name": "DE-01",
                "config": {
                    "log": {"loglevel": "warning"},
                    "inbounds": [inbound],
                    "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
                    "routing": {"rules": [{"type": "field", "protocol": ["bittorrent"],
                                           "outboundTag": "BLOCK"}]},
                },
            }
        ],
        "nodes": [
            {
                "uuid": NODE_UUID,
                "name": "DE-01",
                "address": address,
                "isConnected": True,
                "xrayUptime": 4242,
                "versions": {"xray": xray_version, "node": "3.3.2"},
                "configProfile": {
                    "activeConfigProfileUuid": PROFILE_UUID,
                    "activeInbounds": [{"uuid": INBOUND_UUID}],
                },
            }
        ],
        "hosts": [
            {
                "uuid": HOST_UUID,
                "remark": remark,
                "address": address,
                "port": 443,
                "sni": domain,
                "fingerprint": "firefox",
                "securityLayer": "DEFAULT",
                "isHidden": False,
                "isDisabled": False,
                "tags": ["DE", "DIRECT"],
                "xrayJsonTemplateUuid": TEMPLATE_UUID,
                "nodes": [{"uuid": NODE_UUID}],
                "inbound": {
                    "configProfileUuid": PROFILE_UUID,
                    "configProfileInboundUuid": INBOUND_UUID,
                },
            }
        ],
        "squads": [
            {"uuid": SQUAD_UUID, "name": "Default", "inbounds": [{"uuid": INBOUND_UUID}]}
        ],
        "templates": [
            {"uuid": TEMPLATE_UUID, "name": "Mock Xray Template", "templateType": "XRAY_JSON"}
        ],
        "users": [
            {
                "id": 1,
                "username": "probe",
                "status": "ACTIVE",
                "vlessUuid": PROBE_VLESS_UUID,
                "shortUuid": "probe-short",
                "activeInternalSquads": [{"uuid": SQUAD_UUID, "name": "Default"}],
            }
        ],
        "keygen_calls": 0,
    }


def main() -> None:
    path, domain, address, remark, xray_version = sys.argv[1:6]
    pathlib.Path(path).write_text(
        json.dumps(state(domain, address, remark, xray_version), indent=2), encoding="utf-8"
    )
    print(json.dumps({"privateKey": PRIVATE_KEY, "shortId": SHORT_ID,
                      "vlessUuid": PROBE_VLESS_UUID, "inboundUuid": INBOUND_UUID,
                      "nodeUuid": NODE_UUID, "hostUuid": HOST_UUID}))


if __name__ == "__main__":
    main()
