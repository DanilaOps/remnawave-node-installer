"""The rules the capacity inventory has to keep.

These are not schema tests for their own sake. Each one is a way the capacity
figure could be wrong in a direction that costs money or hides an outage:

  * a node that is off still counted as capacity     -> the fleet looks bigger
  * a bridge added to the pool it belongs to         -> the fleet looks bigger
  * a range turned into a single number              -> an invented figure
  * a bridge measured by a shared tag                -> user traffic counted as
                                                        bridge traffic
  * a UUID or an address committed to a public file  -> a leak
"""

from __future__ import annotations

import hashlib
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import capacity_model as model  # noqa: E402
import capacity_exporter as exporter  # noqa: E402

import yaml  # noqa: E402

CAPACITY_FILE = ROOT / "capacity" / "capacity.yml"

# The nineteen nodes the live Remnawave panel holds. Written out rather than
# counted, so that adding a node to the file without adding it to the fleet -
# or the reverse - fails here instead of quietly changing a capacity total.
PRODUCTION_NODES = {
    "DE-01", "DE-02", "DE-03",
    "EE-01", "SW-01", "NL-01", "PL-01", "CZ-01", "LV-01", "FL-01", "US-01",
    "FR-01", "TR-01",
    "RU-01", "RU-02", "RU-03", "RU-04", "RU-05", "RU-06",
}

# The owner's capacity table totals 24.1-26.1 Gbit/s. 24.1 is the lower bound,
# which is what this inventory publishes: FR and TR are ranges and are published
# conservatively, and the links that the table rates only as a bridge are not
# also counted as standalone exits.
EXPECTED_FLEET_MBPS = 24100.0


def load() -> dict:
    return yaml.safe_load(CAPACITY_FILE.read_text(encoding="utf-8"))


def everything_up(inventory: model.CapacityInventory) -> dict[str, dict]:
    """Panel state in which every node in the inventory is connected."""
    return {name: {"is_connected": True, "is_disabled": False} for name in inventory.nodes}


def active_when_everything_is_up(inventory: model.CapacityInventory) -> set[str]:
    return inventory.active_nodes(everything_up(inventory))


class ShippedInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = load()
        self.inventory = model.validate(self.document)

    def test_the_committed_inventory_has_no_blockers(self) -> None:
        self.assertEqual(
            [], self.inventory.blockers, msg=model.format_problems(self.inventory.blockers)
        )

    def test_the_fleet_total_matches_the_owner_capacity_table(self) -> None:
        active = active_when_everything_is_up(self.inventory)
        for direction in model.DIRECTIONS:
            self.assertAlmostEqual(
                EXPECTED_FLEET_MBPS,
                self.inventory.fleet_active_capacity(direction, active),
                msg=f"{direction} total drifted from the owner's table",
            )

    def test_every_declared_bridge_is_present_with_its_real_tag(self) -> None:
        expected = {
            "RU-01": "RU01-40913C-DE-BRIDGE",
            "RU-02": "RU02-F74902-FL-BRIDGE",
            "RU-03": "RU03-B53002-LV-BRIDGE",
            "RU-04": "RU04-TO-US01-BRIDGE",
        }
        actual = {bridge.source_node: bridge.measurement_tag for bridge in self.inventory.bridges.values()}
        self.assertEqual(expected, actual)

    def test_every_bridge_is_measured_outbound_on_its_source_node(self) -> None:
        for name, bridge in self.inventory.bridges.items():
            with self.subTest(bridge=name):
                self.assertEqual("outbound", bridge.measurement_kind)
                self.assertEqual(bridge.source_node, bridge.measurement_node)
                self.assertTrue(bridge.measurement_tag)

    def test_the_inventory_holds_exactly_the_production_fleet(self) -> None:
        # The live Remnawave panel holds nineteen nodes. A node missing here is
        # a node with no capacity row, no pool and no place in a total; a node
        # here that the panel does not have is a row nothing will ever join to.
        # Both look fine in a diff, so the set is pinned.
        self.assertEqual(PRODUCTION_NODES, set(self.inventory.nodes))
        self.assertEqual(19, len(self.inventory.nodes))

    def test_every_node_belongs_to_a_declared_pool(self) -> None:
        placed = {member for pool in self.inventory.pools.values() for member in pool.members}
        self.assertEqual(PRODUCTION_NODES, placed, "a node that is in no pool is in no pool total")

    def test_pool_membership_is_the_one_that_was_agreed(self) -> None:
        expected = {
            "DE": ("DE-01", "DE-02", "DE-03"),
            "EE": ("EE-01",),
            "SE": ("SW-01",),
            "NL": ("NL-01",),
            "PL": ("PL-01",),
            "CZ": ("CZ-01",),
            "LV": ("LV-01",),
            "FL": ("FL-01",),
            "US": ("US-01",),
            "FR": ("FR-01",),
            "TR": ("TR-01",),
            "RU_ENTRY": ("RU-01", "RU-02", "RU-03", "RU-04"),
            "RU_AUTO": ("RU-05", "RU-06"),
        }
        self.assertEqual(expected, {name: pool.members for name, pool in self.inventory.pools.items()})

    def test_node_names_are_the_names_the_panel_uses(self) -> None:
        # The Swedish node is SW-01 in Remnawave. A capacity file that calls it
        # SE-01 joins against nothing: its metrics never match a capacity row,
        # it never counts towards active capacity, and it collects false health
        # and capacity signals while looking correct in a diff.
        self.assertIn("SW-01", self.inventory.nodes)
        self.assertNotIn("SE-01", self.inventory.nodes)
        self.assertNotIn("SE-01", CAPACITY_FILE.read_text(encoding="utf-8"))
        self.assertEqual(("SW-01",), self.inventory.pools["SE"].members)
        self.assertEqual("SW-01", self.inventory.nodes["SW-01"].measurement_node)

    def test_the_file_states_no_potential_capacity(self) -> None:
        # The expansion scenario in network-capacity.md is a planning document.
        # A number that does not exist yet must never be what an alert fires on.
        text = CAPACITY_FILE.read_text(encoding="utf-8").lower()
        for forbidden in ("potential_capacity", "planned_capacity", "after_expansion"):
            self.assertNotIn(forbidden, text)

    def test_the_file_carries_no_secret(self) -> None:
        self.assertEqual([], model.find_secrets(self.document))

    def test_ranges_are_published_conservatively_and_marked(self) -> None:
        for pool_name in ("FR", "TR"):
            with self.subTest(pool=pool_name):
                capacity = self.inventory.pools[pool_name].capacity["download"]
                self.assertEqual("range", capacity.source)
                self.assertTrue(capacity.conservative)
                self.assertEqual(capacity.bound_min, capacity.mbps)
                self.assertLess(capacity.mbps, capacity.bound_max)


