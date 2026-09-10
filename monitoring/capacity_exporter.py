#!/usr/bin/env python3
"""Prometheus exporter for August VPN capacity.

It publishes three things Prometheus cannot work out on its own:

  1. what each node, pool and bridge is rated to carry, split by direction and
     labelled with the counter the rating is meant to be compared against;
  2. which nodes count right now, so that a disabled or disconnected node drops
     out of the fleet total automatically and comes back automatically;
  3. when each node first came up, which is the only honest way to measure how
     long a new node took to enter service.

It is read-only against Remnawave and against the capacity file. It never
enables a node, never writes to the panel and never prints a credential: the
panel token arrives in the environment from a 0600 file and is used for exactly
one GET.

Bridges are published with scope="bridge" and are deliberately left out of
august_service_capacity_mbps: their traffic is already inside the throughput of
the nodes at both ends, so adding them would count the same gigabits twice. What
a bridge row answers is a different question - how much of that one link is left
- and it is answered on its own. august_physical_capacity_mbps is the other
figure, every leg included, and it exists as a diagnostic for the plumbing
rather than as a statement about the service.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import capacity_model  # noqa: E402  (path set above)
import strict_yaml  # noqa: E402  (path set above)

EXPORTER_VERSION = "2"
DIRECTIONS = capacity_model.DIRECTIONS


def escape_label(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_value(value: Any) -> str:
    """One sample value, at full precision.

    This used to be f"{value:g}", and %g rounds to six significant digits. That
    is harmless for a percentage and wrong for everything large: a Unix
    timestamp of 1788293189 was published as 1.78829e+09, which reads back as
    1788290000 - the metric claimed the exporter had been silent for 3189
    seconds while it was answering normally, and AugustCapacityExporterStale
    fired on a healthy process. Byte counters were quantised the same way.

    So: integral values are written as integers, which is exact and adds no
    decimal noise, and everything else uses repr(), which in Python 3 is the
    shortest string that round-trips back to the same float. NaN and the
    infinities are spelled the way the exposition format requires; anything that
    is not a number at all becomes NaN rather than a line Prometheus rejects.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "NaN"
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "+Inf" if number > 0 else "-Inf"
    # 2**53 is where a float stops being able to hold consecutive integers, so
    # beyond it "looks integral" no longer means "is exactly this integer".
    if number.is_integer() and abs(number) < 2**53:
        return str(int(number))
    return repr(number)


def render_sample(name: str, labels: dict[str, Any], value: float) -> str:
    pairs = ",".join(f'{key}="{escape_label(labels[key])}"' for key in sorted(labels) if labels[key] not in (None, ""))
    body = f"{{{pairs}}}" if pairs else ""
    return f"{name}{body} {format_value(value)}"


class MetricSet:
    """Samples grouped by family.

    Prometheus wants every sample of a family together, behind one HELP and one
    TYPE. Collecting them here instead of appending lines in scrape order keeps
    the exposition valid however the collection below is reordered later.
    """

    def __init__(self) -> None:
        self._families: dict[str, dict[str, Any]] = {}

    def declare(self, name: str, help_text: str, metric_type: str = "gauge") -> None:
        self._families.setdefault(name, {"help": help_text, "type": metric_type, "samples": []})

    def add(self, name: str, labels: dict[str, Any], value: float) -> None:
        if name not in self._families:
            raise KeyError(f"metric {name} was not declared")
        self._families[name]["samples"].append(render_sample(name, labels, value))

    def render(self) -> str:
        lines: list[str] = []
        for name, family in self._families.items():
            lines.append(f"# HELP {name} {family['help']}")
            lines.append(f"# TYPE {name} {family['type']}")
            lines.extend(family["samples"])
        return "\n".join(lines) + "\n"


