#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собрать реестр нод в три вида из одного источника.

Вход: registry-derived.json (выводимая часть, собирается collect_registry.py),
registry-manual.yml (ручной слой: провайдер, тариф, регистратор) и карта
доступов с рабочей машины.

Выход:
  --public <файл>   срез без адресов и доменов, для публичного репозитория
  --private <файл>  всё выводимое плюс ручной слой, для /etc/remnawave
  --doc <файл>      документ для ноутбука: полная картина плюс доступы
"""
import argparse
import json
import os
import re

# Карта доступов - какая нода каким файлом пароля открывается, какие хосты
# управляющие - в этом файле НЕ живёт: репозиторий публичный. Она приходит
# отдельным JSON через --access-map и лежит рядом с самими паролями на рабочей
# машине. Без неё раздел «Доступы» просто не рендерится.
DEFAULT_ACCESS = {"nodes": {}, "control_hosts": [], "via_controller": [],
                  "notes": {}}


def load_manual(path):
    """Крошечный разбор ручного слоя: 'НОДА:' и под ним 'ключ: значение'."""
    manual = {}
    if not path or not os.path.exists(path):
        return manual
    node = None
    for line in open(path, encoding="utf-8"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Z]{2}-\d{2}):\s*$", line.strip())
        if m:
            node = m.group(1)
            manual[node] = {}
            continue
        m = re.match(r"^\s+([a-z_]+):\s*(.*)$", line.rstrip())
        if m and node:
            manual[node][m.group(1)] = m.group(2).strip().strip('"')
    return manual


def yaml_scalar(v):
    if v is None or v == "":
        return '""'
    s = str(v)
    return s if re.fullmatch(r"[A-Za-z0-9._/@:-]+", s) else '"%s"' % s.replace('"', "'")


def write_public(reg, dest):
    # Само имя apex наружу не идёт: перечислить домены парка в публичном
    # репозитории - это ровно та единая подпись, которую пункт 1 ТЗ и
    # предлагает разводить. Наружу идёт непрозрачная метка семейства, по
    # которой видно, сколько их и как ноды по ним распределены.
    families = {}
    for name in sorted(reg["nodes"]):
        apex = reg["nodes"][name].get("apex")
        if apex and apex not in families:
            families[apex] = "family-%d" % (len(families) + 1)
    lines = [
        "---",
        "# Реестр нод: публичный срез.",
        "#",
        "# ЗДЕСЬ НЕТ СЕКРЕТОВ. Ни адресов, ни доменов, ни провайдеров, ни тарифов -",
        "# всё, что сопоставляет имя машине или называет её домен, живёт в",
        "# /etc/remnawave/registry.yml на контроллере. Правило то же, что у",
        "# monitoring/capacity/capacity.yml, и по той же причине: репозиторий публичный.",
        "#",
        "# Файл собирается из панели: роль и пара выводятся из исходящих-мостов,",
        "# срок сертификата - из TLS-хендшейка. Руками не правится.",
        "#",
        "# family - непрозрачная метка доменного семейства. Сколько их и как по ним",
        "# разложены ноды - видно; какие это домены - нет. Сейчас семейство одно на",
        "# весь парк, и это исходная точка для пункта 1 ТЗ.",
        "version: 1",
        "generated_at: %s" % reg["generated_at"],
        "",
        "nodes:",
    ]
    for name in sorted(reg["nodes"]):
        e = reg["nodes"][name]
        c = e.get("certificate") or {}
        p = e.get("pair") or {}
        lines.append("  %s:" % name)
        lines.append("    role: %s" % yaml_scalar(e.get("role")))
        if p.get("target"):
            lines.append("    pair_exit: %s" % yaml_scalar(p["target"]))
        lines.append("    family: %s" % yaml_scalar(families.get(e.get("apex"), "")))
        if c.get("ca"):
            lines.append("    certificate_authority: %s" % yaml_scalar(c["ca"]))
        if c.get("not_after"):
            lines.append("    certificate_expires: %s" % c["not_after"])
        lines.append("    enabled: %s" % ("false" if e.get("disabled") else "true"))
    open(dest, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return len(reg["nodes"])


def write_private(reg, manual, dest):
    lines = [
        "---",
        "# Реестр нод: полная запись. Живёт рядом с fleet.yml и secrets.yml и",
        "# читается плейбуками как файл extra-vars.",
        "#",
        "# Выводимые поля собраны из панели, DNS и TLS - их правка здесь будет",
        "# затёрта следующей сборкой. Правится только ручной слой:",
        "# provider, tariff, registrar_account.",
        "version: 1",
        "generated_at: %s" % reg["generated_at"],
        "source: %s" % yaml_scalar(reg["source"]),
        "",
        "node_registry:",
    ]
    for name in sorted(reg["nodes"]):
        e = reg["nodes"][name]
        c = e.get("certificate") or {}
        p = e.get("pair") or {}
        m = manual.get(name, {})
        lines.append("  %s:" % name)
        lines.append("    address: %s" % yaml_scalar(e["address"]["value"]))
        if e.get("address_mismatch"):
            lines.append("    address_dns: %s" %
                         yaml_scalar(", ".join(e["address_mismatch"]["dns"])))
            lines.append("    # панель управляет этой нодой через релей")
        lines.append("    domains: [%s]" % ", ".join(yaml_scalar(d) for d in e["domains"]))
        lines.append("    apex: %s" % yaml_scalar(e.get("apex")))
        ns = (e.get("dns") or {}).get("ns") or []
        lines.append("    nameservers: [%s]" % ", ".join(yaml_scalar(x) for x in ns))
        lines.append("    registrar: %s" % yaml_scalar(
            "reg.ru" if any("reg.ru" in x for x in ns) else ""))
        lines.append("    role: %s" % yaml_scalar(e.get("role")))
        if p.get("target"):
            lines.append("    pair_exit: %s" % yaml_scalar(p["target"]))
            lines.append("    pair_via_sni: %s" % yaml_scalar(p.get("via_sni")))
            lines.append("    pair_tunnel_address: %s" % yaml_scalar(p.get("tunnel_address")))
        lines.append("    certificate_authority: %s" % yaml_scalar(c.get("ca")))
        lines.append("    certificate_expires: %s" % yaml_scalar(c.get("not_after")))
        if c.get("error"):
            lines.append("    certificate_note: %s" % yaml_scalar(c.get("note", c["error"])))
        lines.append("    inbound_tags: [%s]" %
                     ", ".join(yaml_scalar(t) for t in e.get("inbound_tags", [])))
        lines.append("    # --- ручной слой ---")
        lines.append("    provider: %s" % yaml_scalar(m.get("provider")))
        lines.append("    tariff: %s" % yaml_scalar(m.get("tariff")))
        lines.append("    registrar_account: %s" % yaml_scalar(m.get("registrar_account")))
    open(dest, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return len(reg["nodes"])


def write_doc(reg, manual, dest, access):
    n = reg["nodes"]
    amap = access.get("nodes", {})
    via = set(access.get("via_controller", []))
    notes = access.get("notes", {})
    filled = sum(1 for k in n if manual.get(k, {}).get("provider"))
    out = []
    a = out.append
    a("# Реестр нод August VPN")
    a("")
    a("@ Собрано | %s" % reg["generated_at"].replace("T", " ").replace("+00:00", " UTC"))
    a("@ Источники | панель, DNS, TLS-хендшейк на 443")
    a("@ Ручной слой | заполнено %d из %d нод" % (filled, len(n)))
    a("@ Обновление | collect_registry.py на хосте панели, затем render_registry.py")
    a("")
    a("Этот файл собирается автоматически. Руками правится только ручной слой на "
      "контроллере — `/etc/remnawave/registry-manual.yml`; всё остальное будет "
      "затёрто следующей сборкой.")
    a("")
    a("## Ноды")
    a("")
    a("| Нода | Роль | Пара | Домен | Адрес | Провайдер | Тариф |")
    a("| --- | --- | --- | --- | --- | --- | --- |")
    for k in sorted(n):
        e = n[k]
        p = e.get("pair") or {}
        m = manual.get(k, {})
        a("| %s | %s | %s | %s | %s | %s | %s |" % (
            k, e.get("role") or "-", p.get("target") or "—",
            (e["domains"] or ["—"])[0], e["address"]["value"],
            m.get("provider") or "—", m.get("tariff") or "—"))
    a("")
    a("## Домены и сертификаты")
    a("")
    a("Все ноды на одном apex и одном УЦ. Это и есть единая подпись парка, "
      "которую предлагает разводить пункт 1 ТЗ.")
    a("")
    a("| Нода | Домен | apex | NS | УЦ | Действует до | Дней |")
    a("| --- | --- | --- | --- | --- | --- | --- |")
    for k in sorted(n):
        e = n[k]
        c = e.get("certificate") or {}
        ns = ", ".join((e.get("dns") or {}).get("ns") or []) or "—"
        a("| %s | %s | %s | %s | %s | %s | %s |" % (
            k, (e["domains"] or ["—"])[0], e.get("apex") or "—", ns,
            c.get("ca") or "—", c.get("not_after") or "не измерен",
            c.get("days_left", "—")))
    a("")
    a("## Пары «вход — выход»")
    a("")
    a("Выведено из исходящих-мостов в панели: мост называет цель своим SNI, "
      "а тот резолвится в адрес ноды-выхода.")
    a("")
    a("| Вход | Выход | SNI моста | Адрес туннеля | Тег исходящего |")
    a("| --- | --- | --- | --- | --- |")
    for k in sorted(n):
        p = n[k].get("pair") or {}
        if not p.get("target"):
            continue
        a("| %s | %s | %s | %s | %s |" % (
            k, p["target"], p.get("via_sni") or "—",
            p.get("tunnel_address") or "—", p.get("outbound_tag") or "—"))
    a("")
    if amap or access.get("control_hosts"):
        a("## Доступы")
        a("")
        a("Пароли лежат в файлах рядом с этой картой и в документ не попадают. "
          "Ключ deployer живёт только на контроллере и на рабочую машину не "
          "копируется.")
        a("")
        a("| Нода | Адрес | Файл пароля | Через контроллер | Замечание |")
        a("| --- | --- | --- | --- | --- |")
        for k in sorted(n):
            e = n[k]
            pw = amap.get(k)
            note = notes.get(k, "")
            if e.get("address_mismatch"):
                note = (note + "; " if note else "") + "панель ходит через релей"
            a("| %s | %s | %s | %s | %s |" % (
                k, e["address"]["value"], ("`%s`" % pw) if pw else "—",
                "да" if k in via else "—", note or "—"))
        a("")
        if access.get("control_hosts"):
            a("## Управляющие хосты")
            a("")
            a("| Роль | Адрес | Файл пароля |")
            a("| --- | --- | --- |")
            for h in access["control_hosts"]:
                a("| %s | %s | %s |" % (h.get("role", "—"), h.get("address", "—"),
                                        h.get("password_file", "—")))
            a("")
    a("## Чего здесь нет")
    a("")
    a("- Провайдер и тариф выводятся только руками; пока ручной слой пуст, "
      "пункт 22 ТЗ — учёт стоимости по маршрутам — считать не из чего.")
    a("- Сертификаты трёх входов (RU-01, RU-03, RU-04) не измеряются с хоста "
      "панели: их 443 отвечает не всякому источнику. Мерить с другой ноды.")
    open(dest, "w", encoding="utf-8").write("\n".join(out) + "\n")
    return len(n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("derived")
    ap.add_argument("--manual", default="")
    ap.add_argument("--public", default="")
    ap.add_argument("--private", default="")
    ap.add_argument("--doc", default="")
    ap.add_argument("--access-map", default="",
                    help="JSON с картой доступов; без него раздел не рендерится")
    args = ap.parse_args()
    reg = json.load(open(args.derived, encoding="utf-8"))
    manual = load_manual(args.manual)
    access = dict(DEFAULT_ACCESS)
    if args.access_map:
        access.update(json.load(open(args.access_map, encoding="utf-8")))
    if args.public:
        print("публичный срез:", args.public, write_public(reg, args.public), "нод")
    if args.private:
        print("полная запись:", args.private, write_private(reg, manual, args.private), "нод")
    if args.doc:
        print("документ:", args.doc, write_doc(reg, manual, args.doc, access), "нод")


if __name__ == "__main__":
    main()