class ExclusionTests(unittest.TestCase):
    """A node that is not carrying traffic is not capacity."""

    def setUp(self) -> None:
        self.inventory = model.validate(load())
        self.baseline = self.inventory.fleet_active_capacity(
            "download", active_when_everything_is_up(self.inventory)
        )

    def _without(self, node: str, **state: bool) -> float:
        runtime = everything_up(self.inventory)
        runtime[node].update(state)
        return self.inventory.fleet_active_capacity("download", self.inventory.active_nodes(runtime))

    def test_a_disconnected_node_leaves_the_total(self) -> None:
        self.assertEqual(self.baseline - 5500, self._without("PL-01", is_connected=False))

    def test_a_panel_disabled_node_leaves_the_total(self) -> None:
        self.assertEqual(self.baseline - 5500, self._without("PL-01", is_disabled=True))

    def test_a_node_the_panel_has_never_heard_of_is_not_counted(self) -> None:
        runtime = everything_up(self.inventory)
        del runtime["PL-01"]
        self.assertEqual(
            self.baseline - 5500,
            self.inventory.fleet_active_capacity("download", self.inventory.active_nodes(runtime)),
        )

    def test_a_node_disabled_in_the_inventory_leaves_the_total(self) -> None:
        document = load()
        document["nodes"]["PL-01"]["enabled"] = False
        inventory = model.validate(document)
        self.assertEqual([], inventory.blockers)
        self.assertEqual(
            self.baseline - 5500,
            inventory.fleet_active_capacity("download", active_when_everything_is_up(inventory)),
        )

    def test_capacity_returns_when_the_node_does(self) -> None:
        self.assertEqual(self.baseline, self._without("PL-01", is_connected=True))


class DoubleCountingTests(unittest.TestCase):
    """The bytes on a bridge are already inside the nodes at both of its ends."""

    def setUp(self) -> None:
        self.inventory = model.validate(load())
        self.active = active_when_everything_is_up(self.inventory)

    def test_bridge_capacity_is_not_in_the_fleet_total(self) -> None:
        bridged = sum(
            bridge.capacity["download"].mbps
            for bridge in self.inventory.bridges.values()
            if bridge.capacity["download"].published
        )
        self.assertGreater(bridged, 0, "the fixture must have rated bridges for this test to mean anything")
        total = self.inventory.fleet_active_capacity("download", self.active)
        pools = sum(
            self.inventory.pool_active_capacity(name, "download", self.active) or 0.0
            for name in self.inventory.pools
        )
        self.assertEqual(pools, total)
        self.assertNotEqual(pools + bridged, total)

    def test_a_pool_may_not_state_capacity_beside_a_rated_member(self) -> None:
        # Rating FR-01 while the pool keeps the location's figure puts the same
        # gigabits in both rows, and the pool total would carry them twice.
        document = load()
        document["nodes"]["FR-01"]["capacity"] = {
            "download": {"mbps": 5000, "source": "declared"},
            "upload": {"mbps": 5000, "source": "declared"},
        }
        problems = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("pools.capacity_and_members", problems)

    def test_a_node_belongs_to_exactly_one_pool(self) -> None:
        document = load()
        document["pools"]["DE"]["members"].append("CZ-01")
        problems = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("pools.member_shared", problems)

    def test_two_bridges_may_not_share_an_outbound_tag(self) -> None:
        document = load()
        document["bridges"]["RU02-FL"]["outbound_tag"] = "RU01-40913C-DE-BRIDGE"
        document["bridges"]["RU02-FL"]["measurement"]["tag"] = "RU01-40913C-DE-BRIDGE"
        problems = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("bridges.tag_shared", problems)

    def test_the_exporter_keeps_bridges_out_of_the_service_figure(self) -> None:
        collector = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(), None)
        text = collector.collect()
        service = [line for line in text.splitlines() if line.startswith("august_service_capacity_mbps")]
        self.assertTrue(service)
        for line in service:
            self.assertNotIn('scope="bridge"', line)
        # And the bridge rows are still published, on their own, and inside the
        # diagnostic physical figure.
        self.assertIn('scope="bridge"', text)
        physical = {
            line.split("} ")[1]
            for line in text.splitlines()
            if line.startswith("august_physical_capacity_mbps")
        }
        served = {
            line.split("} ")[1]
            for line in text.splitlines()
            if line.startswith("august_service_capacity_mbps")
        }
        self.assertNotEqual(physical, served, "physical and service must not be the same number")