class RemnawaveClient:
    """The one read this exporter needs from the panel."""

    def __init__(self, base_url: str, token: str, timeout: float = 15.0, verify: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.verify = verify

    def nodes(self) -> list[dict[str, Any]]:
        if not self.base_url or not self.token:
            raise RuntimeError("panel url or token is not configured")
        request = urllib.request.Request(
            f"{self.base_url}/api/nodes",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            method="GET",
        )
        context = None
        if not self.verify:  # pragma: no cover - only for a lab panel
            import ssl

            context = ssl._create_unverified_context()
        with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
            payload = json.loads(response.read().decode("utf-8"))
        items = payload.get("response", payload)
        if isinstance(items, dict):
            items = items.get("nodes", [])
        return [item for item in items if isinstance(item, dict)]


def _timestamp(value: Any) -> float | None:
    """Remnawave returns ISO 8601; be forgiving about the shapes of it."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1000.0 if value > 1e11 else float(value)
    text = str(value).strip().replace("Z", "+00:00")
    for parser in (lambda t: __import__("datetime").datetime.fromisoformat(t),):
        try:
            return parser(text).timestamp()
        except ValueError:
            continue
    return None


class FirstSeenStore:
    """When each node was first observed connected.

    lastStatusChange cannot answer this - it moves on every flap - so the first
    transition to connected is recorded once and never rewritten.
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS first_connected (node TEXT PRIMARY KEY, at REAL NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def record(self, node: str, when: float) -> float:
        with self._lock, self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO first_connected (node, at) VALUES (?, ?)", (node, when))
            row = connection.execute("SELECT at FROM first_connected WHERE node = ?", (node,)).fetchone()
        return float(row[0]) if row else when

    def get(self, node: str) -> float | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT at FROM first_connected WHERE node = ?", (node,)).fetchone()
        return float(row[0]) if row else None


class CapacityCollector:
    def __init__(
        self,
        capacity_path: pathlib.Path,
        client: RemnawaveClient | None,
        store: FirstSeenStore | None,
        node_label: str = "node",
        targets_path: pathlib.Path | None = None,
        panel_staleness_seconds: float = 600.0,
    ) -> None:
        self.capacity_path = capacity_path
        self.client = client
        self.store = store
        self.node_label = node_label
        # Prometheus's own file_sd list of node_exporter targets, which is
        # the third view of the fleet beside the panel and the capacity
        # inventory. Read only to notice that the three disagree; this exporter
        # never writes it.
        self.targets_path = targets_path
        self.panel_staleness_seconds = panel_staleness_seconds
        self._last_good: capacity_model.CapacityInventory | None = None
        self._last_good_at: float | None = None
        self._last_panel_state: dict[str, dict[str, Any]] = {}
        self._last_panel_at: float | None = None

    # -- inputs ---------------------------------------------------------------
    def load_inventory(self) -> tuple[capacity_model.CapacityInventory, bool, bool]:
        """Re-read on every scrape so an edit shows up without a restart.

        Two failure modes, one answer. A file that no longer parses, and a file
        that parses but has a validation blocker in it, are both refused: what
        keeps serving is the last inventory that was good, and
        august_capacity_inventory_valid goes to 0 so the operator is told. The
        alternative - publishing a semantically broken inventory because it
        happened to be syntactically valid - is how a capacity total silently
        becomes wrong.

        Returns (inventory, valid, last_good_in_use).
        """
        try:
            document = strict_yaml.load(self.capacity_path.read_text(encoding="utf-8"))
        except (OSError, strict_yaml.StrictYamlError) as error:
            broken = capacity_model.validate(None)
            broken.problems.append(
                capacity_model.Problem(
                    capacity_model.BLOCKER, "document.unreadable", str(self.capacity_path), str(error)
                )
            )
            if self._last_good is not None:
                return self._last_good, False, True
            return broken, False, False

        inventory = capacity_model.validate(document)
        if inventory.valid:
            self._last_good = inventory
            self._last_good_at = time.time()
            return inventory, True, False
        if self._last_good is not None:
            return self._last_good, False, True
        return inventory, False, False

    def panel_state(self) -> tuple[dict[str, dict[str, Any]], bool, float | None, bool]:
        """The panel's view, with the last good one kept for a while.

        A five-second blip on the panel API must not take the whole fleet out of
        active capacity: that would turn one HTTP error into a fleet-wide RED,
        and the operator would learn to ignore it. So the last good answer keeps
        being used until it is older than panel_staleness_seconds; after that it
        stops being used at all, because a state nobody has confirmed for ten
        minutes is not evidence that anything is healthy.

        Returns (state, fresh, age_seconds, stale).
        """
        if self.client is None:
            return {}, False, None, True
        try:
            nodes = self.client.nodes()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError, ValueError, OSError):
            age = None if self._last_panel_at is None else time.time() - self._last_panel_at
            if age is not None and age <= self.panel_staleness_seconds:
                # Within the window: keep answering with what was last true.
                return dict(self._last_panel_state), False, age, False
            return {}, False, age, True
        state: dict[str, dict[str, Any]] = {}
        for node in nodes:
            name = node.get("name")
            if not name:
                continue
            state[str(name)] = {
                "uuid": str(node.get("uuid") or ""),
                "is_connected": bool(node.get("isConnected")),
                "is_disabled": bool(node.get("isDisabled")),
                "created_at": _timestamp(node.get("createdAt")),
                "traffic_used_bytes": node.get("trafficUsedBytes"),
                "traffic_limit_bytes": node.get("trafficLimitBytes"),
                "users_online": node.get("usersOnline"),
                "xray_version": node.get("xrayVersion"),
            }
        self._last_panel_state = state
        self._last_panel_at = time.time()
        return dict(state), True, 0.0, False

    def ansible_nodes(self) -> set[str] | None:
        """Node names the Ansible inventory knows, from the file_sd targets file.

        None when the file is absent or unreadable: "we do not know" is a
        different answer from "the inventory is empty", and only the second one
        is drift.
        """
        if self.targets_path is None:
            return None
        try:
            entries = json.loads(self.targets_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        names: set[str] = set()
        for entry in entries if isinstance(entries, list) else []:
            labels = entry.get("labels") if isinstance(entry, dict) else None
            if isinstance(labels, dict) and labels.get(self.node_label):
                names.add(str(labels[self.node_label]))
        return names or None

    # -- output ---------------------------------------------------------------
    def collect(self) -> str:
        started = time.time()
        inventory, inventory_valid, last_good_in_use = self.load_inventory()
        state, panel_fresh, panel_age, panel_stale = self.panel_state()
        usable_state = {} if panel_stale else state
        states = inventory.node_states(usable_state)
        active = {name for name, node in states.items() if node["active"]}
        metrics = MetricSet()

        # --- the exporter itself -------------------------------------------
        metrics.declare("august_capacity_exporter_build_info", "Exporter build information.")
        metrics.add("august_capacity_exporter_build_info", {"version": EXPORTER_VERSION}, 1)
        metrics.declare(
            "august_capacity_exporter_timestamp_seconds",
            "When this collection ran. A scrape that succeeds while this stops moving means the "
            "exporter is serving a cached answer.",
        )
        metrics.add("august_capacity_exporter_timestamp_seconds", {}, started)

        # --- the inventory --------------------------------------------------
        metrics.declare("august_capacity_inventory_valid", "1 when the capacity inventory in use has no blockers.")
        metrics.add("august_capacity_inventory_valid", {}, 1 if inventory_valid else 0)
        metrics.declare(
            "august_capacity_inventory_last_good_in_use",
            "1 when the file on disk was refused and an earlier, valid inventory is being served.",
        )
        metrics.add("august_capacity_inventory_last_good_in_use", {}, 1 if last_good_in_use else 0)
        metrics.declare("august_capacity_inventory_problems", "Validation findings, by severity.")
        metrics.add("august_capacity_inventory_problems", {"severity": "blocker"}, len(inventory.blockers))
        metrics.add("august_capacity_inventory_problems", {"severity": "warning"}, len(inventory.warnings))
        metrics.declare(
            "august_capacity_inventory_timestamp_seconds",
            "When the inventory being served was last read and accepted.",
        )
        if self._last_good_at is not None:
            metrics.add("august_capacity_inventory_timestamp_seconds", {}, self._last_good_at)

        # --- the panel ------------------------------------------------------
        metrics.declare("august_capacity_panel_reachable", "1 when the last read of the panel succeeded.")
        metrics.add("august_capacity_panel_reachable", {}, 1 if panel_fresh else 0)
        metrics.declare(
            "august_capacity_panel_state_age_seconds",
            "How old the node state in use is. Within the staleness window the last good answer is "
            "used, so one HTTP error does not take the fleet out of active capacity.",
        )
        if panel_age is not None:
            metrics.add("august_capacity_panel_state_age_seconds", {}, panel_age)
        metrics.declare(
            "august_capacity_panel_state_stale",
            "1 when no confirmed node state is available. Every node then reads as unknown rather "
            "than healthy.",
        )
        metrics.add("august_capacity_panel_state_stale", {}, 1 if panel_stale else 0)

        # --- identity: uuid <-> name ----------------------------------------
        # Remnawave's traffic and status series are labelled by node_uuid, not by
        # name. This is the mapping series the recording rules join on. It is
        # published from the panel's own REST answer, so a UUID that the metrics
        # endpoint reports and this does not is a mapping gap the rules can see.
        metrics.declare(
            "august_node_identity",
            "Mapping from the panel's node UUID to the fleet name. Join Remnawave series on node_uuid.",
        )
        metrics.declare(
            "august_node_identity_problems",
            "Mapping problems, by kind: a UUID or a name seen twice, or a node with no UUID.",
        )
        uuid_owners: dict[str, list[str]] = {}
        missing_uuid: list[str] = []
        for name in sorted(state):
            node_uuid = str(state[name].get("uuid") or "")
            if not node_uuid:
                missing_uuid.append(name)
                continue
            uuid_owners.setdefault(node_uuid, []).append(name)
        for node_uuid, names in sorted(uuid_owners.items()):
            for name in names:
                metrics.add(
                    "august_node_identity",
                    {self.node_label: name, "node_uuid": node_uuid, "pool": self._pool_of(inventory, name)},
                    1,
                )
        duplicate_uuid = sum(1 for names in uuid_owners.values() if len(names) > 1)
        name_counts: dict[str, int] = {}
        for names in uuid_owners.values():
            for name in names:
                name_counts[name] = name_counts.get(name, 0) + 1
        duplicate_name = sum(1 for count in name_counts.values() if count > 1)
        metrics.add("august_node_identity_problems", {"kind": "duplicate_uuid"}, duplicate_uuid)
        metrics.add("august_node_identity_problems", {"kind": "duplicate_name"}, duplicate_name)
        metrics.add("august_node_identity_problems", {"kind": "missing_uuid"}, len(missing_uuid))
        metrics.declare(
            "august_node_identity_mapped",
            "1 when this node has a usable UUID mapping. 0 means its Remnawave series cannot be "
            "attributed and must not be silently folded into an empty label.",
        )
        for name in sorted(inventory.nodes):
            mapped = 1 if any(name in names for names in uuid_owners.values()) else 0
            metrics.add("august_node_identity_mapped", {self.node_label: name}, mapped)

        # --- topology drift --------------------------------------------------
        drift = inventory.topology_drift(
            panel_nodes=set(state) if not panel_stale and state else None,
            ansible_nodes=self.ansible_nodes(),
        )
        metrics.declare(
            "august_topology_drift",
            "How many nodes the three registries disagree about: the panel, this capacity "
            "inventory and the Ansible inventory.",
        )
        metrics.declare(
            "august_topology_drift_node",
            "One series per node that is missing from one of the registries. Normally empty, so the "
            "cardinality is bounded by how broken things are.",
        )
        for kind, names in sorted(drift.items()):
            metrics.add("august_topology_drift", {"kind": kind}, len(names))
            for name in names:
                metrics.add("august_topology_drift_node", {"kind": kind, self.node_label: name}, 1)

        # --- node state, six separate questions ------------------------------
        metrics.declare("august_node_configured", "1 when the capacity inventory knows this node.")
        metrics.declare("august_node_known_to_panel", "1 when the panel knows a node of this name.")
        metrics.declare("august_node_enabled", "1 when the node is not disabled here or in the panel.")
        metrics.declare("august_node_connected", "1 when the panel reports the node connected.")
        metrics.declare(
            "august_node_administratively_disabled",
            "1 when somebody took the node out on purpose. Deliberate, so it must not raise offline.",
        )
        metrics.declare("august_node_active", "1 when the node counts towards active capacity.")
        metrics.declare("august_node_session_limit", "Established-session ceiling; 0 when none has been set.")
        for name in sorted(inventory.nodes):
            node = inventory.nodes[name]
            labels = {self.node_label: name, "pool": node.pool}
            node_state = states[name]
            metrics.add("august_node_configured", labels, 1 if node_state["configured"] else 0)
            metrics.add("august_node_known_to_panel", labels, 1 if node_state["known"] else 0)
            metrics.add("august_node_enabled", labels, 1 if node_state["enabled"] else 0)
            metrics.add("august_node_connected", labels, 1 if node_state["connected"] else 0)
            metrics.add("august_node_administratively_disabled", labels, 1 if node_state["disabled"] else 0)
            metrics.add("august_node_active", labels, 1 if node_state["active"] else 0)
            metrics.add("august_node_session_limit", labels, inventory.session_limits.get(name, 0.0))

        # --- node facts from the panel ---------------------------------------
        metrics.declare("august_node_traffic_used_bytes", "Traffic the panel has counted for this node.")
        metrics.declare("august_node_traffic_limit_bytes", "Traffic quota; 0 means unlimited.")
        metrics.declare("august_node_created_timestamp_seconds", "When the panel first held this node.")
        metrics.declare(
            "august_node_first_observed_connected_timestamp_seconds",
            "First time THIS EXPORTER observed the node connected. Not the moment it first came up: "
            "nothing in the panel records that, and this series starts when the exporter does.",
        )
        for name in sorted(inventory.nodes):
            node = inventory.nodes[name]
            labels = {self.node_label: name, "pool": node.pool}
            observed = usable_state.get(name)
            if not observed:
                continue
            used = observed.get("traffic_used_bytes")
            limit = observed.get("traffic_limit_bytes")
            if isinstance(used, (int, float)) and not isinstance(used, bool):
                metrics.add("august_node_traffic_used_bytes", labels, float(used))
            if isinstance(limit, (int, float)) and not isinstance(limit, bool):
                metrics.add("august_node_traffic_limit_bytes", labels, float(limit))
            created = observed.get("created_at")
            if created:
                metrics.add("august_node_created_timestamp_seconds", labels, created)
            if self.store is not None:
                first = self.store.record(name, started) if observed["is_connected"] else self.store.get(name)
                if first is not None:
                    metrics.add("august_node_first_observed_connected_timestamp_seconds", labels, first)

        # --- rated capacity, as stated ---------------------------------------
        metrics.declare(
            "august_capacity_mbps",
            "Rated capacity by direction, labelled with the counter the rating is compared against.",
        )
        metrics.declare("august_capacity_unrated", "1 for an entry and direction that carries no rating at all.")
        metrics.declare(
            "august_capacity_shared_pool",
            "1 for a node whose capacity is accounted at its pool because the source data rates the "
            "location and not the machine.",
        )
        metrics.declare("august_capacity_range_bound_mbps", "Both ends of a rating that is a range, for reference.")
        for entry in inventory.entries():
            base = {
                "scope": entry.scope,
                "name": entry.name,
                self.node_label: entry.measurement_node,
                "tag": entry.measurement_tag,
                "kind": entry.measurement_kind,
            }
            for direction in DIRECTIONS:
                capacity = entry.capacity.get(direction)
                if capacity is None:
                    continue
                labels = dict(base, direction=direction, source=capacity.source)
                if capacity.published:
                    metrics.add(
                        "august_capacity_mbps",
                        dict(
                            labels,
                            conservative="true" if capacity.conservative else "false",
                            attribution=capacity.attribution,
                        ),
                        capacity.mbps,
                    )
                elif capacity.source == "shared_pool":
                    metrics.add(
                        "august_capacity_shared_pool",
                        {"scope": entry.scope, "name": entry.name, "direction": direction, "pool": entry.pool},
                        1,
                    )
                else:
                    metrics.add("august_capacity_unrated", labels, 1)
                if capacity.bound_min is not None:
                    metrics.add("august_capacity_range_bound_mbps", dict(labels, bound="min"), capacity.bound_min)
                    metrics.add("august_capacity_range_bound_mbps", dict(labels, bound="max"), capacity.bound_max)

        # --- pools: resource capacity of one service pool --------------------
        metrics.declare("august_pool_members", "Nodes declared in the pool.")
        metrics.declare("august_pool_active_members", "Nodes of the pool that count right now.")
        metrics.declare("august_pool_rates_itself", "1 when capacity is stated at the pool instead of per node.")
        metrics.declare(
            "august_pool_capacity_aggregate",
            "1 when the pool's figure covers several machines and the per-machine share is unknown.",
        )
        metrics.declare(
            "august_pool_serves_users",
            "1 when users connect to this pool. A pool that is only the far end of a bridge is 0 and "
            "is left out of service capacity.",
        )
        metrics.declare("august_pool_capacity_mbps", "Active capacity of the pool, by direction.")
        metrics.declare(
            "august_pool_capacity_certain",
            "0 when the pool total is known but the share lost with a missing member is not. The "
            "figure is then an upper bound and the status must read as uncertain, not as a number.",
        )
        for pool_name in sorted(inventory.pools):
            pool = inventory.pools[pool_name]
            labels = {"pool": pool_name}
            metrics.add("august_pool_members", labels, len(pool.members))
            metrics.add("august_pool_active_members", labels, len([m for m in pool.members if m in active]))
            metrics.add("august_pool_rates_itself", labels, 1 if pool.capacity else 0)
            metrics.add("august_pool_serves_users", labels, 1 if pool.serves_users else 0)
            for direction in DIRECTIONS:
                capacity = pool.capacity.get(direction) if pool.capacity else None
                metrics.add(
                    "august_pool_capacity_aggregate",
                    dict(labels, direction=direction),
                    1 if (capacity is not None and capacity.aggregate) else 0,
                )
                value, certain = inventory.pool_capacity(pool_name, direction, active)
                if value is not None:
                    metrics.add("august_pool_capacity_mbps", dict(labels, direction=direction), value)
                    metrics.add("august_pool_capacity_certain", dict(labels, direction=direction), 1 if certain else 0)

        # --- the two fleet figures, and they are not the same ----------------
        metrics.declare(
            "august_service_capacity_mbps",
            "Capacity of the service users reach: summed over the pools users connect to. Bridges and "
            "bridge-only exits are not in it - their throughput carries traffic already counted where "
            "the user entered.",
        )
        metrics.declare(
            "august_service_capacity_certain",
            "0 when at least one pool in the service figure is an aggregate with a member missing.",
        )
        metrics.declare(
            "august_physical_capacity_mbps",
            "Every rated leg, bridges included. A diagnostic figure: one user gigabit appears in it "
            "two or three times by design, so it is never the fleet KPI.",
        )
        for direction in DIRECTIONS:
            metrics.add(
                "august_service_capacity_mbps",
                {"direction": direction},
                inventory.service_capacity(direction, active),
            )
            metrics.add(
                "august_service_capacity_certain",
                {"direction": direction},
                1 if inventory.service_capacity_certain(direction, active) else 0,
            )
            metrics.add(
                "august_physical_capacity_mbps",
                {"direction": direction},
                inventory.physical_capacity(direction, active),
            )

        # --- bridges ---------------------------------------------------------
        metrics.declare("august_bridge_info", "Bridge topology, for joining a bridge row to the nodes at its ends.")
        metrics.declare("august_bridge_enabled", "1 when the bridge is enabled and both of its nodes count.")
        for name in sorted(inventory.bridges):
            bridge = inventory.bridges[name]
            metrics.add(
                "august_bridge_info",
                {
                    "name": name,
                    self.node_label: bridge.source_node,
                    "source_node": bridge.source_node,
                    "destination_node": bridge.destination_node,
                    "tag": bridge.measurement_tag,
                },
                1,
            )
            both_up = bridge.enabled and bridge.source_node in active and bridge.destination_node in active
            metrics.add("august_bridge_enabled", {"name": name}, 1 if both_up else 0)

        metrics.declare("august_capacity_exporter_scrape_duration_seconds", "How long this collection took.")
        metrics.add("august_capacity_exporter_scrape_duration_seconds", {}, time.time() - started)
        return metrics.render()

    @staticmethod
    def _pool_of(inventory: capacity_model.CapacityInventory, name: str) -> str:
        node = inventory.nodes.get(name)
        return node.pool or "" if node else ""


class MetricsHandler(BaseHTTPRequestHandler):
    collector: CapacityCollector

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        try:
            body = self.collector.collect().encode("utf-8")
        except Exception as error:  # pragma: no cover - last resort
            self.send_error(500, explain=str(error))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # A scrape every 15 seconds is not news, and the query string could
        # otherwise reach the journal.
        return


def build_collector(arguments: argparse.Namespace) -> CapacityCollector:
    token = os.environ.get("REMNAWAVE_PANEL_TOKEN", "")
    base = os.environ.get("REMNAWAVE_PANEL_URL", "")
    client = RemnawaveClient(base, token, timeout=arguments.panel_timeout, verify=not arguments.panel_insecure) if base and token else None
    store = FirstSeenStore(arguments.state) if arguments.state else None
    return CapacityCollector(
        arguments.capacity,
        client,
        store,
        node_label=arguments.node_label,
        targets_path=arguments.targets,
        panel_staleness_seconds=arguments.panel_staleness,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capacity", type=pathlib.Path, required=True)
    parser.add_argument("--listen-address", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=9301)
    parser.add_argument("--state", type=pathlib.Path, default=None)
    parser.add_argument("--node-label", default="node")
    parser.add_argument("--panel-timeout", type=float, default=15.0)
    parser.add_argument("--panel-insecure", action="store_true")
    parser.add_argument(
        "--targets",
        type=pathlib.Path,
        default=None,
        help="node_exporter file_sd file, read to notice that the registries disagree",
    )
    parser.add_argument(
        "--panel-staleness",
        type=float,
        default=600.0,
        help="how long a last-good panel answer may keep being used before every node reads as unknown",
    )
    parser.add_argument("--once", action="store_true", help="print one collection and exit")
    arguments = parser.parse_args(argv)

    collector = build_collector(arguments)
    if arguments.once:
        sys.stdout.write(collector.collect())
        return 0

    handler = type("BoundMetricsHandler", (MetricsHandler,), {"collector": collector})
    server = ThreadingHTTPServer((arguments.listen_address, arguments.listen_port), handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
