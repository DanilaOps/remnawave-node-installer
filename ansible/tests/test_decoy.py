from __future__ import annotations

import importlib.util
import pathlib
import re
import unittest
from html.parser import HTMLParser

import jinja2


ROOT = pathlib.Path(__file__).parents[1]
TEMPLATES = ROOT / "roles" / "remnawave_node" / "templates"
MODULE_PATH = ROOT / "filter_plugins" / "decoy.py"

SPEC = importlib.util.spec_from_file_location("remnawave_decoy", MODULE_PATH)
assert SPEC and SPEC.loader
DECOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DECOY)

VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


class TagBalance(HTMLParser):
    """Minimal structural check: every non-void tag must be closed in order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.titles = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag == "title":
            self.titles += 1
        if tag not in VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            return
        if not self.stack:
            self.errors.append(f"closing </{tag}> with nothing open")
        elif self.stack[-1] != tag:
            self.errors.append(f"closing </{tag}> while <{self.stack[-1]}> is open")
        else:
            self.stack.pop()


def environment() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        keep_trailing_newline=True,
    )
    env.filters["remnawave_decoy_filler"] = DECOY.remnawave_decoy_filler
    env.filters["min"] = min
    return env


def render(name: str, profile: dict) -> str:
    return environment().get_template(name).render(
        decoy=profile,
        selfsteal_site="primary",
        selfsteal_domain="node.example.com",
    )


def profile_for(node_id: str, variant: str = "primary") -> dict:
    return DECOY.remnawave_decoy_profile(f"{node_id}||1", variant)


class SeededValueTests(unittest.TestCase):
    def test_fields_are_independent_of_draw_order(self) -> None:
        # Labelled hashing, not a single random stream: adding a new field must
        # not shift the values of the fields that already exist, otherwise an
        # unrelated template change would re-randomise every node's site.
        first = DECOY._Seeded("ee_01")
        direct = first.choice("layout", DECOY.LAYOUTS)
        second = DECOY._Seeded("ee_01")
        for index in range(50):
            second.token(f"unrelated.{index}", 6)
        self.assertEqual(direct, second.choice("layout", DECOY.LAYOUTS))

    def test_shuffle_and_sample_are_deterministic_permutations(self) -> None:
        rng = DECOY._Seeded("ee_07")
        options = list(range(12))
        shuffled = rng.shuffled("x", options)
        self.assertEqual(sorted(shuffled), options)
        self.assertEqual(shuffled, DECOY._Seeded("ee_07").shuffled("x", options))
        self.assertEqual(len(rng.sample("y", options, 4)), 4)

    def test_empty_seed_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            DECOY._Seeded("")


class DeterminismTests(unittest.TestCase):
    def test_same_seed_gives_an_identical_profile_and_page(self) -> None:
        self.assertEqual(profile_for("ee_01"), profile_for("ee_01"))
        self.assertEqual(
            render("index.html.j2", profile_for("ee_01")),
            render("index.html.j2", profile_for("ee_01")),
        )
        self.assertEqual(
            render("site.css.j2", profile_for("ee_01")),
            render("site.css.j2", profile_for("ee_01")),
        )

    def test_salt_and_generation_reroll_the_site(self) -> None:
        base = DECOY.remnawave_decoy_profile("ee_01||1", "primary")
        salted = DECOY.remnawave_decoy_profile("ee_01|rotate|1", "primary")
        next_generation = DECOY.remnawave_decoy_profile("ee_01||2", "primary")
        self.assertNotEqual(base["classes"], salted["classes"])
        self.assertNotEqual(base["classes"], next_generation["classes"])

    def test_variants_differ_within_one_node(self) -> None:
        primary = profile_for("ee_01", "primary")
        secondary = profile_for("ee_01", "secondary")
        self.assertNotEqual(primary["brand"], secondary["brand"])
        self.assertNotEqual(primary["assets"]["css"], secondary["assets"]["css"])


class FleetUniquenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.node_ids = [f"{cc}_{index:02d}" for cc in ("ee", "de", "nl", "fi") for index in range(1, 17)]
        self.profiles = [profile_for(node_id) for node_id in self.node_ids]
        self.pages = [render("index.html.j2", profile) for profile in self.profiles]

    def test_every_node_gets_a_distinct_page(self) -> None:
        self.assertEqual(len(self.pages), 64)
        self.assertEqual(len(set(self.pages)), 64)

    def test_class_names_and_asset_names_do_not_repeat(self) -> None:
        class_signatures = {tuple(sorted(p["classes"].values())) for p in self.profiles}
        css_names = {p["assets"]["css"] for p in self.profiles}
        logo_names = {p["assets"]["logo"] for p in self.profiles}
        self.assertEqual(len(class_signatures), 64)
        self.assertEqual(len(css_names), 64)
        self.assertEqual(len(logo_names), 64)

    def test_visible_identity_varies(self) -> None:
        brands = {p["brand"] for p in self.profiles}
        titles = {p["title"] for p in self.profiles}
        layouts = {p["layout"] for p in self.profiles}
        hues = [p["palette"]["hue"] for p in self.profiles]
        # A small collision rate in a 4000-combination brand space is expected;
        # a systematic bias is not.
        self.assertGreaterEqual(len(brands), 58)
        self.assertGreaterEqual(len(titles), 58)
        self.assertEqual(layouts, set(DECOY.LAYOUTS))
        self.assertGreater(max(hues) - min(hues), 180)

    def test_response_sizes_are_spread_out(self) -> None:
        sizes = [len(page) for page in self.pages]
        self.assertGreaterEqual(len(set(sizes)), 55)
        self.assertGreater(max(sizes) - min(sizes), 500)

    def test_section_order_and_selection_vary(self) -> None:
        signatures = {tuple(p["sections"]) for p in self.profiles}
        self.assertGreaterEqual(len(signatures), 8)


class RenderedOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = profile_for("ee_01")
        self.page = render("index.html.j2", self.profile)

    def test_page_is_structurally_valid_html(self) -> None:
        parser = TagBalance()
        parser.feed(self.page)
        parser.close()
        self.assertEqual(parser.errors, [])
        self.assertEqual(parser.stack, [])
        self.assertEqual(parser.titles, 1)
        self.assertTrue(self.page.startswith("<!DOCTYPE html>"))

    def test_no_unrendered_template_syntax_anywhere(self) -> None:
        for name in ("index.html.j2", "site.css.j2", "favicon.svg.j2", "logo.svg.j2", "robots.txt.j2", "app.js.j2"):
            rendered = render(name, self.profile)
            self.assertNotIn("{{", rendered, name)
            self.assertNotIn("{%", rendered, name)

    def test_page_references_only_its_own_generated_assets(self) -> None:
        self.assertIn(f'href="/{self.profile["assets"]["css"]}"', self.page)
        self.assertIn(f'src="/{self.profile["assets"]["logo"]}"', self.page)
        if self.profile["assets"]["js"]:
            self.assertIn(f'src="/{self.profile["assets"]["js"]}"', self.page)
        # A masking page must not reach out to third parties.
        for host in ("http://", "https://", "//fonts.", "ipify"):
            self.assertNotIn(host, self.page.replace("http-equiv", ""))

    def test_stylesheet_defines_every_class_the_page_uses(self) -> None:
        css = render("site.css.j2", self.profile)
        used = set(re.findall(r'class="([^"]+)"', self.page))
        names = {name for value in used for name in value.split()}
        generated = set(self.profile["classes"].values())
        for name in names & generated:
            self.assertIn(f".{name}", css)

    def test_favicon_and_logo_are_single_root_svg(self) -> None:
        for name in ("favicon.svg.j2", "logo.svg.j2"):
            svg = render(name, self.profile).strip()
            self.assertEqual(svg.count("<svg"), 1, name)
            self.assertTrue(svg.endswith("</svg>"), name)
            parser = TagBalance()
            parser.feed(svg)
            parser.close()
            self.assertEqual(parser.errors, [], name)

    def test_filler_matches_the_declared_size(self) -> None:
        filler = DECOY.remnawave_decoy_filler(self.profile)
        self.assertEqual(
            len(filler.replace("\n", "")), self.profile["filler"]["bytes"]
        )
        self.assertNotIn("--", filler, "filler must not close the HTML comment")

    def test_filler_rejects_a_non_profile(self) -> None:
        with self.assertRaises(Exception):
            DECOY.remnawave_decoy_filler({"nope": True})


if __name__ == "__main__":
    unittest.main()
