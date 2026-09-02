from __future__ import annotations

import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "filter_plugins" / "dns.py"
SPEC = importlib.util.spec_from_file_location("remnawave_dns", MODULE_PATH)
assert SPEC and SPEC.loader
DNS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DNS)


def answer(records: list[dict] | None, dname: str = "example.com") -> dict:
    return {"result": "success", "answer": {"domains": [{"dname": dname, "rrs": records}]}}


class SubdomainTests(unittest.TestCase):
    def test_extracts_the_record_name(self) -> None:
        self.assertEqual(
            DNS.remnawave_dns_subdomain("ee01.example.com", "example.com"), "ee01"
        )
        self.assertEqual(
            DNS.remnawave_dns_subdomain("a.b.example.com", "example.com"), "a.b"
        )

    def test_apex_becomes_at_sign(self) -> None:
        self.assertEqual(DNS.remnawave_dns_subdomain("example.com", "example.com"), "@")

    def test_trailing_dots_and_case_are_ignored(self) -> None:
        self.assertEqual(
            DNS.remnawave_dns_subdomain("EE01.Example.com.", "example.com"), "ee01"
        )

    def test_foreign_zone_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            DNS.remnawave_dns_subdomain("ee01.other.com", "example.com")


class RecordNormalisationTests(unittest.TestCase):
    def test_regru_field_spelling(self) -> None:
        records = DNS.remnawave_dns_records(
            answer([{"rectype": "A", "subname": "ee01", "content": "203.0.113.10"}]),
            "example.com",
        )
        self.assertEqual(
            records, [{"type": "A", "name": "ee01", "content": "203.0.113.10"}]
        )

    def test_alternative_field_spellings(self) -> None:
        records = DNS.remnawave_dns_records(
            answer([{"record_type": "a", "subdomain": "ee01", "ipaddr": "203.0.113.10"}]),
            "example.com",
        )
        self.assertEqual(records[0]["type"], "A")
        self.assertEqual(records[0]["content"], "203.0.113.10")

    def test_missing_name_is_the_apex(self) -> None:
        records = DNS.remnawave_dns_records(
            answer([{"rectype": "A", "content": "203.0.113.10"}]), "example.com"
        )
        self.assertEqual(records[0]["name"], "@")

    def test_empty_zone_is_allowed(self) -> None:
        self.assertEqual(DNS.remnawave_dns_records(answer([]), "example.com"), [])
        self.assertEqual(DNS.remnawave_dns_records(answer(None), "example.com"), [])

    def test_absent_zone_raises_instead_of_looking_empty(self) -> None:
        # Treating an unparsable answer as "no records" would make the role create
        # a duplicate record on every run.
        with self.assertRaises(Exception):
            DNS.remnawave_dns_records(answer([], dname="other.com"), "example.com")
        with self.assertRaises(Exception):
            DNS.remnawave_dns_records({"result": "success", "answer": {}}, "example.com")
        with self.assertRaises(Exception):
            DNS.remnawave_dns_records(
                {"result": "success", "answer": {"domains": [{"dname": "example.com", "rrs": "oops"}]}},
                "example.com",
            )


class MatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            {"type": "A", "name": "ee01", "content": "203.0.113.10"},
            {"type": "A", "name": "ee02", "content": "203.0.113.11"},
            {"type": "AAAA", "name": "ee01", "content": "2001:db8::1"},
            {"type": "MX", "name": "@", "content": "mail.example.com"},
        ]

    def test_matches_only_the_requested_name_and_type(self) -> None:
        matched = DNS.remnawave_dns_matches(self.records, "ee01", "A")
        self.assertEqual(matched, [{"type": "A", "name": "ee01", "content": "203.0.113.10"}])

    def test_case_insensitive(self) -> None:
        self.assertEqual(len(DNS.remnawave_dns_matches(self.records, "EE01", "a")), 1)

    def test_no_match_is_empty(self) -> None:
        self.assertEqual(DNS.remnawave_dns_matches(self.records, "ee09", "A"), [])


if __name__ == "__main__":
    unittest.main()