class MeasurementTests(unittest.TestCase):
    def test_an_outbound_measurement_needs_a_tag(self) -> None:
        document = load()
        document["bridges"]["RU03-LV"]["measurement"].pop("tag")
        document["bridges"]["RU03-LV"].pop("outbound_tag")
        problems = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("measurement.tag_required", problems)

    def test_an_inbound_measurement_may_not_carry_a_tag(self) -> None:
        document = load()
        document["nodes"]["DE-01"]["measurement"]["tag"] = "SOMETHING"
        problems = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("measurement.tag_forbidden", problems)

    def test_a_bridge_measured_inbound_is_refused(self) -> None:
        document = load()
        document["bridges"]["RU03-LV"]["measurement"]["kind"] = "inbound"
        problems = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("measurement.bridge_outbound", problems)

    def test_a_measurement_node_that_is_not_the_source_is_refused(self) -> None:
        document = load()
        document["bridges"]["RU03-LV"]["measurement"]["node"] = "RU-01"
        problems = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("bridges.source_mismatch", problems)

    def test_a_node_measurement_naming_another_node_is_refused(self) -> None:
        document = load()
        document["nodes"]["DE-01"]["measurement"]["node"] = "CZ-01"
        problems = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("measurement.node_mismatch", problems)

    def test_an_unknown_measurement_kind_is_refused(self) -> None:
        document = load()
        document["nodes"]["DE-01"]["measurement"]["kind"] = "sideways"
        problems = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("measurement.kind", problems)


class RangeTests(unittest.TestCase):
    """FR 5-6 and TR 3-4 must never become an invented exact number."""

    def test_a_range_without_a_policy_is_a_blocker(self) -> None:
        document = load()
        document["pools"]["FR"]["capacity"]["download"].pop("policy")
        problems = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("capacity.range_policy", problems)

    def test_a_range_with_policy_blocker_stops_the_run(self) -> None:
        document = load()
        document["pools"]["TR"]["capacity"]["download"]["policy"] = "blocker"
        inventory = model.validate(document)
        self.assertIn("capacity.range_blocker", {problem.code for problem in inventory.blockers})
        self.assertIsNone(inventory.pools["TR"].capacity["download"].mbps)

    def test_a_range_with_an_exact_number_beside_it_is_refused(self) -> None:
        document = load()
        document["pools"]["FR"]["capacity"]["download"]["mbps"] = 5500
        problems = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("capacity.invented_point", problems)

    def test_conservative_publishes_the_lower_bound(self) -> None:
        inventory = model.validate(load())
        capacity = inventory.pools["TR"].capacity["upload"]
        self.assertEqual(3000, capacity.mbps)
        self.assertEqual(4000, capacity.bound_max)

    def test_the_exporter_marks_a_conservative_value(self) -> None:
        collector = exporter.CapacityCollector(CAPACITY_FILE, None, None)
        conservative = [
            line
            for line in collector.collect().splitlines()
            if line.startswith("august_capacity_mbps") and 'name="FR"' in line
        ]
        self.assertTrue(conservative)
        for line in conservative:
            self.assertIn('conservative="true"', line)

    def test_an_inverted_range_is_refused(self) -> None:
        document = load()
        document["pools"]["FR"]["capacity"]["download"]["mbps_min"] = 9000
        problems = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("capacity.range", problems)


class PoolCapacityTests(unittest.TestCase):
    def test_a_pool_with_capacity_and_no_members_must_say_why(self) -> None:
        document = load()
        document["pools"]["FR"]["members"] = []
        document["pools"]["FR"].pop("members_unknown_reason", None)
        problems = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("pools.members_unknown", problems)

    def test_a_self_rated_pool_may_name_members_that_are_not_rated(self) -> None:
        # The normal case for FR and TR: the machine is known, the owner's table
        # rates the location, and nothing measures the machine on its own. There
        # is no double count to prevent, because the member publishes nothing.
        inventory = model.validate(load())
        self.assertEqual([], inventory.blockers, msg=model.format_problems(inventory.blockers))
        self.assertEqual(("FR-01",), inventory.pools["FR"].members)
        self.assertFalse(any(c.published for c in inventory.nodes["FR-01"].capacity.values()))

    def test_a_self_rated_pool_loses_its_capacity_when_every_member_is_down(self) -> None:
        # Without this, a location whose only machine is offline would keep
        # advertising 5 Gbit/s of headroom that nothing can carry.
        inventory = model.validate(load())
        active = active_when_everything_is_up(inventory)
        self.assertEqual(5000, inventory.pool_active_capacity("FR", "download", active))
        self.assertEqual(0.0, inventory.pool_active_capacity("FR", "download", active - {"FR-01"}))

    def test_a_self_rated_pool_publishes_its_own_capacity(self) -> None:
        inventory = model.validate(load())
        active = active_when_everything_is_up(inventory)
        self.assertEqual(5000, inventory.pool_active_capacity("FR", "download", active))

    def test_a_member_rated_pool_sums_only_active_members(self) -> None:
        inventory = model.validate(load())
        active = active_when_everything_is_up(inventory)
        self.assertEqual(4000, inventory.pool_active_capacity("RU_ENTRY", "download", active))
        self.assertEqual(3000, inventory.pool_active_capacity("RU_ENTRY", "download", active - {"RU-01"}))


class ApiMisuseTests(unittest.TestCase):
    """The one call that would silently report the whole fleet as available."""

    def test_passing_the_state_mapping_instead_of_the_active_set_raises(self) -> None:
        inventory = model.validate(load())
        state = everything_up(inventory)
        state["PL-01"]["is_connected"] = False
        with self.assertRaises(TypeError):
            inventory.fleet_active_capacity("download", state)
        with self.assertRaises(TypeError):
            inventory.pool_active_capacity("PL", "download", state)


class SecretTests(unittest.TestCase):
    def test_a_uuid_is_refused(self) -> None:
        document = load()
        document["nodes"]["DE-01"]["panel_reference"] = "7fad6bb7-3bac-44b8-90c4-fd0872f08938"
        codes = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("secret.uuid", codes)

    def test_a_credential_shaped_key_is_refused(self) -> None:
        document = load()
        document["nodes"]["DE-01"]["api_token"] = "anything"
        codes = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("secret.key_name", codes)

    def test_a_public_address_is_refused(self) -> None:
        document = load()
        document["nodes"]["DE-01"]["note"] = "reachable at 94.249.240.114"
        codes = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("secret.public_ip", codes)

    def test_a_documentation_address_is_allowed(self) -> None:
        document = load()
        document["nodes"]["DE-01"]["note"] = "example address 203.0.113.10"
        codes = {problem.code for problem in model.validate(document).blockers}
        self.assertNotIn("secret.public_ip", codes)

    def test_potential_capacity_is_refused_as_a_top_level_key(self) -> None:
        document = load()
        document["potential_capacity"] = {"RU-01": 7000}
        codes = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("document.potential_capacity", codes)


