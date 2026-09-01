"""What is allowed to reach a phone, and what this stack deliberately never sends.

This fleet already alerts on a server going down, on nodes and users being
edited, on the torrent blocker and on the CRM, through a system that is not
Prometheus. The value of this file is that it fails when somebody re-adds one
of those here: two systems saying the same thing is how people learn to ignore
both.

The routing half is checked as configuration rather than behaviour - promtool
and amtool prove the behaviour in monitoring/tests/test_monitoring_rules.sh and
test_alert_thresholds.sh, and those need binaries that are not always present.
These assertions need nothing but the files.
"""

from __future__ import annotations

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
ALERTS = ROOT / "prometheus" / "alert-rules.yml"
RECORDING = ROOT / "prometheus" / "recording-rules.yml"
ALERTMANAGER = ROOT / "alertmanager" / "alertmanager.yml.example"


def alert_rules() -> list[dict]:
    document = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    return [rule for group in document["groups"] for rule in group["rules"] if "alert" in rule]


def recorded_names() -> set[str]:
    document = yaml.safe_load(RECORDING.read_text(encoding="utf-8"))
    return {rule["record"] for group in document["groups"] for rule in group["rules"] if "record" in rule}


class NoDuplicateAlertingTests(unittest.TestCase):
    """Alerts the fleet already receives from somewhere else must not exist here."""

    #: Every one of these is covered by the existing alerting system.
    FORBIDDEN = (
        "AugustNodeOffline",
        "AugustNodeUnhealthy",
        "AugustNodeUnknownToPanel",
        "AugustNodeAdministrativelyDisabled",
        "AugustNodeIdentityUnmapped",
        "AugustNodeIdentityAmbiguous",
        "AugustTopologyDrift",
        "AugustScrapeTargetDown",
        "AugustPoolSingleNodeRemaining",
        "AugustCapacityUnrated",
        "AugustSemaphoreExporterDegraded",
        "AugustSemaphoreNoRecentInstall",
    )

    def setUp(self) -> None:
        self.names = {rule["alert"] for rule in alert_rules()}

    def test_none_of_the_duplicated_alerts_is_declared(self) -> None:
        for name in self.FORBIDDEN:
            self.assertNotIn(
                name,
                self.names,
                f"{name} duplicates the fleet's existing alerting system; keep the metric, drop the alert",
            )

    def test_nothing_alerts_on_a_node_being_down_or_edited(self) -> None:
        # Not by name but by what the expression reads: a rule that watches
        # connectivity, panel membership or topology is a duplicate whatever it
        # is called.
        watched = (
            "august_node_connected",
            "august_node_known_to_panel",
            "august_topology_drift",
            "august_node_identity_problems",
            "august_capacity_unrated",
            'up{job="august_node_exporter"',
        )
        for rule in alert_rules():
            for series in watched:
                self.assertNotIn(
                    series,
                    rule["expr"],
                    f"{rule['alert']} alerts on {series}, which the existing system already reports",
                )

    def test_the_metrics_behind_them_are_still_published(self) -> None:
        # The point of removing the alerts is that the dashboard keeps the data.
        # These recording rules must survive, or the panels go blank.
        for record in ("august:node_health_status", "august:node_health_signal", "august:node_meta"):
            self.assertIn(record, recorded_names(), f"{record} is still needed by the dashboard")


