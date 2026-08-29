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


class SharedProfileMergeTests(unittest.TestCase):
    def shared_config(self) -> dict:
        return {
            "inbounds": [
                {
                    "uuid": "foreign-uuid",
                    "tag": "OTHER_REALITY",
                    "streamSettings": {
                        "realitySettings": {"privateKey": "foreign-key"}
                    },
                }
            ],
            "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}],
            "routing": {"rules": [{"type": "field", "outboundTag": "DIRECT"}]},
        }

    def test_upsert_appends_without_touching_the_rest(self) -> None:
        config = self.shared_config()
        merged = MODULE.remnawave_upsert_inbounds(
            config, [{"tag": "DE_REALITY", "port": 443}]
        )
        self.assertEqual(
            [inbound["tag"] for inbound in merged["inbounds"]],
            ["OTHER_REALITY", "DE_REALITY"],
        )
        self.assertEqual(merged["routing"], config["routing"])
        self.assertEqual(merged["outbounds"], config["outbounds"])
        # The source config must not be mutated in place.
        self.assertEqual(len(config["inbounds"]), 1)

    def test_upsert_replaces_by_tag_and_keeps_panel_uuid(self) -> None:
        config = self.shared_config()
        config["inbounds"].append({"uuid": "managed-uuid", "tag": "DE_REALITY", "port": 443})
        merged = MODULE.remnawave_upsert_inbounds(
            config, [{"tag": "DE_REALITY", "port": 8443}]
        )
        managed = [i for i in merged["inbounds"] if i["tag"] == "DE_REALITY"][0]
        self.assertEqual(managed["port"], 8443)
        self.assertEqual(managed["uuid"], "managed-uuid")
        self.assertEqual(len(merged["inbounds"]), 2)

    def test_upsert_prunes_only_declared_stale_tags(self) -> None:
        config = self.shared_config()
        config["inbounds"].append({"tag": "OLD_REALITY", "port": 443})
        merged = MODULE.remnawave_upsert_inbounds(
            config, [{"tag": "DE_REALITY", "port": 443}], ["OLD_REALITY"]
        )
        tags = [inbound["tag"] for inbound in merged["inbounds"]]
        self.assertEqual(tags, ["OTHER_REALITY", "DE_REALITY"])

    def test_upsert_rejects_untagged_inbound(self) -> None:
        with self.assertRaises(Exception):
            MODULE.remnawave_upsert_inbounds(self.shared_config(), [{"port": 443}])

    def test_reality_settings_scoped_to_managed_tags(self) -> None:
        config = self.shared_config()
        config["inbounds"].append(
            {
                "tag": "DE_REALITY",
                "streamSettings": {"realitySettings": {"privateKey": "own-key"}},
            }
        )
        self.assertEqual(
            MODULE.remnawave_reality_settings(config, ["DE_REALITY"])["privateKey"],
            "own-key",
        )
        self.assertEqual(
            MODULE.remnawave_reality_settings(config, ["MISSING"]),
            {},
        )
        # Without a tag filter the first Reality inbound still wins.
        self.assertEqual(
            MODULE.remnawave_reality_settings(config)["privateKey"], "foreign-key"
        )

    def test_strip_inbound_uuids(self) -> None:
        stripped = MODULE.remnawave_strip_inbound_uuids(self.shared_config())
        self.assertNotIn("uuid", stripped["inbounds"][0])
        self.assertIn("routing", stripped)

    def test_normalize_node_links(self) -> None:
        node = {
            "name": "DE-01",
            "configProfile": {
                "activeConfigProfileUuid": "profile-uuid",
                "activeInbounds": [{"uuid": "inbound-uuid", "tag": "DE_REALITY"}],
            },
        }
        normalized = MODULE.remnawave_normalize_node_links(node)
        self.assertEqual(
            normalized["configProfile"]["activeInbounds"], ["inbound-uuid"]
        )
        desired = {
            "name": "DE-01",
            "configProfile": {
                "activeConfigProfileUuid": "profile-uuid",
                "activeInbounds": ["inbound-uuid"],
            },
        }
        self.assertTrue(MODULE.remnawave_is_subset(desired, normalized))

    def test_normalize_host_links(self) -> None:
        host = {"remark": "DE", "nodes": [{"uuid": "node-uuid"}]}
        self.assertEqual(
            MODULE.remnawave_normalize_host_links(host)["nodes"], ["node-uuid"]
        )