class StubPanel:
    """The panel's view of the fleet, without a panel.

    Shaped like the real answer: a UUID per node, because that is what
    Remnawave's traffic series are labelled with.
    """

    def __init__(
        self,
        offline: set[str] | None = None,
        disabled: set[str] | None = None,
        extra: set[str] | None = None,
        missing: set[str] | None = None,
        no_uuid: set[str] | None = None,
        shared_uuid: set[str] | None = None,
    ) -> None:
        self.offline = offline or set()
        self.disabled = disabled or set()
        self.extra = extra or set()
        self.missing = missing or set()
        self.no_uuid = no_uuid or set()
        self.shared_uuid = shared_uuid or set()

    @staticmethod
    def _uuid(name: str) -> str:
        digest = hashlib.md5(name.encode()).hexdigest()
        return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"

    def nodes(self) -> list[dict]:
        names = (set(PRODUCTION_NODES) | self.extra) - self.missing
        answer = []
        for name in sorted(names):
            if name in self.no_uuid:
                node_uuid = ""
            elif name in self.shared_uuid:
                node_uuid = self._uuid("shared")
            else:
                node_uuid = self._uuid(name)
            answer.append(
                {
                    "name": name,
                    "uuid": node_uuid,
                    "isConnected": name not in self.offline,
                    "isDisabled": name in self.disabled,
                    "createdAt": "2026-06-01T00:00:00Z",
                    "trafficUsedBytes": 0,
                    "trafficLimitBytes": 0,
                }
            )
        return answer


class UnratedNodeVisibilityTests(unittest.TestCase):
    """A node with no rating is still a node.

    Nine of the nineteen carry no capacity figure. The rule they test is the
    one that decides whether the dashboard is usable: an absent rating must
    remove the node from the capacity arithmetic and from nothing else. If it
    also removed the node from the operational series, a machine nobody has
    measured would be a machine nobody can see go down.
    """

    # No rating at all. DE-01..03 are a separate case: their capacity is
    # accounted at pool DE, which is a statement about where the number lives
    # rather than an absence of one.
    UNRATED = ("FR-01", "TR-01", "RU-05", "RU-06", "LV-01", "FL-01", "US-01")
    SHARED = ("DE-01", "DE-02", "DE-03")

    def setUp(self) -> None:
        self.text = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(), None).collect()

    def _samples(self, metric: str, node: str) -> list[str]:
        return [
            line
            for line in self.text.splitlines()
            if line.startswith(metric + "{") and f'node="{node}"' in line
        ]

    def test_every_node_has_operational_metadata_whether_rated_or_not(self) -> None:
        for node in sorted(PRODUCTION_NODES):
            for metric in (
                "august_node_enabled",
                "august_node_known_to_panel",
                "august_node_connected",
                "august_node_administratively_disabled",
                "august_node_active",
                "august_node_session_limit",
            ):
                with self.subTest(node=node, metric=metric):
                    self.assertEqual(1, len(self._samples(metric, node)))

    def test_an_unrated_node_is_active_and_says_so(self) -> None:
        for node in self.UNRATED:
            with self.subTest(node=node):
                self.assertTrue(self._samples("august_node_active", node)[0].endswith(" 1"))

    def test_a_shared_pool_node_says_where_its_capacity_lives(self) -> None:
        # Not "unrated": the number exists, one level up. The distinction is what
        # keeps the dashboard from showing DE as unmeasured when it is measured
        # as a location.
        for node in self.SHARED:
            with self.subTest(node=node):
                self.assertEqual([], self._samples("august_capacity_mbps", node))
                shared = [
                    line
                    for line in self.text.splitlines()
                    if line.startswith("august_capacity_shared_pool") and f'name="{node}"' in line
                ]
                self.assertEqual(2, len(shared), "both directions must say so")
                for line in shared:
                    self.assertIn('pool="DE"', line)

    def test_an_unrated_node_publishes_no_capacity_but_declares_the_gap(self) -> None:
        for node in self.UNRATED:
            with self.subTest(node=node):
                self.assertEqual([], self._samples("august_capacity_mbps", node))
                unrated = self._samples("august_capacity_unrated", node)
                self.assertEqual(
                    {"download", "upload"},
                    {line.split('direction="')[1].split('"')[0] for line in unrated},
                )

    def test_an_unrated_node_still_counts_towards_its_pool_membership(self) -> None:
        # PoolSingleNodeRemaining and the pool tables are driven by these two,
        # so a location with three machines must read as three whether or not
        # anybody has measured them.
        self.assertIn('august_pool_members{pool="DE"} 3', self.text)
        self.assertIn('august_pool_active_members{pool="DE"} 3', self.text)
        self.assertIn('august_pool_members{pool="RU_AUTO"} 2', self.text)
        self.assertIn('august_pool_active_members{pool="RU_AUTO"} 2', self.text)

    def test_an_unrated_pool_publishes_membership_without_capacity(self) -> None:
        # RU_AUTO has no rating anywhere. It must still appear as a pool.
        self.assertIn('august_pool_rates_itself{pool="RU_AUTO"} 0', self.text)
        self.assertEqual(
            [],
            [
                line
                for line in self.text.splitlines()
                if line.startswith("august_pool_active_capacity_mbps") and 'pool="RU_AUTO"' in line
            ],
        )

    def test_an_unrated_node_going_offline_is_visible_and_costs_no_capacity(self) -> None:
        # The two halves of the rule in one assertion: health notices, capacity
        # does not move, because it never counted the node in the first place.
        before = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(), None).collect()
        after = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(offline={"DE-02"}), None).collect()
        self.assertIn('august_node_active{node="DE-02",pool="DE"} 1', before)
        self.assertIn('august_node_active{node="DE-02",pool="DE"} 0', after)
        self.assertIn('august_node_connected{node="DE-02",pool="DE"} 0', after)
        self.assertIn('august_pool_active_members{pool="DE"} 2', after)
        fleet = [line for line in after.splitlines() if line.startswith("august_service_capacity_mbps")]
        self.assertEqual(
            [line for line in before.splitlines() if line.startswith("august_service_capacity_mbps")],
            fleet,
        )

    def test_a_rated_node_going_offline_does_move_the_total(self) -> None:
        # The control for the test above: the mechanism works, so the unchanged
        # total there means "contributed nothing", not "nothing was measured".
        before = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(), None).collect()
        after = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(offline={"PL-01"}), None).collect()
        self.assertIn('august_service_capacity_mbps{direction="download"} 24100', before)
        self.assertIn('august_service_capacity_mbps{direction="download"} 18600', after)

    def test_a_disabled_node_is_still_published_as_a_node(self) -> None:
        text = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(disabled={"RU-05"}), None).collect()
        self.assertIn('august_node_administratively_disabled{node="RU-05",pool="RU_AUTO"} 1', text)
        self.assertIn('august_node_active{node="RU-05",pool="RU_AUTO"} 0', text)
        self.assertIn('august_node_known_to_panel{node="RU-05",pool="RU_AUTO"} 1', text)


class ExporterOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = exporter.CapacityCollector(CAPACITY_FILE, None, None).collect()

    def test_every_family_is_grouped_behind_one_help_and_type(self) -> None:
        seen: list[str] = []
        declared: set[str] = set()
        for line in self.text.splitlines():
            if line.startswith("# HELP "):
                name = line.split()[2]
                self.assertNotIn(name, declared, f"{name} is declared twice")
                declared.add(name)
                seen.append(name)
            elif line and not line.startswith("#"):
                name = line.split("{")[0].split(" ")[0]
                self.assertEqual(seen[-1], name, f"{name} appears outside its own family block")

    def test_the_inventory_is_reported_valid(self) -> None:
        self.assertIn("august_capacity_inventory_valid 1", self.text)

    def test_a_broken_file_does_not_take_the_exporter_down(self) -> None:
        collector = exporter.CapacityCollector(pathlib.Path("/nonexistent/capacity.yml"), None, None)
        text = collector.collect()
        self.assertIn("august_capacity_inventory_valid 0", text)

    def test_the_last_good_inventory_keeps_serving(self) -> None:
        collector = exporter.CapacityCollector(CAPACITY_FILE, None, None)
        collector.collect()
        collector.capacity_path = pathlib.Path("/nonexistent/capacity.yml")
        text = collector.collect()
        self.assertIn("august_capacity_inventory_valid 0", text)
        self.assertIn("august_capacity_inventory_last_good_in_use 1", text)
        self.assertIn("august_service_capacity_mbps", text)

    def test_bridge_info_carries_the_join_labels(self) -> None:
        rows = [line for line in self.text.splitlines() if line.startswith("august_bridge_info")]
        self.assertEqual(4, len(rows))
        for row in rows:
            self.assertIn('node="RU-0', row)
            self.assertIn("tag=", row)

    def test_label_values_are_escaped(self) -> None:
        self.assertEqual('a\\"b', exporter.escape_label('a"b'))
        self.assertEqual("a\\\\b", exporter.escape_label("a\\b"))


class SampleValuePrecisionTests(unittest.TestCase):
    """A published number has to survive being read back.

    The regression: values were rendered with f"{value:g}", which rounds to six
    significant digits. A Unix timestamp of 1788293189 went out as 1.78829e+09
    and parsed back as 1788290000, so every "how old is this?" rule measured an
    age that was up to ten thousand seconds wrong. AugustCapacityExporterStale
    fired against an exporter that was answering every scrape.
    """

    def test_a_unix_timestamp_survives_a_round_trip(self) -> None:
        stamp = 1788293189.0
        rendered = exporter.format_value(stamp)
        self.assertNotIn("e+", rendered, "a timestamp must not be rendered in exponent form")
        self.assertAlmostEqual(stamp, float(rendered), delta=1e-6)

    def test_the_old_six_digit_format_would_have_failed_this(self) -> None:
        # Guards the guard: proves the assertion above is actually sensitive to
        # the defect, so it cannot quietly stop testing anything.
        stamp = 1788293189.0
        self.assertGreater(abs(stamp - float(f"{stamp:g}")), 1000)

    def test_a_byte_counter_survives_a_round_trip(self) -> None:
        used = 5219238471
        self.assertEqual(used, int(float(exporter.format_value(used))))

    def test_integers_carry_no_decimal_noise(self) -> None:
        for value, expected in ((1, "1"), (0, "0"), (True, "1"), (False, "0"), (1000.0, "1000"), (-3.0, "-3")):
            self.assertEqual(expected, exporter.format_value(value))

    def test_fractions_keep_their_value_without_gaining_digits(self) -> None:
        self.assertEqual("0.5", exporter.format_value(0.5))
        self.assertEqual(0.255049, float(exporter.format_value(0.255049)))

    def test_not_a_number_and_the_infinities_stay_parseable(self) -> None:
        self.assertEqual("NaN", exporter.format_value(float("nan")))
        self.assertEqual("+Inf", exporter.format_value(float("inf")))
        self.assertEqual("-Inf", exporter.format_value(float("-inf")))
        # Something that is not a number at all must not produce a line that
        # Prometheus refuses to parse for the rest of the scrape.
        self.assertEqual("NaN", exporter.format_value("not a number"))
        self.assertEqual("NaN", exporter.format_value(None))

    def test_every_published_sample_parses_as_a_prometheus_value(self) -> None:
        collector = exporter.CapacityCollector(CAPACITY_FILE, None, None)
        for line in collector.collect().splitlines():
            if not line or line.startswith("#"):
                continue
            value = line.rsplit(" ", 1)[1]
            if value in ("NaN", "+Inf", "-Inf"):
                continue
            float(value)  # raises, and fails the test, on anything unparseable

    def test_ordinary_metrics_are_unchanged_by_the_new_formatter(self) -> None:
        rendered = exporter.render_sample("august_node_enabled", {"node": "PL-01", "pool": "PL"}, 1)
        self.assertEqual('august_node_enabled{node="PL-01",pool="PL"} 1', rendered)


