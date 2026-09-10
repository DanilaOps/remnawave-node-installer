#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собрать выводимую часть реестра нод из живых источников.

Ничего не изобретает: каждое поле берётся из панели, DNS или TLS-хендшейка, и
рядом с ним пишется, откуда оно взято. Поля, которые машинно не выводятся -
провайдер и тариф - сюда не попадают вовсе; их место в ручном слое.

Запускать на контроллере: у него есть доступ к панели, рабочие резолверы и
маршрут до нод. Вывод - один JSON на stdout или в файл.
"""
import json
import re
import ssl
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

# Ничего про конкретную установку здесь не зашито: репозиторий публичный, а
# адрес панели и её домен - это карта парка. Всё приходит аргументами.
TOKEN_FILE = "/tmp/.tok"
TLS_TIMEOUT = 8


def api(path, token, panel):
    req = urllib.request.Request(
        panel + path, headers={"Authorization": "Bearer " + token})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        return json.load(r)["response"]


def dig(name, rrtype, resolver):
    try:
        out = subprocess.run(
            ["dig", "+short", rrtype, name, "@" + resolver],
            capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    return [l.strip().rstrip(".") for l in out.splitlines() if l.strip()]


def cert_facts(address, sni):
    """Выписавший УЦ и срок годности - прямо с работающего 443."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((address, 443), timeout=TLS_TIMEOUT) as s:
            with ctx.wrap_socket(s, server_hostname=sni) as ts:
                der = ts.getpeercert(binary_form=True)
    except Exception as e:
        return {"error": type(e).__name__}
    try:
        txt = subprocess.run(
            ["openssl", "x509", "-inform", "der", "-noout", "-issuer",
             "-subject", "-enddate"],
            input=der, capture_output=True, timeout=15).stdout.decode()
    except Exception as e:
        return {"error": type(e).__name__}
    facts = {}
    for line in txt.splitlines():
        if line.startswith("issuer="):
            facts["issuer"] = line.split("=", 1)[1].strip()
            m = re.search(r"O\s*=\s*([^,/]+)", facts["issuer"])
            if m:
                facts["ca"] = m.group(1).strip()
        elif line.startswith("subject="):
            m = re.search(r"CN\s*=\s*([^,/]+)", line)
            if m:
                facts["subject_cn"] = m.group(1).strip()
        elif line.startswith("notAfter="):
            raw = line.split("=", 1)[1].strip()
            facts["not_after_raw"] = raw
            try:
                dt = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(
                    tzinfo=timezone.utc)
                facts["not_after"] = dt.date().isoformat()
                facts["days_left"] = (dt - datetime.now(timezone.utc)).days
            except ValueError:
                pass
    return facts


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", nargs="?", default="-", help="файл или - для stdout")
    ap.add_argument("--panel", required=True, help="базовый URL панели")
    ap.add_argument("--token-file", default=TOKEN_FILE)
    ap.add_argument("--resolver", default="1.1.1.1")
    args = ap.parse_args()
    panel, resolver = args.panel.rstrip("/"), args.resolver
    token = open(args.token_file).read().strip()
    nodes = api("/api/nodes", token, panel)
    nodes = nodes if isinstance(nodes, list) else nodes.get("nodes", nodes)
    profiles = api("/api/config-profiles", token, panel)["configProfiles"]
    hosts = api("/api/hosts", token, panel)
    hosts = hosts if isinstance(hosts, list) else hosts.get("hosts", hosts)

    # адрес -> имя ноды: этим потом опознаём цель моста
    by_address = {n["address"]: n["name"] for n in nodes}

    # SNI -> имя ноды. Строится до разбора мостов: цель моста называется своим
    # SNI, и это надёжнее адреса.
    by_domain = {}
    for p in profiles:
        for i in p["config"].get("inbounds", []):
            r = (i.get("streamSettings") or {}).get("realitySettings") or {}
            for sn in r.get("serverNames", []):
                by_domain[sn] = p["name"]

    profile_by_name = {p["name"]: p for p in profiles}
    # профиль ноды: сперва по совпадению имени, иначе по инбаунду, чей SNI
    # резолвится в адрес ноды
    reg = {}
    for n in nodes:
        name, address = n["name"], n["address"]
        entry = {
            "address": {"value": address, "source": "панель, node.address"},
            "connected": n.get("isConnected"),
            "disabled": n.get("isDisabled"),
            "domains": [],
            "apex": None,
            "inbound_tags": [],
            "role": None,
            "pair": None,
            "certificate": {},
            "dns": {},
        }
        prof = profile_by_name.get(name)
        if prof is None:
            for p in profiles:
                for i in p["config"].get("inbounds", []):
                    r = (i.get("streamSettings") or {}).get("realitySettings") or {}
                    for sn in r.get("serverNames", []):
                        if address in dig(sn, "A", resolver):
                            prof = p
                            break
        if prof is not None:
            entry["profile"] = {"value": prof["name"], "source": "панель, config profile"}
            doms = []
            for i in prof["config"].get("inbounds", []):
                if i.get("protocol") != "vless":
                    continue
                entry["inbound_tags"].append(i.get("tag"))
                r = (i.get("streamSettings") or {}).get("realitySettings") or {}
                doms.extend(r.get("serverNames", []))
            entry["domains"] = sorted(set(doms))
            # роль и пара: исходящий-мост называет цель своим SNI, а тот
            # резолвится ровно в адрес ноды-выхода
            for o in prof["config"].get("outbounds", []):
                tag = o.get("tag", "")
                if "BRIDGE" not in tag and "HOP" not in tag:
                    continue
                r = ((o.get("streamSettings") or {}).get("realitySettings") or {})
                target_sni = r.get("serverName")
                target = None
                if target_sni:
                    # Сперва прямое совпадение SNI с доменом ноды. Резолв в
                    # адрес - только запасной путь: у RU-07 в панели записан
                    # адрес релея на контроллере, а не собственный, поэтому
                    # сопоставление по IP на нём и не срабатывает.
                    target = by_domain.get(target_sni)
                    if target is None:
                        for ip in dig(target_sni, "A", resolver):
                            if ip in by_address:
                                target = by_address[ip]
                                break
                entry["role"] = "вход"
                entry["pair"] = {
                    "target": target,
                    "via_sni": target_sni,
                    "outbound_tag": tag,
                    "tunnel_address": (o.get("settings") or {}).get("address"),
                    "source": "панель, исходящий-мост; цель опознана DNS",
                }
        if entry["domains"]:
            primary = entry["domains"][0]
            entry["apex"] = ".".join(primary.split(".")[-2:])
            entry["dns"] = {
                "a": dig(primary, "A", resolver),
                "ns": dig(entry["apex"], "NS", resolver),
                "source": "DNS @" + resolver,
            }
            entry["certificate"] = cert_facts(address, primary)
            probed = address
            if entry["certificate"].get("error") and entry["dns"].get("a"):
                # Адрес из панели не всегда тот, на который указывает домен:
                # у RU-07 там релей. Пробуем адрес из A-записи.
                alt = entry["dns"]["a"][0]
                if alt != address:
                    alt_facts = cert_facts(alt, primary)
                    if not alt_facts.get("error"):
                        entry["certificate"] = alt_facts
                        probed = alt
            entry["certificate"]["source"] = "TLS-хендшейк на " + probed + ":443"
            if entry["certificate"].get("error"):
                entry["certificate"]["note"] = (
                    "не измерено с этой точки: " + entry["certificate"]["error"]
                    + ". 443 отвечает не всем источникам - мерить с другой ноды.")
        if entry.get("dns", {}).get("a") and address not in entry["dns"]["a"]:
            entry["address_mismatch"] = {
                "panel": address,
                "dns": entry["dns"]["a"],
                "note": ("Адрес в панели не совпадает с A-записью домена. "
                         "Штатный случай для ноды, которой панель управляет "
                         "через релей."),
            }
        entry["published_hosts"] = [
            {"remark": h.get("remark"), "sni": h.get("sni"),
             "port": h.get("port"), "disabled": h.get("isDisabled")}
            for h in hosts if h.get("address") == address
        ]
        reg[name] = entry

    # цели мостов - это выходы или транзиты
    targets = {e["pair"]["target"] for e in reg.values()
               if e.get("pair") and e["pair"].get("target")}
    for name, e in reg.items():
        if e["role"] == "вход" and name in targets:
            e["role"] = "транзит"
        elif e["role"] is None:
            e["role"] = "выход"

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "панель " + panel + ", DNS @" + resolver + ", TLS на 443",
        "note": ("Только выводимые поля. Провайдер, тариф и учётная запись "
                 "регистратора машинно не выводятся и живут в ручном слое."),
        "nodes": reg,
    }
    dest = args.out
    text = json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True)
    if dest == "-":
        print(text)
    else:
        open(dest, "w", encoding="utf-8").write(text + "\n")
        print("записано:", dest, len(reg), "нод")


if __name__ == "__main__":
    main()
