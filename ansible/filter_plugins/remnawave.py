"""Small, deterministic filters used by the Remnawave roles.

The panel adds runtime fields to resources.  These helpers compare the desired
declarative subset with the API response without deleting fields managed by
other operators.
"""

from __future__ import annotations

import base64
import re
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


def remnawave_reality_public_key(private_key: str) -> str:
    """Derive the Reality X25519 public key from the server's private key.

    The end-to-end probe needs the public half to connect, and only the private
    half is stored. Xray prints both when it generates a key, but by the time a
    node is verified the keypair may have come from the panel instead, and the
    controller deliberately has no Docker to run `xray x25519` in. cryptography
    is already a dependency of community.crypto, so the derivation is local,
    offline and deterministic.
    """

    if not isinstance(private_key, str) or not private_key.strip():
        raise AnsibleFilterError("Reality private key must be a non-empty string")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    except ImportError as error:  # pragma: no cover - dependency of community.crypto
        raise AnsibleFilterError(
            f"cryptography is required to derive the Reality public key: {error}"
        ) from error

    text = private_key.strip()
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except Exception as error:
        raise AnsibleFilterError(f"Reality private key is not base64url: {error}") from error
    if len(raw) != 32:
        raise AnsibleFilterError(
            f"Reality private key must decode to 32 bytes, got {len(raw)}"
        )
    public = (
        X25519PrivateKey.from_private_bytes(raw)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return base64.urlsafe_b64encode(public).decode().rstrip("=")


def remnawave_ip_in_cidrs(address: str, cidrs: Iterable[str]) -> bool:
    """Return whether address belongs to at least one declared network."""

    try:
        parsed_address = ip_address(address)
        return any(parsed_address in ip_network(cidr, strict=False) for cidr in cidrs)
    except ValueError as error:
        raise AnsibleFilterError(f"Invalid address or CIDR: {error}") from error


NODE_NAME_PATTERN = re.compile(r"^([A-Z]{2})-([0-9]{2,})$")


def remnawave_country_ordinals(names: Any, country_code: str) -> list[int]:
    """Ordinals of every managed name of one country, from any list of names.

    Only the exact ``CC-NN`` form counts. A name that merely starts with the
    country code - ``TREX``, ``TR-EDGE``, ``tr-01`` - is not this fleet's naming
    and must not influence the next number.
    """

    code = str(country_code).strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        raise AnsibleFilterError(
            f"Country code must be two letters, got {country_code!r}"
        )
    ordinals = []
    for name in names or []:
        if isinstance(name, dict):
            name = name.get("name", "")
        match = NODE_NAME_PATTERN.match(str(name).strip())
        if match and match.group(1) == code:
            ordinals.append(int(match.group(2)))
    return sorted(set(ordinals))


def remnawave_next_node_name(names: Any, country_code: str, width: int = 2) -> str:
    """The next node name for a country: one above the highest one in use.

    Deliberately not the first gap. A number that was used once keeps living in
    DNS records, certificates, logs and other people's notes long after the node
    is gone, so handing it to a different machine is how two unrelated things end
    up with one identity.
    """

    ordinals = remnawave_country_ordinals(names, country_code)
    highest = ordinals[-1] if ordinals else 0
    return f"{str(country_code).strip().upper()}-{highest + 1:0{int(width)}d}"


def node_verify_host_matches(hosts: Any, spec: Any, inbound_uuid_map: Any) -> list[Any]:
    """The Host identity chain, exactly as the reconciler matches it.

    A Host is found by its explicit uuid when the declaration pins one, else by
    remark+address, else - renamed - by inbound+address, else - re-addressed -
    by inbound+remark.  Acceptance resolves the same Host the reconciler would
    have updated, without ever creating one.
    """
    if not isinstance(hosts, list) or not isinstance(spec, dict):
        raise AnsibleFilterError("node_verify_host_matches needs the Host list and one host_spec")
    inbound_uuid = (inbound_uuid_map or {}).get(spec.get("inbound_tag"))

    def by(predicate: Any) -> list[Any]:
        return [host for host in hosts if isinstance(host, dict) and predicate(host)]

    if spec.get("uuid"):
        return by(lambda host: host.get("uuid") == spec["uuid"])
    matches = by(
        lambda host: host.get("remark") == spec.get("remark")
        and host.get("address") == spec.get("address")
    )
    if not matches and inbound_uuid:
        matches = by(
            lambda host: (host.get("inbound") or {}).get("configProfileInboundUuid") == inbound_uuid
            and host.get("address") == spec.get("address")
        )
    if not matches and inbound_uuid:
        matches = by(
            lambda host: (host.get("inbound") or {}).get("configProfileInboundUuid") == inbound_uuid
            and host.get("remark") == spec.get("remark")
        )
    return matches


def remnawave_firewall_covers(ruleset: Any, cidr: Any) -> bool:
    """True when an nftables ruleset carries the given source restriction.

    nft prints a host route without its prefix: the template writes
    94.141.123.63/32 and "nft list table" answers 94.141.123.63, so a raw
    substring comparison calls a correct firewall wrong.  Both spellings of a
    host route (and only of a host route - a real subnet keeps its prefix) are
    accepted, matched on token boundaries so 10.0.0.1 does not pass because
    10.0.0.10 is present.
    """
    if not isinstance(ruleset, str) or not str(cidr).strip():
        raise AnsibleFilterError("remnawave_firewall_covers needs a ruleset string and a CIDR")
    network = ip_network(str(cidr).strip(), strict=False)
    spellings = {str(network), str(network.network_address) + "/" + str(network.prefixlen)}
    if network.prefixlen == network.max_prefixlen:
        spellings.add(str(network.network_address))
    pattern = "(^|[\s,{])(" + "|".join(re.escape(s) for s in sorted(spellings)) + ")($|[\s,}])"
    return re.search(pattern, ruleset) is not None


class FilterModule:
    """Ansible filter registration."""

    def filters(self) -> dict[str, Any]:
        return {
            "node_verify_host_matches": node_verify_host_matches,
            "remnawave_firewall_covers": remnawave_firewall_covers,
            "remnawave_is_subset": remnawave_is_subset,
            "remnawave_response_items": remnawave_response_items,
            "remnawave_inbound_owners": remnawave_inbound_owners,
            "remnawave_uuid_list": remnawave_uuid_list,
            "remnawave_reality_settings": remnawave_reality_settings,
            "remnawave_upsert_inbounds": remnawave_upsert_inbounds,
            "remnawave_strip_inbound_uuids": remnawave_strip_inbound_uuids,
            "remnawave_normalize_node_links": remnawave_normalize_node_links,
            "remnawave_normalize_host_links": remnawave_normalize_host_links,
            "remnawave_reality_public_key": remnawave_reality_public_key,
            "remnawave_ip_in_cidrs": remnawave_ip_in_cidrs,
            "remnawave_country_ordinals": remnawave_country_ordinals,
            "remnawave_next_node_name": remnawave_next_node_name,
        }