class NodeStateMatrixTests(unittest.TestCase):
    """Six states, and every combination of the three inputs that produce them.

    The distinction that matters operationally: a node somebody switched off must
    not page anyone, and a node that fell over must. Both leave active capacity,
    and for the dashboard they are not the same thing at all.
    """

    def setUp(self) -> None:
        self.inventory = model.validate(load())

    def state(self, *, inventory_enabled: bool, known: bool, panel_disabled: bool, connected: bool) -> dict:
        document = load()
        document["nodes"]["PL-01"]["enabled"] = inventory_enabled
        inventory = model.validate(document)
        runtime = {}
        if known:
            runtime["PL-01"] = {"is_connected": connected, "is_disabled": panel_disabled}
        return inventory.node_state("PL-01", runtime)

    def test_the_full_matrix(self) -> None:
        # inventory_enabled, known, panel_disabled, connected -> expectations
        matrix = [
            # the ordinary healthy node
            (True, True, False, True, {"enabled": True, "connected": True, "disabled": False, "active": True}),
            # enabled and fell over: health alerts, capacity leaves
            (True, True, False, False, {"enabled": True, "connected": False, "disabled": False, "active": False}),
            # disabled in the panel: deliberate, no offline alert, capacity leaves
            (True, True, True, False, {"enabled": False, "connected": False, "disabled": True, "active": False}),
            # disabled in the panel while still connected: still deliberate
            (True, True, True, True, {"enabled": False, "connected": True, "disabled": True, "active": False}),
            # taken out in the capacity inventory
            (False, True, False, True, {"enabled": False, "connected": True, "disabled": True, "active": False}),
            (False, True, False, False, {"enabled": False, "connected": False, "disabled": True, "active": False}),
            # both say disabled
            (False, True, True, False, {"enabled": False, "connected": False, "disabled": True, "active": False}),
            # the panel has never heard of it: not active, and not "disabled"
            (True, False, False, False, {"enabled": True, "connected": False, "disabled": False, "active": False, "known": False}),
            (False, False, False, False, {"enabled": False, "connected": False, "disabled": True, "active": False, "known": False}),
        ]
        for inventory_enabled, known, panel_disabled, connected, expected in matrix:
            with self.subTest(
                inventory_enabled=inventory_enabled, known=known, panel_disabled=panel_disabled, connected=connected
            ):
                state = self.state(
                    inventory_enabled=inventory_enabled,
                    known=known,
                    panel_disabled=panel_disabled,
                    connected=connected,
                )
                self.assertTrue(state["configured"], "the capacity inventory declares this node")
                for key, value in expected.items():
                    self.assertEqual(value, state[key], f"{key} for this combination")

    def test_disabled_is_never_also_enabled(self) -> None:
        for inventory_enabled in (True, False):
            for panel_disabled in (True, False):
                state = self.state(
                    inventory_enabled=inventory_enabled, known=True, panel_disabled=panel_disabled, connected=True
                )
                self.assertFalse(state["enabled"] and state["disabled"])

    def test_active_requires_all_three(self) -> None:
        # Enabled here, enabled there, and connected. Anything less is not
        # capacity somebody can plan against.
        self.assertTrue(
            self.state(inventory_enabled=True, known=True, panel_disabled=False, connected=True)["active"]
        )
        for broken in (
            dict(inventory_enabled=False, known=True, panel_disabled=False, connected=True),
            dict(inventory_enabled=True, known=True, panel_disabled=True, connected=True),
            dict(inventory_enabled=True, known=True, panel_disabled=False, connected=False),
            dict(inventory_enabled=True, known=False, panel_disabled=False, connected=False),
        ):
            with self.subTest(**broken):
                self.assertFalse(self.state(**broken)["active"])

    def test_a_disabled_node_leaves_capacity_without_being_offline(self) -> None:
        runtime = everything_up(self.inventory)
        runtime["PL-01"] = {"is_connected": False, "is_disabled": True}
        active = self.inventory.active_nodes(runtime)
        self.assertNotIn("PL-01", active)
        state = self.inventory.node_state("PL-01", runtime)
        # This is the pair a dashboard needs: out of capacity, not an incident.
        self.assertTrue(state["disabled"])
        self.assertFalse(state["enabled"])

    def test_an_enabled_disconnected_node_is_an_incident(self) -> None:
        runtime = everything_up(self.inventory)
        runtime["PL-01"] = {"is_connected": False, "is_disabled": False}
        state = self.inventory.node_state("PL-01", runtime)
        self.assertTrue(state["enabled"])
        self.assertFalse(state["connected"])
        self.assertFalse(state["disabled"])
        self.assertFalse(state["active"])


class ServiceAccountingTests(unittest.TestCase):
    """Service capacity is not the sum of the infrastructure."""

    def setUp(self) -> None:
        self.inventory = model.validate(load())
        self.active = active_when_everything_is_up(self.inventory)

    def test_service_and_physical_are_different_numbers(self) -> None:
        service = self.inventory.service_capacity("download", self.active)
        physical = self.inventory.physical_capacity("download", self.active)
        self.assertLess(service, physical)
        # The difference is exactly the rated bridges, which is what makes the
        # physical figure a diagnostic and not a KPI.
        bridged = sum(
            bridge.capacity["download"].mbps
            for bridge in self.inventory.bridges.values()
            if bridge.capacity["download"].published
        )
        self.assertAlmostEqual(service + bridged, physical)

    def test_service_capacity_matches_the_owner_table(self) -> None:
        self.assertAlmostEqual(EXPECTED_FLEET_MBPS, self.inventory.service_capacity("download", self.active))

    def test_a_bridge_only_exit_is_not_service_capacity(self) -> None:
        # FL is reachable only through the RU-02 bridge, so its throughput is
        # bridged traffic and not capacity of its own.
        self.assertFalse(self.inventory.pools["FL"].serves_users)

    def test_no_pool_capacity_is_counted_twice(self) -> None:
        summed = sum(
            self.inventory.pool_capacity(name, "download", self.active)[0] or 0.0
            for name, pool in self.inventory.pools.items()
            if pool.serves_users
        )
        self.assertAlmostEqual(summed, self.inventory.service_capacity("download", self.active))


