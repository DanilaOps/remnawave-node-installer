"""The documents PyYAML accepts and this project must not.

Each case is a real mistake with a silent consequence, which is why the loader
refuses rather than warns.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import strict_yaml  # noqa: E402

import yaml  # noqa: E402

CAPACITY_FILE = ROOT / "capacity" / "capacity.yml"


class DuplicateKeyTests(unittest.TestCase):
    def test_plain_yaml_keeps_the_last_duplicate(self) -> None:
        # The behaviour this loader exists to prevent, asserted so the reason is
        # visible rather than folklore.
        self.assertEqual({"a": 2}, yaml.safe_load("a: 1\na: 2\n"))

    def test_a_duplicate_key_is_refused(self) -> None:
        with self.assertRaises(strict_yaml.StrictYamlError) as caught:
            strict_yaml.load("a: 1\na: 2\n")
        self.assertIn("duplicate key", str(caught.exception))

    def test_a_duplicate_node_is_refused(self) -> None:
        document = """
        nodes:
          DE-01:
            pool: DE
          DE-01:
            pool: EE
        """
        with self.assertRaises(strict_yaml.StrictYamlError):
            strict_yaml.load(document)

    def test_a_duplicate_pool_is_refused(self) -> None:
        with self.assertRaises(strict_yaml.StrictYamlError):
            strict_yaml.load("pools:\n  DE:\n    members: []\n  DE:\n    members: []\n")

    def test_a_duplicate_bridge_is_refused(self) -> None:
        with self.assertRaises(strict_yaml.StrictYamlError):
            strict_yaml.load("bridges:\n  A:\n    enabled: true\n  A:\n    enabled: false\n")

    def test_the_error_names_the_line(self) -> None:
        with self.assertRaises(strict_yaml.StrictYamlError) as caught:
            strict_yaml.load("a: 1\nb: 2\nb: 3\n")
        self.assertIn("line 3", str(caught.exception))


class NumericTests(unittest.TestCase):
    def test_nan_is_refused(self) -> None:
        # A NaN capacity poisons every sum it touches into NaN.
        for spelling in (".nan", ".NaN", "-.nan"):
            with self.subTest(spelling=spelling):
                with self.assertRaises(strict_yaml.StrictYamlError):
                    strict_yaml.load(f"mbps: {spelling}\n")

    def test_infinity_is_refused(self) -> None:
        # An infinite capacity makes free capacity infinite and the fleet
        # permanently green.
        for spelling in (".inf", "-.inf", ".Inf"):
            with self.subTest(spelling=spelling):
                with self.assertRaises(strict_yaml.StrictYamlError):
                    strict_yaml.load(f"mbps: {spelling}\n")

    def test_a_negative_capacity_is_refused(self) -> None:
        with self.assertRaises(strict_yaml.StrictYamlError):
            strict_yaml.load("mbps: -1000\n")

    def test_a_negative_quota_is_refused(self) -> None:
        with self.assertRaises(strict_yaml.StrictYamlError):
            strict_yaml.load("quota_bytes: -1\n")

    def test_a_quoted_number_is_refused(self) -> None:
        with self.assertRaises(strict_yaml.StrictYamlError) as caught:
            strict_yaml.load('mbps: "1000"\n')
        self.assertIn("string", str(caught.exception))

    def test_a_boolean_is_refused(self) -> None:
        with self.assertRaises(strict_yaml.StrictYamlError):
            strict_yaml.load("mbps: true\n")

    def test_an_empty_number_is_refused(self) -> None:
        with self.assertRaises(strict_yaml.StrictYamlError):
            strict_yaml.load("mbps:\n")

    def test_zero_is_allowed_where_it_means_unlimited(self) -> None:
        self.assertEqual({"quota_bytes": 0, "session_limit": 0}, strict_yaml.load("quota_bytes: 0\nsession_limit: 0\n"))

    def test_a_nested_capacity_is_checked(self) -> None:
        document = "nodes:\n  DE-01:\n    capacity:\n      download:\n        mbps: -5\n"
        with self.assertRaises(strict_yaml.StrictYamlError):
            strict_yaml.load(document)

    def test_a_float_capacity_is_allowed(self) -> None:
        self.assertEqual({"mbps": 999.5}, strict_yaml.load("mbps: 999.5\n"))


class MalformedTests(unittest.TestCase):
    def test_broken_yaml_becomes_a_strict_error(self) -> None:
        # So that one caller-visible exception type covers every refusal and the
        # exporter's except clause cannot miss one.
        with self.assertRaises(strict_yaml.StrictYamlError):
            strict_yaml.load("a: [1, 2\n")

    def test_a_tab_indent_is_refused(self) -> None:
        with self.assertRaises(strict_yaml.StrictYamlError):
            strict_yaml.load("a:\n\tb: 1\n")

    def test_an_empty_document_loads_as_none(self) -> None:
        self.assertIsNone(strict_yaml.load(""))


class ShippedFileTests(unittest.TestCase):
    def test_the_committed_capacity_file_passes_the_strict_loader(self) -> None:
        document = strict_yaml.load(CAPACITY_FILE.read_text(encoding="utf-8"))
        self.assertEqual(19, len(document["nodes"]))
        self.assertEqual(13, len(document["pools"]))
        self.assertEqual(4, len(document["bridges"]))

    def test_the_strict_loader_and_safe_load_agree_on_it(self) -> None:
        text = CAPACITY_FILE.read_text(encoding="utf-8")
        self.assertEqual(yaml.safe_load(text), strict_yaml.load(text))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
