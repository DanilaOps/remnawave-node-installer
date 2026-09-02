"""Model, validator and metric renderer for the August VPN capacity inventory.

One module, used by three callers, so that the rules a human reads in
monitoring/capacity/README.md, the rules CI enforces and the rules the running
exporter applies cannot drift apart:

  * monitoring/validate_capacity.py  - CI and role preflight
  * monitoring/capacity_exporter.py  - the running exporter
  * monitoring/tests/test_capacity_model.py     - the tests

Nothing here reaches the network and nothing here is Ansible-specific: it takes
a parsed document and answers questions about it.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import math
import re
from typing import Any, Iterable, Iterator, Sequence

SCHEMA_VERSION = 2

DIRECTIONS = ("download", "upload")
# "shared_pool" is a node saying "my capacity is accounted at my pool, which is
# rated as one location". It is not the same as "unmeasured", which says nothing
# is known at all: shared_pool points at a real figure that exists one level up.
CAPACITY_SOURCES = ("measured", "declared", "range", "unmeasured", "shared_pool")
ATTRIBUTIONS = ("per_node", "aggregate")
RANGE_POLICIES = ("conservative", "blocker")
MEASUREMENT_KINDS = ("inbound", "outbound")

NAME_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]*$")
TAG_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]*$")

# A capacity file that carries any of these is a capacity file that has stopped
# being safe to commit. Checked by shape, not by name, so a value that merely
# looks like a credential is caught even under an innocent key.
SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|private[_-]?key|credential|uuid)",
    re.IGNORECASE,
)
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

BLOCKER = "blocker"
WARNING = "warning"

# An evidence run this far above the published rating means one of the two is
# wrong. Reported, never applied.
EVIDENCE_ABOVE_DECLARED_RATIO = 1.2


@dataclasses.dataclass(frozen=True)
class Problem:
    """One validation finding. `severity` decides whether it stops a deploy."""

    severity: str
    code: str
    where: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.severity.upper()} {self.code} [{self.where}] {self.message}"


@dataclasses.dataclass(frozen=True)
class Capacity:
    """One direction of one entry, after the source policy has been applied."""

    direction: str
    source: str
    mbps: float | None
    conservative: bool = False
    bound_min: float | None = None
    bound_max: float | None = None
    # How a figure divides between the machines it covers. "aggregate" means the
    # division is unknown, so losing one member does not subtract a computable
    # amount - the exporter reports that as uncertainty rather than a number.
    attribution: str = "per_node"

    @property
    def published(self) -> bool:
        return self.mbps is not None

    @property
    def aggregate(self) -> bool:
        return self.attribution == "aggregate"


@dataclasses.dataclass(frozen=True)
class Entry:
    """A node, a pool or a bridge - everything capacity is stated about."""

    scope: str
    name: str
    enabled: bool
    capacity: dict[str, Capacity]
    measurement_kind: str | None
    measurement_node: str | None
    measurement_tag: str | None
    pool: str | None = None
    members: tuple[str, ...] = ()
    source_node: str | None = None
    destination_node: str | None = None
    # Whether users connect here. A node reachable only as the far end of a
    # bridge carries bridged traffic, which is already counted where the user
    # entered, so it is not service capacity of its own.
    serves_users: bool = True


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _walk(node: Any, path: str = "") -> Iterator[tuple[str, Any, Any]]:
    """Yield (path, key, value) for every scalar in a nested document."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(value, (dict, list)):
                yield from _walk(value, child)
            else:
                yield child, key, value
    elif isinstance(node, list):
        for index, value in enumerate(node):
            child = f"{path}[{index}]"
            if isinstance(value, (dict, list)):
                yield from _walk(value, child)
            else:
                yield child, None, value