class SharedPoolTests(unittest.TestCase):
    """An aggregate rating, and what must not be invented from it."""

    def setUp(self) -> None:
        self.inventory = model.validate(load())
        self.active = active_when_everything_is_up(self.inventory)

    def test_no_de_node_carries_the_aggregate_figure(self) -> None:
        for node in ("DE-01", "DE-02", "DE-03"):
            with self.subTest(node=node):
                for direction in model.DIRECTIONS:
                    capacity = self.inventory.nodes[node].capacity[direction]
                    self.assertEqual("shared_pool", capacity.source)
                    self.assertIsNone(capacity.mbps)

    def test_the_pool_carries_it_and_says_it_is_aggregate(self) -> None:
        capacity = self.inventory.pools["DE"].capacity["download"]
        self.assertEqual(2600, capacity.mbps)
        self.assertTrue(capacity.aggregate)

    def test_losing_one_member_does_not_subtract_a_guess(self) -> None:
        # The whole point. 2600/3 is a number nothing measured supports.
        value, certain = self.inventory.pool_capacity("DE", "download", self.active - {"DE-02"})
        self.assertEqual(2600, value)
        self.assertFalse(certain)

    def test_losing_every_member_does_subtract_everything(self) -> None:
        value, certain = self.inventory.pool_capacity(
            "DE", "download", self.active - {"DE-01", "DE-02", "DE-03"}
        )
        self.assertEqual(0.0, value)
        self.assertTrue(certain)

    def test_uncertainty_propagates_to_the_service_figure(self) -> None:
        self.assertTrue(self.inventory.service_capacity_certain("download", self.active))
        self.assertFalse(self.inventory.service_capacity_certain("download", self.active - {"DE-03"}))

    def test_a_shared_pool_node_must_point_at_a_pool_that_rates_itself(self) -> None:
        document = load()
        del document["pools"]["DE"]["capacity"]
        codes = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("capacity.shared_pool_missing", codes)

    def test_a_shared_pool_node_may_not_point_at_a_per_node_figure(self) -> None:
        document = load()
        document["pools"]["DE"]["capacity"]["download"]["attribution"] = "per_node"
        codes = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("capacity.shared_pool_attribution", codes)

    def test_a_shared_pool_node_may_not_also_state_a_number(self) -> None:
        document = load()
        document["nodes"]["DE-01"]["capacity"]["download"]["mbps"] = 900
        codes = {problem.code for problem in model.validate(document).blockers}
        self.assertIn("capacity.shared_pool_number", codes)

    def test_the_exporter_publishes_the_uncertainty(self) -> None:
        text = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(offline={"DE-02"}), None).collect()
        self.assertIn('august_pool_capacity_aggregate{direction="download",pool="DE"} 1', text)
        self.assertIn('august_pool_capacity_certain{direction="download",pool="DE"} 0', text)
        self.assertIn('august_service_capacity_certain{direction="download"} 0', text)

    def test_a_per_node_pool_stays_certain(self) -> None:
        text = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(offline={"RU-01"}), None).collect()
        self.assertIn('august_pool_capacity_certain{direction="download",pool="RU_ENTRY"} 1', text)


class TopologyDriftTests(unittest.TestCase):
    """A new production node must not disappear quietly."""

    def setUp(self) -> None:
        self.inventory = model.validate(load())

    def test_a_panel_node_missing_here_is_drift(self) -> None:
        drift = self.inventory.topology_drift(panel_nodes=set(self.inventory.nodes) | {"TR-02"})
        self.assertEqual(["TR-02"], drift["missing_from_capacity"])

    def test_a_capacity_node_missing_from_the_panel_is_drift(self) -> None:
        drift = self.inventory.topology_drift(panel_nodes=set(self.inventory.nodes) - {"CZ-01"})
        self.assertEqual(["CZ-01"], drift["missing_from_panel"])

    def test_a_node_missing_from_the_ansible_inventory_is_drift(self) -> None:
        drift = self.inventory.topology_drift(ansible_nodes=set(self.inventory.nodes) - {"NL-01"})
        self.assertEqual(["NL-01"], drift["missing_from_ansible"])

    def test_an_ansible_node_missing_here_is_drift(self) -> None:
        drift = self.inventory.topology_drift(ansible_nodes=set(self.inventory.nodes) | {"XX-01"})
        self.assertEqual(["XX-01"], drift["missing_from_capacity_but_in_ansible"])

    def test_agreement_is_no_drift(self) -> None:
        names = set(self.inventory.nodes)
        drift = self.inventory.topology_drift(panel_nodes=names, ansible_nodes=names)
        self.assertEqual({key: [] for key in drift}, drift)

    def test_unknown_is_not_drift(self) -> None:
        # No panel answer is a different thing from an empty panel.
        drift = self.inventory.topology_drift(panel_nodes=None, ansible_nodes=None)
        self.assertEqual({key: [] for key in drift}, drift)

    def test_the_exporter_publishes_counts_and_names(self) -> None:
        text = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(extra={"TR-02"}), None).collect()
        self.assertIn('august_topology_drift{kind="missing_from_capacity"} 1', text)
        self.assertIn('august_topology_drift_node{kind="missing_from_capacity",node="TR-02"} 1', text)

    def test_no_drift_publishes_no_node_series(self) -> None:
        text = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(), None).collect()
        self.assertIn('august_topology_drift{kind="missing_from_capacity"} 0', text)
        self.assertNotIn("august_topology_drift_node{", text)


