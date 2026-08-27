"""Small, deterministic filters used by the Remnawave roles.

The panel adds runtime fields to resources.  These helpers compare the desired
declarative subset with the API response without deleting fields managed by
other operators.
"""

from __future__ import annotations

from copy import deepcopy
from ipaddress import ip_address, ip_network
from typing import Any, Iterable

from ansible.errors import AnsibleFilterError


DEFAULT_IGNORED_KEYS = {
    "createdAt",
    "updatedAt",
    "viewPosition",
    "lastStatusChange",
    "lastStatusMessage",
}


def _clean(value: Any, ignored: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _clean(item, ignored)
            for key, item in value.items()
            if key not in ignored
        }
    if isinstance(value, list):
        return [_clean(item, ignored) for item in value]
    return deepcopy(value)


def remnawave_is_subset(
    desired: Any,
    actual: Any,
    ignored_keys: Iterable[str] | None = None,
) -> bool:
    """Return True when every desired value exists in the actual value.

    Dictionaries are compared recursively. Lists are ordered because inbound
    and node ordering is significant in the panel. Runtime UUIDs in list items
    do not affect the comparison when they are absent from desired.
    """

    ignored = DEFAULT_IGNORED_KEYS | set(ignored_keys or [])
    desired = _clean(desired, ignored)
    actual = _clean(actual, ignored)

    if isinstance(desired, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            key in actual and remnawave_is_subset(value, actual[key], ignored)
            for key, value in desired.items()
        )
    if isinstance(desired, list):
        if not isinstance(actual, list) or len(desired) != len(actual):
            return False
        return all(
            remnawave_is_subset(expected, received, ignored)
            for expected, received in zip(desired, actual)
        )
    return desired == actual


def remnawave_response_items(payload: Any, collection_key: str = "") -> list[Any]:
    """Extract a collection from Remnawave's two response envelope shapes."""

    if not isinstance(payload, dict):
        raise AnsibleFilterError("Remnawave response must be a dictionary")
    response = payload.get("response", [])
    if collection_key and isinstance(response, dict):
        response = response.get(collection_key, [])
    if response is None:
        return []
    if not isinstance(response, list):
        raise AnsibleFilterError(
            f"Remnawave response collection {collection_key!r} is not a list"
        )
    return response


def remnawave_inbound_owners(
    profiles: Any,
    managed_tags: Iterable[str],
    allowed_profile_uuid: str = "",
) -> list[dict[str, str]]:
    """Find profiles other than allowed_profile_uuid that own managed tags."""

    tags = set(managed_tags)
    owners: list[dict[str, str]] = []
    for profile in profiles or []:
        if not isinstance(profile, dict):
            continue
        if allowed_profile_uuid and profile.get("uuid") == allowed_profile_uuid:
            continue
        inbounds = profile.get("inbounds")
        if inbounds is None:
            inbounds = (profile.get("config") or {}).get("inbounds", [])
        for inbound in inbounds or []:
            tag = inbound.get("tag") if isinstance(inbound, dict) else None
            if tag in tags:
                owners.append(
                    {
                        "uuid": str(profile.get("uuid", "")),
                        "name": str(profile.get("name", "")),
                        "tag": str(tag),
                    }
                )
    return owners


def remnawave_uuid_list(values: Any) -> list[str]:
    """Normalize API lists containing UUID strings or objects with uuid."""

    result: list[str] = []
    for value in values or []:
        uuid = value.get("uuid") if isinstance(value, dict) else value
        if uuid and str(uuid) not in result:
            result.append(str(uuid))
    return result


def remnawave_reality_settings(config: Any) -> dict[str, Any]:
    """Return the first server-side Reality settings object in a profile."""

    if not isinstance(config, dict):
        return {}
    for inbound in config.get("inbounds", []) or []:
        if not isinstance(inbound, dict):
            continue
        stream = inbound.get("streamSettings") or {}
        reality = stream.get("realitySettings")
        if isinstance(reality, dict) and reality.get("privateKey"):
            return reality
    return {}


def remnawave_ip_in_cidrs(address: str, cidrs: Iterable[str]) -> bool:
    """Return whether address belongs to at least one declared network."""

    try:
        parsed_address = ip_address(address)
        return any(parsed_address in ip_network(cidr, strict=False) for cidr in cidrs)
    except ValueError as error:
        raise AnsibleFilterError(f"Invalid address or CIDR: {error}") from error


class FilterModule:
    """Ansible filter registration."""

    def filters(self) -> dict[str, Any]:
        return {
            "remnawave_is_subset": remnawave_is_subset,
            "remnawave_response_items": remnawave_response_items,
            "remnawave_inbound_owners": remnawave_inbound_owners,
            "remnawave_uuid_list": remnawave_uuid_list,
            "remnawave_reality_settings": remnawave_reality_settings,
            "remnawave_ip_in_cidrs": remnawave_ip_in_cidrs,
        }