def find_secrets(document: Any) -> list[Problem]:
    """Refuse anything that turns a public capacity file into a credential leak.

    Three shapes are caught: a key whose name says credential, a value that is
    a UUID (Remnawave node and squad identifiers are UUIDs and do not belong in
    a public file), and an inline private key. Public IP addresses are caught
    too: the file names nodes, and where a node lives is not its name.
    """
    problems: list[Problem] = []
    for path, key, value in _walk(document):
        if key is not None and SECRET_KEY_RE.search(str(key)):
            problems.append(
                Problem(BLOCKER, "secret.key_name", path, f"key {key!r} must not appear in a versioned capacity file")
            )
            continue
        if not isinstance(value, str):
            continue
        if UUID_RE.search(value):
            problems.append(Problem(BLOCKER, "secret.uuid", path, "a UUID must not be committed here"))
        if PEM_RE.search(value):
            problems.append(Problem(BLOCKER, "secret.private_key", path, "a private key must not be committed here"))
        for token in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", value):
            try:
                address = ipaddress.ip_address(token)
            except ValueError:
                continue
            if address.is_global:
                problems.append(
                    Problem(BLOCKER, "secret.public_ip", path, f"public address {token} must not be committed here")
                )
    return problems


def _parse_capacity(raw: Any, direction: str, where: str, problems: list[Problem]) -> Capacity:
    if not isinstance(raw, dict):
        problems.append(Problem(BLOCKER, "capacity.shape", where, f"{direction} must be a mapping"))
        return Capacity(direction, "unmeasured", None)

    source = raw.get("source")
    if source not in CAPACITY_SOURCES:
        problems.append(
            Problem(BLOCKER, "capacity.source", where, f"{direction}.source must be one of {list(CAPACITY_SOURCES)}")
        )
        return Capacity(direction, "unmeasured", None)

    attribution = raw.get("attribution", "per_node")
    if attribution not in ATTRIBUTIONS:
        problems.append(
            Problem(BLOCKER, "capacity.attribution", where, f"{direction}.attribution must be one of {list(ATTRIBUTIONS)}")
        )
        attribution = "per_node"

    if source == "shared_pool":
        for forbidden in ("mbps", "mbps_min", "mbps_max"):
            if forbidden in raw:
                problems.append(
                    Problem(
                        BLOCKER,
                        "capacity.shared_pool_number",
                        where,
                        f"{direction} is accounted at its pool and must not carry {forbidden}",
                    )
                )
        return Capacity(direction, source, None, attribution="aggregate")

    if source in ("measured", "declared"):
        mbps = raw.get("mbps")
        if not _is_number(mbps) or mbps <= 0:
            problems.append(
                Problem(BLOCKER, "capacity.mbps", where, f"{direction}.mbps must be a positive number for source {source}")
            )
            return Capacity(direction, source, None)
        if source == "measured" and not raw.get("measured_at"):
            problems.append(
                Problem(BLOCKER, "capacity.measured_at", where, f"{direction} is measured but carries no measured_at")
            )
        for forbidden in ("mbps_min", "mbps_max"):
            if forbidden in raw:
                problems.append(
                    Problem(BLOCKER, "capacity.mixed", where, f"{direction}.{forbidden} is only valid for source range")
                )
        return Capacity(direction, source, float(mbps), attribution=attribution)

    if source == "range":
        low, high = raw.get("mbps_min"), raw.get("mbps_max")
        if not _is_number(low) or not _is_number(high) or low <= 0 or high < low:
            problems.append(
                Problem(BLOCKER, "capacity.range", where, f"{direction} needs 0 < mbps_min <= mbps_max")
            )
            return Capacity(direction, source, None)
        if "mbps" in raw:
            problems.append(
                Problem(
                    BLOCKER,
                    "capacity.invented_point",
                    where,
                    f"{direction} is a range; an exact mbps here would be an invented number",
                )
            )
            return Capacity(direction, source, None, bound_min=float(low), bound_max=float(high))
        policy = raw.get("policy")
        if policy not in RANGE_POLICIES:
            problems.append(
                Problem(
                    BLOCKER,
                    "capacity.range_policy",
                    where,
                    f"{direction} is a range and needs policy: conservative (publish mbps_min) or blocker",
                )
            )
            return Capacity(direction, source, None, bound_min=float(low), bound_max=float(high))
        if policy == "blocker":
            problems.append(
                Problem(
                    BLOCKER,
                    "capacity.range_blocker",
                    where,
                    f"{direction} is a range of {low}-{high} Mbit/s and policy is blocker: "
                    "narrow the range by measuring, or set policy: conservative to publish the lower bound",
                )
            )
            return Capacity(direction, source, None, bound_min=float(low), bound_max=float(high))
        return Capacity(
            direction, source, float(low), conservative=True,
            bound_min=float(low), bound_max=float(high), attribution=attribution,
        )

    # unmeasured
    if "mbps" in raw or "mbps_min" in raw:
        problems.append(
            Problem(BLOCKER, "capacity.mixed", where, f"{direction} is unmeasured but carries a number")
        )
    problems.append(
        Problem(WARNING, "capacity.unmeasured", where, f"{direction} has no rating; it is excluded from every total")
    )
    return Capacity(direction, "unmeasured", None)