class IdentityTests(unittest.TestCase):
    """Remnawave labels its series by UUID. The mapping is published, not guessed."""

    def test_every_known_node_gets_a_mapping_series(self) -> None:
        text = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(), None).collect()
        mapping = [line for line in text.splitlines() if line.startswith("august_node_identity{")]
        self.assertEqual(19, len(mapping))
        for line in mapping:
            self.assertIn("node_uuid=", line)
            self.assertIn("pool=", line)

    def test_a_node_with_no_uuid_is_counted_and_not_mapped(self) -> None:
        text = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(no_uuid={"CZ-01"}), None).collect()
        self.assertIn('august_node_identity_problems{kind="missing_uuid"} 1', text)
        self.assertIn('august_node_identity_mapped{node="CZ-01"} 0', text)
        self.assertNotIn('august_node_identity{node="CZ-01"', text)

    def test_two_names_on_one_uuid_are_reported(self) -> None:
        text = exporter.CapacityCollector(
            CAPACITY_FILE, StubPanel(shared_uuid={"CZ-01", "NL-01"}), None
        ).collect()
        self.assertIn('august_node_identity_problems{kind="duplicate_uuid"} 1', text)

    def test_a_clean_fleet_reports_no_identity_problems(self) -> None:
        text = exporter.CapacityCollector(CAPACITY_FILE, StubPanel(), None).collect()
        for kind in ("duplicate_uuid", "duplicate_name", "missing_uuid"):
            self.assertIn(f'august_node_identity_problems{{kind="{kind}"}} 0', text)


class PanelFailSafeTests(unittest.TestCase):
    """One HTTP error must not take the fleet out of active capacity."""

    class FlakyPanel:
        def __init__(self, panel: "StubPanel") -> None:
            self.panel = panel
            self.fail = False

        def nodes(self) -> list[dict]:
            if self.fail:
                raise OSError("panel is having a moment")
            return self.panel.nodes()

    def test_a_transient_error_keeps_the_last_good_state(self) -> None:
        panel = self.FlakyPanel(StubPanel())
        collector = exporter.CapacityCollector(CAPACITY_FILE, panel, None, panel_staleness_seconds=600)
        first = collector.collect()
        self.assertIn('august_service_capacity_mbps{direction="download"} 24100', first)
        panel.fail = True
        second = collector.collect()
        self.assertIn("august_capacity_panel_reachable 0", second)
        self.assertIn("august_capacity_panel_state_stale 0", second)
        # The fleet is still there. This is the whole point.
        self.assertIn('august_service_capacity_mbps{direction="download"} 24100', second)

    def test_past_the_window_everything_reads_as_unknown(self) -> None:
        panel = self.FlakyPanel(StubPanel())
        collector = exporter.CapacityCollector(CAPACITY_FILE, panel, None, panel_staleness_seconds=0)
        collector.collect()
        panel.fail = True
        text = collector.collect()
        self.assertIn("august_capacity_panel_state_stale 1", text)
        # Not a false healthy: nothing is active and nothing is known.
        self.assertIn('august_service_capacity_mbps{direction="download"} 0', text)
        self.assertIn('august_node_known_to_panel{node="PL-01",pool="PL"} 0', text)
        self.assertIn('august_node_active{node="PL-01",pool="PL"} 0', text)

    def test_a_node_state_is_never_invented_when_there_was_none(self) -> None:
        panel = self.FlakyPanel(StubPanel())
        panel.fail = True
        collector = exporter.CapacityCollector(CAPACITY_FILE, panel, None, panel_staleness_seconds=600)
        text = collector.collect()
        self.assertIn("august_capacity_panel_state_stale 1", text)
        self.assertIn('august_service_capacity_mbps{direction="download"} 0', text)


class InventoryFailSafeTests(unittest.TestCase):
    """A semantically broken inventory is refused, not published."""

    def _write(self, tmpdir: pathlib.Path, text: str) -> pathlib.Path:
        path = tmpdir / "capacity.yml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_blocked_inventory_is_not_published(self) -> None:
        import tempfile

        good = CAPACITY_FILE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as raw:
            tmpdir = pathlib.Path(raw)
            path = self._write(tmpdir, good)
            collector = exporter.CapacityCollector(path, StubPanel(), None)
            first = collector.collect()
            self.assertIn("august_capacity_inventory_valid 1", first)

            # Syntactically fine, semantically refused: a bridge on a tag that
            # another bridge already owns.
            broken = good.replace("RU02-F74902-FL-BRIDGE", "RU01-40913C-DE-BRIDGE")
            self._write(tmpdir, broken)
            second = collector.collect()
            self.assertIn("august_capacity_inventory_valid 0", second)
            self.assertIn("august_capacity_inventory_last_good_in_use 1", second)
            # And the numbers are the last good ones, not the broken file's.
            self.assertIn('august_service_capacity_mbps{direction="download"} 24100', second)

    def test_a_duplicate_key_is_refused_by_the_loader(self) -> None:
        import tempfile

        good = CAPACITY_FILE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as raw:
            tmpdir = pathlib.Path(raw)
            path = self._write(tmpdir, good)
            collector = exporter.CapacityCollector(path, StubPanel(), None)
            collector.collect()
            self._write(tmpdir, good + "\nversion: 3\n")
            text = collector.collect()
            self.assertIn("august_capacity_inventory_valid 0", text)
            self.assertIn("august_capacity_inventory_last_good_in_use 1", text)

    def test_with_no_last_good_nothing_is_invented(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            path = self._write(
                pathlib.Path(raw), "version: 2\nnodes: {}\npools: {}\nbridges: {}\n"
            )
            collector = exporter.CapacityCollector(path, StubPanel(), None)
            text = collector.collect()
            self.assertIn("august_capacity_inventory_last_good_in_use 0", text)
            self.assertIn('august_service_capacity_mbps{direction="download"} 0', text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
