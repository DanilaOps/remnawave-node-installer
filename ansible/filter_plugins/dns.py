"""Helpers for provider-agnostic DNS reconciliation.

Only the shape of a provider's answer is provider-specific; the decision
(create, retarget, leave alone) is not.  These filters normalise a provider
answer into one list of records so the decision logic lives in one place and is
unit-testable without touching a registrar.
"""

from __future__ import annotations

from typing import Any

from ansible.errors import AnsibleFilterError


TYPE_KEYS = ("rectype", "record_type", "type")
NAME_KEYS = ("subname", "subdomain", "name")
CONTENT_KEYS = ("content", "ipaddr", "value", "data")


def _first(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def remnawave_dns_subdomain(fqdn: Any, zone: Any) -> str:
    """Return the record name of fqdn inside zone ('ee01' in 'ee01.example.com').

    The apex record is returned as '@', which is what registrars expect.
    """

    if not isinstance(fqdn, str) or not isinstance(zone, str) or not fqdn or not zone:
        raise AnsibleFilterError("both fqdn and zone must be non-empty strings")
    name = fqdn.rstrip(".").lower()
    suffix = zone.rstrip(".").lower()
    if name == suffix:
        return "@"
    if not name.endswith("." + suffix):
        raise AnsibleFilterError(f"{fqdn} does not belong to zone {zone}")
    return name[: -(len(suffix) + 1)]


def remnawave_dns_records(answer: Any, zone: Any) -> list[dict[str, str]]:
    """Normalise a REG.RU style answer into [{type, name, content}].

    Registrar answers vary in field spelling between endpoints and versions, so
    every known spelling is accepted and anything unrecognisable raises instead
    of being silently treated as 'no records' - which would make the role create
    a duplicate.
    """

    if not isinstance(answer, dict):
        raise AnsibleFilterError("DNS answer must be a dictionary")
    if not isinstance(zone, str) or not zone:
        raise AnsibleFilterError("zone must be a non-empty string")

    payload = answer.get("answer", answer)
    domains = payload.get("domains") if isinstance(payload, dict) else None
    if domains is None:
        raise AnsibleFilterError(f"no 'domains' collection in DNS answer: {sorted(payload)}")

    wanted = zone.rstrip(".").lower()
    records: list[dict[str, str]] = []
    seen_zone = False
    for domain in domains:
        if not isinstance(domain, dict):
            continue
        if str(domain.get("dname", "")).rstrip(".").lower() != wanted:
            continue
        seen_zone = True
        rrs = domain.get("rrs")
        if rrs in (None, ""):
            # An empty zone is legitimate; an unparsable one is not.
            continue
        if not isinstance(rrs, list):
            raise AnsibleFilterError("'rrs' must be a list of records")
        for record in rrs:
            if not isinstance(record, dict):
                continue
            records.append(
                {
                    "type": _first(record, TYPE_KEYS).upper(),
                    "name": _first(record, NAME_KEYS) or "@",
                    "content": _first(record, CONTENT_KEYS),
                }
            )
    if not seen_zone:
        raise AnsibleFilterError(f"zone {zone} is not present in the provider answer")
    return records


def remnawave_dns_matches(records: Any, name: Any, record_type: Any) -> list[dict[str, str]]:
    """Return the records that own exactly this name and type."""

    if not isinstance(records, list):
        raise AnsibleFilterError("records must be a list")
    wanted_name = str(name).lower()
    wanted_type = str(record_type).upper()
    return [
        record
        for record in records
        if str(record.get("name", "")).lower() == wanted_name
        and str(record.get("type", "")).upper() == wanted_type
    ]


class FilterModule:
    """Ansible filter registration."""

    def filters(self) -> dict[str, Any]:
        return {
            "remnawave_dns_subdomain": remnawave_dns_subdomain,
            "remnawave_dns_records": remnawave_dns_records,
            "remnawave_dns_matches": remnawave_dns_matches,
        }
