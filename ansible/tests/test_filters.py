from __future__ import annotations

import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "filter_plugins" / "remnawave.py"
SPEC = importlib.util.spec_from_file_location("remnawave_filters", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RemnawaveFilterTests(unittest.TestCase):
    def test_subset_ignores_runtime_fields_inside_lists(self) -> None:
        desired = {"inbounds": [{"tag": "DE_REALITY", "port": 443}]}
        actual = {
            "inbounds": [
                {
                    "uuid": "inbound-uuid",
                    "tag": "DE_REALITY",
                    "port": 443,
                    "createdAt": "runtime",
                }
            ]
        }
        self.assertTrue(MODULE.remnawave_is_subset(desired, actual, ["uuid"]))

    def test_subset_detects_managed_change(self) -> None:
        self.assertFalse(
            MODULE.remnawave_is_subset(
                {"address": "203.0.113.10", "port": 443},
                {"address": "203.0.113.11", "port": 443},
            )
        )

    def test_response_items_supports_both_envelopes(self) -> None:
        self.assertEqual(
            MODULE.remnawave_response_items({"response": [1, 2]}), [1, 2]
        )
        self.assertEqual(
            MODULE.remnawave_response_items(
                {"response": {"configProfiles": [3]}}, "configProfiles"
            ),
            [3],
        )

    def test_foreign_inbound_owner_is_reported(self) -> None:
        profiles = [
            {
                "uuid": "foreign",
                "name": "Foreign",
                "config": {"inbounds": [{"tag": "DE_REALITY"}]},
            }
        ]
        self.assertEqual(
            MODULE.remnawave_inbound_owners(profiles, ["DE_REALITY"]),
            [{"uuid": "foreign", "name": "Foreign", "tag": "DE_REALITY"}],
        )

    def test_uuid_list_preserves_order_and_removes_duplicates(self) -> None:
        self.assertEqual(
            MODULE.remnawave_uuid_list([{"uuid": "a"}, "b", {"uuid": "a"}]),
            ["a", "b"],
        )

    def test_reality_settings_are_extracted(self) -> None:
        config = {
            "inbounds": [
                {
                    "streamSettings": {
                        "realitySettings": {
                            "privateKey": "secret",
                            "shortIds": ["0011"],
                        }
                    }
                }
            ]
        }
        self.assertEqual(
            MODULE.remnawave_reality_settings(config)["shortIds"], ["0011"]
        )

    def test_ip_membership_handles_provider_proxy_sources(self) -> None:
        self.assertTrue(
            MODULE.remnawave_ip_in_cidrs(
                "10.20.30.40", ["203.0.113.1/32", "10.20.30.0/24"]
            )
        )
        self.assertFalse(
            MODULE.remnawave_ip_in_cidrs("10.20.31.40", ["10.20.30.0/24"])
        )


if __name__ == "__main__":
    unittest.main()
