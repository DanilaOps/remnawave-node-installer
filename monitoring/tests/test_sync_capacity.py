"""The inventory-to-capacity sync, tested where it can go wrong.

Each test is a way the sync could turn a one-line inventory entry into a wrong
or invalid capacity figure: an aggregate pool quietly gaining a rated member, an
uncertain figure being counted, an address leaking into a file that must carry
none, a number written as a string slipping past the strict loader. The merge is
refused in every one of those cases, and the happy path produces a document the
capacity model accepts unchanged.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import capacity_model as model  # noqa: E402
import strict_yaml  # noqa: E402
import sync_capacity  # noqa: E402

CAPACITY_FILE = ROOT / "capacity" / "capacity.yml"


def base() -> dict:
    return strict_yaml.load(CAPACITY_FILE.read_text(encoding="utf-8"))


class MergeTests(unittest.TestCase):
    def test_a_declared_node_lands_in_a_per_node_pool_and_counts(self) -> None:
        doc = sync_capacity.merge(base(), [
            {"name": "RU-07", "pool": "RU_ENTRY", "download_mbps": 1500, "upload_mbps": 900, "certain": True},
        ])
        node = doc["nodes"]["RU-07"]
        self.assertEqual("RU_ENTRY", node["pool"])
        self.assertEqual({"mbps": 1500, "source": "declared"}, node["capacity"]["download"])
        self.assertEqual({"mbps": 900, "source": "declared"}, node["capacity"]["upload"])
        self.assertEqual({"kind": "inbound", "node": "RU-07"}, node["measurement"])
        self.assertIn("RU-07", doc["pools"]["RU_ENTRY"]["members"])
        # And the model accepts the whole thing.
        self.assertEqual([], model.validate(doc).blockers)

    def test_a_rated_node_in_an_aggregate_pool_is_refused(self) -> None:
        # Pool TR carries a range figure for the whole location; a member that
        # rates itself would be counted twice.
        with self.assertRaises(sync_capacity.SyncError) as caught:
            sync_capacity.merge(base(), [
                {"name": "TR-02", "pool": "TR", "download_mbps": 1000, "upload_mbps": 1000, "certain": True},
            ])
        self.assertIn("counted twice", str(caught.exception))
        self.assertIn("per-node pool", str(caught.exception))

    def test_an_uncertain_node_in_an_aggregate_pool_is_allowed_as_unrated(self) -> None:
        # certain=false -> unmeasured -> not a rated member -> no double count.
        doc = sync_capacity.merge(base(), [
            {"name": "TR-02", "pool": "TR", "download_mbps": 1000, "upload_mbps": 1000, "certain": False},
        ])
        self.assertEqual("unmeasured", doc["nodes"]["TR-02"]["capacity"]["download"]["source"])
        self.assertNotIn("mbps", doc["nodes"]["TR-02"]["capacity"]["download"])
        self.assertIn("TR-02", doc["pools"]["TR"]["members"])
        self.assertEqual([], model.validate(doc).blockers)

    def test_a_missing_figure_is_unmeasured_even_when_certain(self) -> None:
        doc = sync_capacity.merge(base(), [
            {"name": "RU-07", "pool": "RU_ENTRY", "download_mbps": 1000, "certain": True},
        ])
        node = doc["nodes"]["RU-07"]
        self.assertEqual({"mbps": 1000, "source": "declared"}, node["capacity"]["download"])
        self.assertEqual({"source": "unmeasured"}, node["capacity"]["upload"])

    def test_a_brand_new_pool_is_created_without_an_aggregate_figure(self) -> None:
        doc = sync_capacity.merge(base(), [
            {"name": "JP-01", "pool": "JP", "download_mbps": 500, "upload_mbps": 500, "certain": True},
        ])
        self.assertIn("JP", doc["pools"])
        self.assertEqual(["JP-01"], doc["pools"]["JP"]["members"])
        self.assertNotIn("capacity", doc["pools"]["JP"])
        self.assertEqual([], model.validate(doc).blockers)

    def test_inventory_overrides_a_base_node(self) -> None:
        # RU-01 is declared 1000/1000 in the base; the inventory raises it.
        doc = sync_capacity.merge(base(), [
            {"name": "RU-01", "pool": "RU_ENTRY", "download_mbps": 2000, "upload_mbps": 2000, "certain": True},
        ])
        self.assertEqual(2000, doc["nodes"]["RU-01"]["capacity"]["download"]["mbps"])
        # Not duplicated in the members list.
        self.assertEqual(doc["pools"]["RU_ENTRY"]["members"].count("RU-01"), 1)
        self.assertEqual([], model.validate(doc).blockers)

    def test_membership_is_idempotent(self) -> None:
        once = sync_capacity.merge(base(), [
            {"name": "RU-07", "pool": "RU_ENTRY", "download_mbps": 1000, "upload_mbps": 1000, "certain": True},
        ])
        twice = sync_capacity.merge(once, [
            {"name": "RU-07", "pool": "RU_ENTRY", "download_mbps": 1000, "upload_mbps": 1000, "certain": True},
        ])
        self.assertEqual(twice["pools"]["RU_ENTRY"]["members"].count("RU-07"), 1)

    def test_a_node_that_changes_pool_is_moved_not_duplicated(self) -> None:
        # RU-05 is a member of RU_AUTO in the base. Declaring it in RU_ENTRY from
        # the inventory must move it, not leave it in both - two memberships is a
        # validation blocker.
        doc = sync_capacity.merge(base(), [
            {"name": "RU-05", "pool": "RU_ENTRY", "download_mbps": 1000, "upload_mbps": 1000, "certain": True},
        ])
        self.assertNotIn("RU-05", doc["pools"]["RU_AUTO"]["members"])
        self.assertIn("RU-05", doc["pools"]["RU_ENTRY"]["members"])
        self.assertEqual("RU_ENTRY", doc["nodes"]["RU-05"]["pool"])
        self.assertEqual([], model.validate(doc).blockers)

    def test_empty_inventory_leaves_the_base_valid(self) -> None:
        doc = sync_capacity.merge(base(), [])
        self.assertEqual([], model.validate(doc).blockers)
        self.assertEqual(len(base()["nodes"]), len(doc["nodes"]))

    def test_the_base_is_not_mutated(self) -> None:
        original = base()
        before = json.dumps(original, sort_keys=True)
        sync_capacity.merge(original, [
            {"name": "RU-07", "pool": "RU_ENTRY", "download_mbps": 1000, "upload_mbps": 1000, "certain": True},
        ])
        self.assertEqual(before, json.dumps(original, sort_keys=True))


class RefusalTests(unittest.TestCase):
    def test_a_string_number_is_refused(self) -> None:
        with self.assertRaises(sync_capacity.SyncError):
            sync_capacity.merge(base(), [
                {"name": "RU-07", "pool": "RU_ENTRY", "download_mbps": "1000", "upload_mbps": 1000, "certain": True},
            ])

    def test_a_negative_number_is_refused(self) -> None:
        with self.assertRaises(sync_capacity.SyncError):
            sync_capacity.merge(base(), [
                {"name": "RU-07", "pool": "RU_ENTRY", "download_mbps": -5, "upload_mbps": 1000, "certain": True},
            ])

    def test_a_boolean_is_not_a_number(self) -> None:
        with self.assertRaises(sync_capacity.SyncError):
            sync_capacity.merge(base(), [
                {"name": "RU-07", "pool": "RU_ENTRY", "download_mbps": True, "upload_mbps": 1000, "certain": True},
            ])

    def test_a_node_without_a_pool_is_refused(self) -> None:
        with self.assertRaises(sync_capacity.SyncError) as caught:
            sync_capacity.merge(base(), [
                {"name": "RU-07", "download_mbps": 1000, "upload_mbps": 1000, "certain": True},
            ])
        self.assertIn("pool", str(caught.exception))

    def test_a_node_without_a_name_is_refused(self) -> None:
        with self.assertRaises(sync_capacity.SyncError):
            sync_capacity.merge(base(), [
                {"pool": "RU_ENTRY", "download_mbps": 1000, "upload_mbps": 1000, "certain": True},
            ])


class NoSecretsTests(unittest.TestCase):
    """The merged file must carry no address, whatever the inventory passes."""

    def test_an_address_field_is_ignored_not_written(self) -> None:
        # Ansible only ever sends name/pool/figures, but prove that even if an
        # address rode along it would not reach the merged document.
        doc = sync_capacity.merge(base(), [
            {"name": "RU-07", "pool": "RU_ENTRY", "download_mbps": 1000, "upload_mbps": 1000,
             "certain": True, "ansible_host": "203.0.113.9"},
        ])
        blob = json.dumps(doc)
        self.assertNotIn("203.0.113.9", blob)
        self.assertNotIn("ansible_host", blob)
        # The capacity model's own secret scan is clean.
        self.assertEqual([], model.find_secrets(doc))

    def test_the_rendered_output_has_no_address_and_parses(self) -> None:
        doc = sync_capacity.merge(base(), [
            {"name": "RU-07", "pool": "RU_ENTRY", "download_mbps": 1000, "upload_mbps": 1000, "certain": True},
        ])
        rendered = sync_capacity._dump(doc)
        self.assertIn("GENERATED", rendered)
        reparsed = strict_yaml.load(rendered)
        self.assertEqual([], model.validate(reparsed).blockers)


class CliTests(unittest.TestCase):
    def _write(self, tmp: pathlib.Path, nodes: list) -> pathlib.Path:
        path = tmp / "inv.json"
        path.write_text(json.dumps(nodes), encoding="utf-8")
        return path

    def test_check_returns_zero_on_a_valid_merge(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            inv = self._write(tmp, [{"name": "RU-07", "pool": "RU_ENTRY", "download_mbps": 1000, "upload_mbps": 1000, "certain": True}])
            rc = sync_capacity.main(["--base", str(CAPACITY_FILE), "--inventory-json", str(inv), "--check"])
            self.assertEqual(0, rc)

    def test_check_returns_one_on_a_pool_conflict(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            inv = self._write(tmp, [{"name": "TR-02", "pool": "TR", "download_mbps": 1000, "upload_mbps": 1000, "certain": True}])
            rc = sync_capacity.main(["--base", str(CAPACITY_FILE), "--inventory-json", str(inv), "--check"])
            self.assertEqual(1, rc)

    def test_output_is_written_and_valid(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            inv = self._write(tmp, [{"name": "RU-07", "pool": "RU_ENTRY", "download_mbps": 1000, "upload_mbps": 1000, "certain": True}])
            out = tmp / "merged.yml"
            rc = sync_capacity.main(["--base", str(CAPACITY_FILE), "--inventory-json", str(inv), "--output", str(out)])
            self.assertEqual(0, rc)
            self.assertTrue(out.exists())
            doc = strict_yaml.load(out.read_text(encoding="utf-8"))
            self.assertIn("RU-07", doc["nodes"])
            self.assertEqual([], model.validate(doc).blockers)


if __name__ == "__main__":
    unittest.main()