if __name__ == "__main__":
    unittest.main()


class RealityPublicKeyTests(unittest.TestCase):
    """The probe connects with the public half of the node's Reality key."""

    # Generated once with cryptography; the pair is fixed so the derivation is
    # checked against a known answer rather than against itself.
    PRIVATE = "oLpYJEHmHMIA2KCpmsoUihaKHH_nfKzH3ex-yM9p8Ek"
    PUBLIC = "j2nxh8TooQQvk3cO_adThfAo6f-GE6gMTu5yReH9SmA"

    def test_public_key_is_derived_from_the_private_key(self) -> None:
        self.assertEqual(self.PUBLIC, MODULE.remnawave_reality_public_key(self.PRIVATE))

    def test_unpadded_and_padded_input_agree(self) -> None:
        self.assertEqual(
            MODULE.remnawave_reality_public_key(self.PRIVATE),
            MODULE.remnawave_reality_public_key(self.PRIVATE + "="),
        )

    def test_garbage_is_rejected_loudly(self) -> None:
        for value in ("", "   ", "not-base64!!", "c2hvcnQ="):
            with self.assertRaises(Exception):
                MODULE.remnawave_reality_public_key(value)


class NodeNamingTests(unittest.TestCase):
    """<COUNTRY>-NN, one above the highest number that country ever used."""

    def test_the_first_node_of_a_country_is_01(self) -> None:
        self.assertEqual("TR-01", MODULE.remnawave_next_node_name([], "TR"))
        self.assertEqual("TR-01", MODULE.remnawave_next_node_name(["DE-01", "NL-07"], "tr"))

    def test_the_next_node_follows_the_highest_one(self) -> None:
        self.assertEqual("TR-02", MODULE.remnawave_next_node_name(["TR-01"], "TR"))
        self.assertEqual("TR-03", MODULE.remnawave_next_node_name(["TR-01", "TR-02"], "TR"))

    def test_a_freed_number_is_not_handed_out_again(self) -> None:
        # TR-03 existed once. Its DNS record, its certificates and somebody's
        # notes still say TR-03, so a different machine must not become it.
        self.assertEqual("TR-04", MODULE.remnawave_next_node_name(["TR-01", "TR-03"], "TR"))
        self.assertEqual([1, 3], MODULE.remnawave_country_ordinals(["TR-01", "TR-03"], "TR"))

    def test_other_countries_do_not_shift_the_count(self) -> None:
        names = ["DE-01", "DE-02", "TR-01", "NL-09"]
        self.assertEqual("TR-02", MODULE.remnawave_next_node_name(names, "TR"))
        self.assertEqual("DE-03", MODULE.remnawave_next_node_name(names, "DE"))

    def test_only_the_exact_form_counts(self) -> None:
        # A name that merely starts with the country code is not this naming.
        for name in ["TREX", "TR-EDGE", "tr-01", "TR01", "TR-1", "XTR-01"]:
            with self.subTest(name=name):
                self.assertEqual([], MODULE.remnawave_country_ordinals([name], "TR"))
        self.assertEqual("TR-01", MODULE.remnawave_next_node_name(["TREX", "TR-EDGE"], "TR"))

    def test_names_may_arrive_as_objects(self) -> None:
        # Panel collections are lists of objects, not of strings.
        self.assertEqual("TR-10", MODULE.remnawave_next_node_name([{"name": "TR-09"}], "TR"))

    def test_the_ordinal_grows_past_two_digits(self) -> None:
        self.assertEqual("TR-100", MODULE.remnawave_next_node_name(["TR-99"], "TR"))

    def test_a_country_code_has_to_be_two_letters(self) -> None:
        for code in ["", "T", "TUR", "T1", "-"]:
            with self.subTest(code=code):
                with self.assertRaises(Exception):
                    MODULE.remnawave_next_node_name([], code)
