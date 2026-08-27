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


def remnawave_reality_settings(
    config: Any,
    tags: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return server-side Reality settings from a profile config.

    When tags is given, only inbounds carrying one of those tags are
    considered.  A shared Config Profile holds one inbound per node, so a
    node must reuse its own Reality keypair and must never adopt another
    node's key just because that inbound happens to come first.
    """

    if not isinstance(config, dict):
        return {}
    wanted = set(tags) if tags else None
    for inbound in config.get("inbounds", []) or []:
        if not isinstance(inbound, dict):
            continue
        if wanted is not None and inbound.get("tag") not in wanted:
            continue
        stream = inbound.get("streamSettings") or {}
        reality = stream.get("realitySettings")
        if isinstance(reality, dict) and reality.get("privateKey"):
            return reality
    return {}


def remnawave_upsert_inbounds(
    config: Any,
    desired_inbounds: Any,
    prune_tags: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Merge desired inbounds into an existing profile config by tag.

    Everything the shared profile owns outside the managed inbounds --
    routing, dns, outbounds, policy and other nodes' inbounds -- is copied
    through untouched.  An existing inbound with the same tag is replaced in
    place, keeping the panel-assigned uuid so Hosts and Nodes keep pointing
    at the same inbound identity.  prune_tags removes inbounds this node
    used to own and no longer declares.
    """

    if not isinstance(config, dict):
        raise AnsibleFilterError("Config Profile config must be a dictionary")
    if not isinstance(desired_inbounds, list):
        raise AnsibleFilterError("Desired inbounds must be a list")

    merged = deepcopy(config)
    inbounds = list(merged.get("inbounds") or [])
    index_by_tag: dict[str, int] = {}
    for position, inbound in enumerate(inbounds):
        if isinstance(inbound, dict) and inbound.get("tag"):
            index_by_tag.setdefault(str(inbound["tag"]), position)

    declared: list[str] = []
    for desired in desired_inbounds:
        if not isinstance(desired, dict) or not desired.get("tag"):
            raise AnsibleFilterError("Every desired inbound must carry a tag")
        tag = str(desired["tag"])
        declared.append(tag)
        replacement = deepcopy(desired)
        position = index_by_tag.get(tag)
        if position is None:
            inbounds.append(replacement)
            index_by_tag[tag] = len(inbounds) - 1
            continue
        existing_uuid = inbounds[position].get("uuid")
        if existing_uuid is not None and "uuid" not in replacement:
            replacement["uuid"] = existing_uuid
        inbounds[position] = replacement

    stale = {str(tag) for tag in prune_tags or []} - set(declared)
    if stale:
        inbounds = [
            inbound
            for inbound in inbounds
            if not (isinstance(inbound, dict) and str(inbound.get("tag")) in stale)
        ]

    merged["inbounds"] = inbounds
    return merged


def remnawave_strip_inbound_uuids(config: Any) -> dict[str, Any]:
    """Return the config with panel-assigned inbound uuids removed.

    The uuid is Remnawave bookkeeping, not Xray configuration; it is stripped
    before the config is handed to `xray run -test`.
    """

    if not isinstance(config, dict):
        raise AnsibleFilterError("Config Profile config must be a dictionary")
    stripped = deepcopy(config)
    inbounds = []
    for inbound in stripped.get("inbounds") or []:
        if isinstance(inbound, dict):
            inbound = {key: value for key, value in inbound.items() if key != "uuid"}
        inbounds.append(inbound)
    if "inbounds" in stripped:
        stripped["inbounds"] = inbounds
    return stripped


def remnawave_normalize_node_links(node: Any) -> dict[str, Any]:
    """Normalize a Node response so it can be compared with a desired payload.

    The panel returns activeInbounds as objects while the managed payload
    sends uuid strings.  Without this the Node PATCH would fire on every
    single run.
    """

    if not isinstance(node, dict):
        return {}
    normalized = deepcopy(node)
    profile = normalized.get("configProfile")
    if isinstance(profile, dict) and "activeInbounds" in profile:
        profile["activeInbounds"] = remnawave_uuid_list(profile.get("activeInbounds"))
    return normalized


def remnawave_normalize_host_links(host: Any) -> dict[str, Any]:
    """Normalize a Host response so it can be compared with a desired payload."""

    if not isinstance(host, dict):
        return {}
    normalized = deepcopy(host)
    if "nodes" in normalized:
        normalized["nodes"] = remnawave_uuid_list(normalized.get("nodes"))
    return normalized


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
            "remnawave_upsert_inbounds": remnawave_upsert_inbounds,
            "remnawave_strip_inbound_uuids": remnawave_strip_inbound_uuids,
            "remnawave_normalize_node_links": remnawave_normalize_node_links,
            "remnawave_normalize_host_links": remnawave_normalize_host_links,
            "remnawave_ip_in_cidrs": remnawave_ip_in_cidrs,
        }