class CriticalTierTests(unittest.TestCase):
    """Exactly five things may page somebody, and these are they."""

    EXPECTED_CRITICAL = {
        "AugustNodeCpuCritical",
        "AugustNodeMemoryCritical",
        "AugustNodeBandwidthCritical",
        "AugustPoolBandwidthCritical",
        "AugustBridgeBandwidthCritical",
    }

    def test_the_critical_set_is_exactly_the_agreed_five(self) -> None:
        critical = {r["alert"] for r in alert_rules() if r["labels"]["severity"] == "critical"}
        self.assertEqual(
            self.EXPECTED_CRITICAL,
            critical,
            "the set of alerts that reach Telegram changed; that is a decision, not a refactor",
        )

    def test_every_alert_declares_a_known_severity(self) -> None:
        for rule in alert_rules():
            self.assertIn(
                rule["labels"]["severity"],
                ("critical", "warning", "info"),
                f"{rule['alert']} has an unroutable severity",
            )

    def test_thresholds_and_delays_are_the_agreed_ones(self) -> None:
        expected = {
            "AugustNodeCpuHigh": ("0.80", "10m"),
            "AugustNodeCpuCritical": ("0.90", "5m"),
            "AugustNodeMemoryHigh": ("0.85", "10m"),
            "AugustNodeMemoryCritical": ("0.95", "5m"),
            "AugustNodeBandwidthLow": ("500", "5m"),
            "AugustNodeBandwidthCritical": ("200", "5m"),
            "AugustPoolBandwidthLow": ("500", "5m"),
            "AugustPoolBandwidthCritical": ("200", "5m"),
            "AugustBridgeBandwidthLow": ("500", "5m"),
            "AugustBridgeBandwidthCritical": ("200", "5m"),
        }
        by_name = {rule["alert"]: rule for rule in alert_rules()}
        for name, (threshold, delay) in expected.items():
            self.assertIn(name, by_name, f"{name} is missing")
            self.assertIn(threshold, by_name[name]["expr"], f"{name} no longer uses {threshold}")
            self.assertEqual(delay, by_name[name]["for"], f"{name} no longer waits {delay}")

    def test_bandwidth_pages_on_absolute_headroom_not_a_ratio(self) -> None:
        # The whole reason the model changed: 20% of 5 Gbit/s is not an
        # emergency and 20% of 1 Gbit/s is.
        for name in ("AugustNodeBandwidthCritical", "AugustPoolBandwidthCritical",
                     "AugustBridgeBandwidthCritical"):
            expr = next(r["expr"] for r in alert_rules() if r["alert"] == name)
            self.assertIn("free_mbps", expr, f"{name} must read Mbit/s free")
            self.assertNotIn("free_ratio", expr, f"{name} must not read a percentage")

    def test_bandwidth_alerts_cannot_fire_on_an_unmeasured_capacity(self) -> None:
        # They read the august:alert_* rules, which exist only where a rating
        # does. Reading the raw free_mbps would alert on a pool whose figure
        # became an upper bound.
        for rule in alert_rules():
            if "Bandwidth" in rule["alert"]:
                self.assertIn("august:alert_", rule["expr"], f"{rule['alert']} bypasses the alertable view")


class RoutingTests(unittest.TestCase):
    """Telegram gets severity=critical, and nothing else has an integration."""

    def setUp(self) -> None:
        self.config = yaml.safe_load(ALERTMANAGER.read_text(encoding="utf-8"))
        self.receivers = {r["name"]: r for r in self.config["receivers"]}

    def test_the_only_route_with_a_receiver_that_notifies_selects_critical(self) -> None:
        notifying = {
            name for name, body in self.receivers.items()
            if any(key.endswith("_configs") for key in body)
        }
        self.assertTrue(notifying, "no receiver sends anything at all")
        for route in self.config["route"]["routes"]:
            if route["receiver"] in notifying:
                self.assertEqual(
                    ['severity = "critical"'],
                    route["matchers"],
                    f"the route to {route['receiver']} does not select severity=critical only",
                )

    def test_the_default_receiver_notifies_nobody(self) -> None:
        # Everything that does not match the critical route lands here, so if
        # this ever grows an integration every warning starts paging.
        default = self.receivers[self.config["route"]["receiver"]]
        self.assertEqual(
            ["name"],
            list(default),
            "the catch-all receiver gained an integration; warnings would start notifying",
        )

    def test_telegram_reads_its_token_from_a_file(self) -> None:
        telegram = self.receivers["telegram-critical"]["telegram_configs"][0]
        self.assertIn("bot_token_file", telegram)
        self.assertNotIn("bot_token", telegram)

    def test_no_secret_is_committed(self) -> None:
        text = ALERTMANAGER.read_text(encoding="utf-8")
        self.assertIn("<TELEGRAM_CHAT_ID>", text, "the chat id must stay a placeholder")
        self.assertNotIn("bot_token:", text)
        # A real bot token is digits, a colon and 35 URL-safe characters.
        import re

        self.assertIsNone(
            re.search(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\b", text),
            "a Telegram bot token is committed in this file",
        )

    def test_every_severity_a_rule_can_emit_is_routable(self) -> None:
        emitted = {rule["labels"]["severity"] for rule in alert_rules()}
        routed = {
            matcher.split('"')[1]
            for route in self.config["route"]["routes"]
            for matcher in route["matchers"]
            if matcher.startswith("severity")
        }
        # Anything not explicitly routed falls through to the catch-all, which
        # is fine - but the critical tier must never be the one that falls
        # through, because that would mean nothing pages.
        self.assertIn("critical", routed)
        self.assertTrue(emitted - routed <= {"warning", "info"})


if __name__ == "__main__":
    unittest.main()