def _parse_measurement(raw: Any, scope: str, where: str, problems: list[Problem]) -> tuple[str | None, str | None, str | None]:
    if not isinstance(raw, dict):
        problems.append(Problem(BLOCKER, "measurement.shape", where, "measurement must be a mapping"))
        return None, None, None
    kind = raw.get("kind")
    if kind not in MEASUREMENT_KINDS:
        problems.append(
            Problem(BLOCKER, "measurement.kind", where, f"measurement.kind must be one of {list(MEASUREMENT_KINDS)}")
        )
        return None, None, None
    tag = raw.get("tag")
    node = raw.get("node")
    if kind == "outbound":
        if not tag:
            problems.append(
                Problem(
                    BLOCKER,
                    "measurement.tag_required",
                    where,
                    "an outbound measurement needs its own outbound tag; a shared DIRECT tag mixes "
                    "user traffic with bridge traffic and cannot be used",
                )
            )
        elif not TAG_RE.match(str(tag)):
            problems.append(Problem(BLOCKER, "measurement.tag_format", where, f"tag {tag!r} is not a valid outbound tag"))
        if not node:
            problems.append(Problem(BLOCKER, "measurement.node_required", where, "an outbound measurement needs its source node"))
    else:
        if tag:
            problems.append(
                Problem(
                    BLOCKER,
                    "measurement.tag_forbidden",
                    where,
                    "an inbound measurement is the node's own counter and must not carry a tag",
                )
            )
        if scope != "pool" and not node:
            problems.append(Problem(BLOCKER, "measurement.node_required", where, "an inbound measurement needs its node"))
    return kind, (str(node) if node else None), (str(tag) if tag else None)


def _capacity_block(raw: Any, where: str, problems: list[Problem]) -> dict[str, Capacity]:
    if not isinstance(raw, dict):
        problems.append(Problem(BLOCKER, "capacity.shape", where, "capacity must be a mapping with download and upload"))
        return {d: Capacity(d, "unmeasured", None) for d in DIRECTIONS}
    unknown = set(raw) - set(DIRECTIONS)
    if unknown:
        problems.append(
            Problem(BLOCKER, "capacity.unknown_direction", where, f"unknown capacity direction(s): {sorted(unknown)}")
        )
    result: dict[str, Capacity] = {}
    for direction in DIRECTIONS:
        if direction not in raw:
            problems.append(Problem(BLOCKER, "capacity.missing", where, f"{direction} capacity is not stated"))
            result[direction] = Capacity(direction, "unmeasured", None)
        else:
            result[direction] = _parse_capacity(raw[direction], direction, where, problems)
    return result


def _check_evidence(raw: Any, capacity: dict[str, Capacity], where: str, problems: list[Problem]) -> None:
    if raw is None:
        return
    if not isinstance(raw, list):
        problems.append(Problem(BLOCKER, "evidence.shape", where, "evidence must be a list"))
        return
    for index, item in enumerate(raw):
        spot = f"{where}.evidence[{index}]"
        if not isinstance(item, dict):
            problems.append(Problem(BLOCKER, "evidence.shape", spot, "each evidence entry must be a mapping"))
            continue
        direction = item.get("direction")
        if direction not in DIRECTIONS:
            problems.append(Problem(BLOCKER, "evidence.direction", spot, f"direction must be one of {list(DIRECTIONS)}"))
            continue
        if not item.get("at"):
            problems.append(Problem(BLOCKER, "evidence.at", spot, "evidence without a date is not evidence"))
        value = item.get("mbps")
        if not _is_number(value) or value <= 0:
            problems.append(Problem(BLOCKER, "evidence.mbps", spot, "evidence needs a positive mbps"))
            continue
        published = capacity.get(direction)
        if published and published.published and value > published.mbps * EVIDENCE_ABOVE_DECLARED_RATIO:
            problems.append(
                Problem(
                    WARNING,
                    "capacity.evidence_above_declared",
                    spot,
                    f"a {value:g} Mbit/s run is more than {EVIDENCE_ABOVE_DECLARED_RATIO:g}x the published "
                    f"{published.mbps:g} Mbit/s; review the rating instead of assuming either number",
                )
            )


