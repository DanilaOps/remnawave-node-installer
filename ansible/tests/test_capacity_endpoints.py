"""The auto-capacity test must ship a working default set of RU endpoints.

capacity_auto_test: true has to measure something without the operator first
pasting a server list, so the role's defaults carry the real endpoints from the
reference project - not placeholders. These tests fail if the placeholders ever
creep back, if a city loses its fallback, or if the set shrinks below five
cities. They also confirm the list stays overridable, so a fleet can swap in its
own servers without touching the role.
"""

from __future__ import annotations

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "roles" / "node_capacity_test" / "defaults" / "main.yml"

REQUIRED_CITIES = {"Moscow", "SaintPetersburg", "NizhnyNovgorod", "Chelyabinsk", "Tyumen"}
# The current reference set (itdoginfo/russian-iperf3-servers, speedtest.sh).
EXPECTED_PRIMARIES = {
    "spd-rudp.hostkey.ru",
    "st.spb.ertelecom.ru",
    "st.nn.ertelecom.ru",
    "st.chel.ertelecom.ru",
    "st.tmn.ertelecom.ru",
}
EXPECTED_FALLBACKS = {
    "st.tver.ertelecom.ru",
    "st.yar.ertelecom.ru",
    "speed-nn.vtt.net",
    "st.mgn.ertelecom.ru",
    "st.krsk.ertelecom.ru",
}


class EndpointDefaultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = DEFAULTS.read_text(encoding="utf-8")
        self.defaults = yaml.safe_load(self.text)
        self.endpoints = self.defaults["capacity_test_endpoints"]

    def test_no_placeholder_hosts_remain(self) -> None:
        # The .example TLD is reserved for documentation and never resolves; a
        # node could not measure anything against it.
        self.assertNotIn(".example", self.text)

    def test_at_least_five_cities(self) -> None:
        self.assertGreaterEqual(len(self.endpoints), 5)

    def test_the_five_required_cities_are_present(self) -> None:
        cities = {entry["city"] for entry in self.endpoints}
        self.assertTrue(REQUIRED_CITIES.issubset(cities), f"missing: {REQUIRED_CITIES - cities}")

    def test_every_city_has_a_primary_and_a_fallback(self) -> None:
        for entry in self.endpoints:
            hosts = entry.get("hosts") or []
            self.assertGreaterEqual(len(hosts), 2, f"{entry['city']} has no fallback")
            for host in hosts:
                self.assertIn("host", host)
                self.assertIn("ports", host)

    def test_the_real_reference_servers_are_the_defaults(self) -> None:
        # The default value inside each override expression must be a real host.
        for host in EXPECTED_PRIMARIES | EXPECTED_FALLBACKS:
            self.assertIn(host, self.text, f"expected reference server {host} in the defaults")

    def test_every_host_is_overridable(self) -> None:
        # Each host is "{{ capacity_test_host_<city>[_fallback] | default('real') }}",
        # so a single server can be swapped without replacing the whole list.
        for entry in self.endpoints:
            for host in entry["hosts"]:
                self.assertIn("capacity_test_host_", host["host"])
                self.assertIn("| default(", host["host"])

    def test_the_whole_list_is_a_role_default(self) -> None:
        # A top-level role default, so group_vars / fleet.yml override it by
        # ordinary precedence.
        self.assertIn("capacity_test_endpoints", self.defaults)

    def test_ports_are_the_configurable_5201_5209_range(self) -> None:
        self.assertEqual(list(range(5201, 5210)), self.defaults["capacity_test_ports"])
        # And the endpoints reference the port variable rather than hard-coding.
        for entry in self.endpoints:
            for host in entry["hosts"]:
                self.assertIn("capacity_test_ports", host["ports"])


if __name__ == "__main__":
    unittest.main()