class CapacityInventory:
    """A parsed, validated capacity document."""

    def __init__(self, document: Any) -> None:
        self.problems: list[Problem] = []
        self.document = document if isinstance(document, dict) else {}
        self.nodes: dict[str, Entry] = {}
        self.pools: dict[str, Entry] = {}
        self.bridges: dict[str, Entry] = {}
        self.session_limits: dict[str, float] = {}
        self.quota_bytes: dict[str, float] = {}
        self._parse()

    # -- parsing --------------------------------------------------------------
    def _parse(self) -> None:
        document = self.document
        if not document:
            self.problems.append(Problem(BLOCKER, "document.empty", "<root>", "capacity file is empty or not a mapping"))
            return

        self.problems.extend(find_secrets(document))

        version = document.get("version")
        if version != SCHEMA_VERSION:
            self.problems.append(
                Problem(BLOCKER, "document.version", "version", f"expected schema version {SCHEMA_VERSION}, got {version!r}")
            )

        for forbidden in ("potential_capacity", "planned_capacity", "potential"):
            if forbidden in document:
                self.problems.append(
                    Problem(
                        BLOCKER,
                        "document.potential_capacity",
                        forbidden,
                        "this file states measured and declared capacity only; capacity that does not exist yet "
                        "belongs in a planning document, not in what the alerts are computed from",
                    )
                )

        defaults = document.get("defaults") or {}
        default_sessions = defaults.get("session_limit", 0)
        default_quota = defaults.get("quota_bytes", 0)

        self._parse_nodes(document.get("nodes") or {}, default_sessions, default_quota)
        self._parse_pools(document.get("pools") or {})
        self._parse_bridges(document.get("bridges") or {})
        self._cross_check()

    def _parse_nodes(self, raw: Any, default_sessions: Any, default_quota: Any) -> None:
        if not isinstance(raw, dict):
            self.problems.append(Problem(BLOCKER, "nodes.shape", "nodes", "nodes must be a mapping"))
            return
        for name, body in raw.items():
            where = f"nodes.{name}"
            if not NAME_RE.match(str(name)):
                self.problems.append(Problem(BLOCKER, "name.format", where, f"node name {name!r} is not <UPPER>-<NN>"))
            if not isinstance(body, dict):
                self.problems.append(Problem(BLOCKER, "nodes.shape", where, "node must be a mapping"))
                continue
            capacity = _capacity_block(body.get("capacity"), where, self.problems)
            _check_evidence(body.get("evidence"), capacity, where, self.problems)
            kind, mnode, mtag = _parse_measurement(body.get("measurement"), "node", where, self.problems)
            if kind == "outbound":
                self.problems.append(
                    Problem(
                        BLOCKER,
                        "measurement.node_inbound",
                        where,
                        "a node is measured by its own inbound counters; an outbound tag describes a bridge",
                    )
                )
            if mnode and mnode != str(name):
                self.problems.append(
                    Problem(BLOCKER, "measurement.node_mismatch", where, f"measurement.node {mnode!r} is not this node")
                )
            self.session_limits[str(name)] = float(body.get("session_limit", default_sessions) or 0)
            self.quota_bytes[str(name)] = float(body.get("quota_bytes", default_quota) or 0)
            self.nodes[str(name)] = Entry(
                scope="node",
                name=str(name),
                enabled=bool(body.get("enabled", True)),
                capacity=capacity,
                measurement_kind=kind,
                measurement_node=mnode or str(name),
                measurement_tag=None,
                pool=str(body["pool"]) if body.get("pool") else None,
                serves_users=bool(body.get("serves_users", True)),
            )
            if not body.get("pool"):
                self.problems.append(Problem(BLOCKER, "nodes.pool", where, "every node belongs to a pool"))

    def _parse_pools(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            self.problems.append(Problem(BLOCKER, "pools.shape", "pools", "pools must be a mapping"))
            return
        for name, body in raw.items():
            where = f"pools.{name}"
            if not NAME_RE.match(str(name)):
                self.problems.append(Problem(BLOCKER, "name.format", where, f"pool name {name!r} is not upper-case"))
            if not isinstance(body, dict):
                self.problems.append(Problem(BLOCKER, "pools.shape", where, "pool must be a mapping"))
                continue
            members = tuple(str(member) for member in (body.get("members") or []))
            has_own_capacity = "capacity" in body
            capacity: dict[str, Capacity]
            kind = mnode = mtag = None
            if has_own_capacity:
                # Pool-level capacity: the honest answer when the source data
                # rates a location and not the machines behind it.
                capacity = _capacity_block(body.get("capacity"), where, self.problems)
                _check_evidence(body.get("evidence"), capacity, where, self.problems)
                kind, mnode, mtag = _parse_measurement(body.get("measurement"), "pool", where, self.problems)
                if kind == "outbound":
                    self.problems.append(
                        Problem(BLOCKER, "measurement.pool_inbound", where, "pool capacity is measured inbound")
                    )
                if not members and not body.get("members_unknown_reason"):
                    self.problems.append(
                        Problem(
                            BLOCKER,
                            "pools.members_unknown",
                            where,
                            "a pool with capacity and no members must say why in members_unknown_reason, "
                            "so that an empty pool is a recorded gap and not a typo",
                        )
                    )
            else:
                capacity = {}
            self.pools[str(name)] = Entry(
                scope="pool",
                name=str(name),
                enabled=bool(body.get("enabled", True)),
                capacity=capacity,
                measurement_kind=kind,
                measurement_node=mnode,
                measurement_tag=mtag,
                members=members,
                serves_users=bool(body.get("serves_users", True)),
            )

    def _parse_bridges(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            self.problems.append(Problem(BLOCKER, "bridges.shape", "bridges", "bridges must be a mapping"))
            return
        seen_tags: dict[str, str] = {}
        for name, body in raw.items():
            where = f"bridges.{name}"
            if not NAME_RE.match(str(name)):
                self.problems.append(Problem(BLOCKER, "name.format", where, f"bridge name {name!r} is not upper-case"))
            if not isinstance(body, dict):
                self.problems.append(Problem(BLOCKER, "bridges.shape", where, "bridge must be a mapping"))
                continue
            capacity = _capacity_block(body.get("capacity"), where, self.problems)
            _check_evidence(body.get("evidence"), capacity, where, self.problems)
            kind, mnode, mtag = _parse_measurement(body.get("measurement"), "bridge", where, self.problems)
            if kind != "outbound":
                self.problems.append(
                    Problem(
                        BLOCKER,
                        "measurement.bridge_outbound",
                        where,
                        "a bridge is measured by the source node's outbound counter for its own tag",
                    )
                )
            declared_tag = body.get("outbound_tag")
            if declared_tag and mtag and declared_tag != mtag:
                self.problems.append(
                    Problem(BLOCKER, "bridges.tag_mismatch", where, "outbound_tag and measurement.tag disagree")
                )
            tag = str(declared_tag or mtag or "")
            if tag:
                if tag in seen_tags:
                    self.problems.append(
                        Problem(
                            BLOCKER,
                            "bridges.tag_shared",
                            where,
                            f"outbound tag {tag} is already used by bridge {seen_tags[tag]}; "
                            "two bridges on one tag cannot be told apart",
                        )
                    )
                seen_tags[tag] = str(name)
            source_node = body.get("source_node")
            if mnode and source_node and mnode != source_node:
                self.problems.append(
                    Problem(
                        BLOCKER,
                        "bridges.source_mismatch",
                        where,
                        "measurement.node must be the source node: usage is counted where the bridge leaves",
                    )
                )
            self.bridges[str(name)] = Entry(
                scope="bridge",
                name=str(name),
                enabled=bool(body.get("enabled", True)),
                capacity=capacity,
                measurement_kind=kind,
                measurement_node=str(source_node or mnode or ""),
                measurement_tag=tag or None,
                source_node=str(source_node) if source_node else None,
                destination_node=str(body.get("destination_node")) if body.get("destination_node") else None,
            )

    def _cross_check(self) -> None:
        for name, node in self.nodes.items():
            if node.pool and node.pool not in self.pools:
                self.problems.append(
                    Problem(BLOCKER, "nodes.pool_unknown", f"nodes.{name}", f"pool {node.pool!r} is not declared")
                )
        owner: dict[str, str] = {}
        for pool_name, pool in self.pools.items():
            for member in pool.members:
                if member not in self.nodes:
                    self.problems.append(
                        Problem(BLOCKER, "pools.member_unknown", f"pools.{pool_name}", f"member {member!r} is not a node")
                    )
                    continue
                if member in owner:
                    self.problems.append(
                        Problem(
                            BLOCKER,
                            "pools.member_shared",
                            f"pools.{pool_name}",
                            f"{member} is already a member of {owner[member]}; a node belongs to one pool",
                        )
                    )
                owner[member] = pool_name
                if self.nodes[member].pool != pool_name:
                    self.problems.append(
                        Problem(
                            BLOCKER,
                            "pools.member_disagrees",
                            f"pools.{pool_name}",
                            f"{member} lists pool {self.nodes[member].pool!r} but is a member here",
                        )
                    )
            if pool.capacity:
                # A pool states capacity only when its members cannot be rated
                # individually. Naming the members is fine and desirable - the
                # dashboard needs them, and a location whose machines are known
                # but unmeasured is the normal case. What is refused is a pool
                # figure standing next to a member figure, because then the same
                # gigabits are in both and the pool total counts them twice.
                rated_members = sorted(
                    member
                    for member in pool.members
                    if member in self.nodes
                    and any(capacity.published for capacity in self.nodes[member].capacity.values())
                )
                if rated_members:
                    self.problems.append(
                        Problem(
                            BLOCKER,
                            "pools.capacity_and_members",
                            f"pools.{pool_name}",
                            f"the pool states its own capacity while {', '.join(rated_members)} "
                            "also publish theirs; the pool total would count the same capacity twice. "
                            "Rate the members and drop the pool figure, or leave the members unrated",
                        )
                    )
        for name, node in self.nodes.items():
            shared = [
                direction
                for direction, capacity in node.capacity.items()
                if capacity.source == "shared_pool"
            ]
            if not shared:
                continue
            pool = self.pools.get(node.pool or "")
            if pool is None or not pool.capacity:
                self.problems.append(
                    Problem(
                        BLOCKER,
                        "capacity.shared_pool_missing",
                        f"nodes.{name}",
                        f"{sorted(shared)} are accounted at pool {node.pool!r}, but that pool states "
                        "no capacity of its own; the node points at a figure that does not exist",
                    )
                )
                continue
            for direction in shared:
                pool_capacity = pool.capacity.get(direction)
                if pool_capacity is None or not pool_capacity.published:
                    self.problems.append(
                        Problem(
                            BLOCKER,
                            "capacity.shared_pool_missing",
                            f"nodes.{name}",
                            f"{direction} is accounted at pool {node.pool}, which publishes no "
                            f"{direction} figure",
                        )
                    )
                elif not pool_capacity.aggregate:
                    self.problems.append(
                        Problem(
                            BLOCKER,
                            "capacity.shared_pool_attribution",
                            f"pools.{node.pool}",
                            f"{name} defers its {direction} capacity here, so this figure covers more "
                            "than one machine and must be declared attribution: aggregate",
                        )
                    )

        for name, bridge in self.bridges.items():
            for role, value in (("source_node", bridge.source_node), ("destination_node", bridge.destination_node)):
                if not value:
                    self.problems.append(Problem(BLOCKER, f"bridges.{role}", f"bridges.{name}", f"{role} is required"))
                elif value not in self.nodes:
                    self.problems.append(
                        Problem(BLOCKER, f"bridges.{role}_unknown", f"bridges.{name}", f"{role} {value!r} is not a node")
                    )

    # -- results --------------------------------------------------------------
    @property
    def blockers(self) -> list[Problem]:
        return [problem for problem in self.problems if problem.severity == BLOCKER]

    @property
    def warnings(self) -> list[Problem]:
        return [problem for problem in self.problems if problem.severity == WARNING]

    @property
    def valid(self) -> bool:
        return not self.blockers

    def entries(self) -> Iterator[Entry]:
        yield from self.nodes.values()
        yield from (pool for pool in self.pools.values() if pool.capacity)
        yield from self.bridges.values()

    # -- node state -----------------------------------------------------------
    @staticmethod
    def _require_active_set(active: Any) -> set[str]:
        """Refuse the one mistake that silently inflates every total.

        node_states()/active_nodes() take the panel's state, keyed by node; the
        capacity functions take the set of names that came out. Handing the
        state mapping straight back would test membership against its keys -
        every node, including the ones that are off - and quietly report the
        full fleet as available. That is the exact failure this module exists to
        prevent, so it raises rather than guessing.
        """
        if isinstance(active, dict):
            raise TypeError(
                "pass active_nodes(runtime) - a set of node names - not the runtime state mapping; "
                "the mapping contains inactive nodes as keys and would be counted as available"
            )
        return set(active)

    def node_state(self, name: str, runtime: dict[str, dict[str, Any]] | None = None) -> dict[str, bool]:
        """The five states of a node, kept apart on purpose.

        They answer different questions and one must never stand in for another:

        ``configured``   this file knows the node at all.
        ``known``        the panel knows a node of this name.
        ``enabled``      nobody has taken it out by hand, here or in the panel.
        ``connected``    the panel reports it connected right now.
        ``active``       it counts towards active capacity: enabled and connected.
        ``disabled``     taken out deliberately - administratively, not broken.

        A disabled node must not raise "offline": somebody meant it. A node that
        is enabled and disconnected must raise health, and must leave capacity.
        Both leave active capacity, for different reasons.
        """
        runtime = runtime or {}
        node = self.nodes.get(name)
        observed = runtime.get(name)
        configured = node is not None
        inventory_enabled = bool(node.enabled) if node else False
        known = observed is not None
        panel_disabled = bool(observed.get("is_disabled")) if observed else False
        connected = bool(observed.get("is_connected")) if observed else False
        disabled = (configured and not inventory_enabled) or panel_disabled
        enabled = configured and inventory_enabled and not panel_disabled
        return {
            "configured": configured,
            "known": known,
            "enabled": enabled,
            "connected": connected and known,
            "disabled": disabled,
            # Unknown to the panel is not active: capacity that cannot be
            # observed is capacity nobody should plan against.
            "active": enabled and known and connected,
        }

    def node_states(self, runtime: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, bool]]:
        return {name: self.node_state(name, runtime) for name in self.nodes}

    def active_nodes(self, runtime: dict[str, dict[str, Any]] | None = None) -> set[str]:
        return {name for name, state in self.node_states(runtime).items() if state["active"]}

    # -- capacity -------------------------------------------------------------
    def pool_capacity(self, pool_name: str, direction: str, active: Iterable[str]) -> tuple[float | None, bool]:
        """Active capacity of one pool, and whether that number is trustworthy.

        Returns ``(value, certain)``. ``certain`` is False when the pool's
        figure covers more than one machine and at least one of them is not
        active: the total is known, the share that just went away is not, and
        subtracting a guess would be worse than admitting it. The caller shows
        uncertainty rather than a number.
        """
        active = self._require_active_set(active)
        pool = self.pools.get(pool_name)
        if pool is None:
            return None, True

        if pool.capacity:
            if not pool.enabled:
                return 0.0, True
            capacity = pool.capacity.get(direction)
            if capacity is None or not capacity.published:
                return None, True
            members_down = [member for member in pool.members if member not in active]
            if pool.members and len(members_down) == len(pool.members):
                # Nothing left at that location to carry it.
                return 0.0, True
            if capacity.aggregate and members_down:
                # The figure still stands as an upper bound, and how much of it
                # survived is unknown. Certain=False is the honest answer.
                return capacity.mbps, False
            return capacity.mbps, True

        total = 0.0
        rated = False
        for member in pool.members:
            if member not in active:
                continue
            capacity = self.nodes[member].capacity.get(direction)
            if capacity and capacity.published:
                total += capacity.mbps
                rated = True
        return (total if rated else None), True

    def pool_active_capacity(self, pool_name: str, direction: str, active: Iterable[str]) -> float | None:
        """Backwards-compatible view of pool_capacity that drops the certainty."""
        return self.pool_capacity(pool_name, direction, active)[0]

    def service_capacity(self, direction: str, active: Iterable[str]) -> float:
        """Capacity of the service users actually reach.

        Summed over the pools users connect to. A pool that exists only as the
        far end of a bridge is left out: its throughput carries traffic that was
        already counted where the user entered, so adding it would describe the
        infrastructure rather than the service. Bridges are never in this sum for
        the same reason - they are reported on their own.
        """
        active = self._require_active_set(active)
        total = 0.0
        for pool_name, pool in self.pools.items():
            if not pool.serves_users:
                continue
            value, _certain = self.pool_capacity(pool_name, direction, active)
            if value is not None:
                total += value
        return total

    def physical_capacity(self, direction: str, active: Iterable[str]) -> float:
        """Every rated leg of the infrastructure, bridges included.

        A diagnostic figure and never the fleet KPI: the same user gigabit
        appears in it two or three times by design, which is what makes it
        useful for capacity planning of the plumbing and useless as a measure of
        the service.
        """
        active = self._require_active_set(active)
        total = 0.0
        for pool_name in self.pools:
            value, _certain = self.pool_capacity(pool_name, direction, active)
            if value is not None:
                total += value
        for bridge in self.bridges.values():
            if not bridge.enabled:
                continue
            if bridge.source_node not in active or bridge.destination_node not in active:
                continue
            capacity = bridge.capacity.get(direction)
            if capacity and capacity.published:
                total += capacity.mbps
        return total

    def fleet_active_capacity(self, direction: str, active: Iterable[str]) -> float:
        """The service figure. Kept under its old name for existing callers."""
        return self.service_capacity(direction, active)

    def service_capacity_certain(self, direction: str, active: Iterable[str]) -> bool:
        active = self._require_active_set(active)
        return all(
            self.pool_capacity(name, direction, active)[1]
            for name, pool in self.pools.items()
            if pool.serves_users
        )

    # -- topology -------------------------------------------------------------
    def topology_drift(
        self,
        panel_nodes: Iterable[str] | None = None,
        ansible_nodes: Iterable[str] | None = None,
    ) -> dict[str, list[str]]:
        """Where the three registries disagree about which nodes exist.

        A new production node that is in the panel and not here is a node with
        no capacity row, no pool and no place on the dashboard - it disappears
        silently, which is the failure this exists to make loud. The reverse, a
        row here that the panel does not have, is a figure counted towards
        nothing.

        Names only, so the metrics built from this stay low-cardinality: the
        counts are what alerts on, the names are for the message.
        """
        capacity = set(self.nodes)
        panel = set(panel_nodes) if panel_nodes is not None else None
        ansible = set(ansible_nodes) if ansible_nodes is not None else None
        drift: dict[str, list[str]] = {
            "missing_from_capacity": sorted(panel - capacity) if panel is not None else [],
            "missing_from_panel": sorted(capacity - panel) if panel is not None else [],
            "missing_from_ansible": sorted(capacity - ansible) if ansible is not None else [],
            "missing_from_capacity_but_in_ansible": sorted(ansible - capacity) if ansible is not None else [],
        }
        return drift


def validate(document: Any) -> CapacityInventory:
    return CapacityInventory(document)


def format_problems(problems: Sequence[Problem]) -> str:
    return "\n".join(str(problem) for problem in problems)
