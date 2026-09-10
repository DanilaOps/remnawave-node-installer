"""Deterministic per-node decoy site generation.

Every masking node serves a real-looking site in front of Reality.  Two failure
modes matter:

* If all nodes serve the same page, one scan links the whole fleet by page hash,
  DOM shape, favicon, asset names, class names and response size.
* If the page changes on every Ansible run, the deployment stops being
  idempotent and produces visible churn for no benefit.

The site is therefore a pure function of a stable per-node seed: the same node
gets a byte-identical site forever, a different node gets a visibly different
brand, copy, layout, palette, class names, asset names and response size.

Values are derived from SHA-256 of `seed | field-label`, deliberately not from a
single `random.Random` stream.  Two properties follow, and both matter here:
a stream's output is an interpreter implementation detail that may change
between releases, and drawing fields in sequence means adding one new field
shifts every value after it - which would re-randomise the entire fleet's sites
on an unrelated template change.  Labelled hashing keeps every existing field
stable when a new one is added.
"""

from __future__ import annotations

import hashlib
from typing import Any, Sequence

from ansible.errors import AnsibleFilterError


BRAND_ADJECTIVES = [
    "Northwind", "Lumen", "Nimbus", "Vivid", "Atlas", "Pulse", "Quartz",
    "Onyx", "Vertex", "Cobalt", "Ember", "Solace", "Zephyr", "Apex",
    "Meridian", "Cedar", "Indigo", "Mistral", "Harbor", "Summit", "Aurora",
    "Basalt", "Cortex", "Drift",
]
BRAND_NOUNS = [
    "Systems", "Works", "Hub", "Studio", "Labs", "Stream", "Desk", "Space",
    "Grid", "Port", "Loop", "Stack", "Nest", "Core", "Pixel", "Bay", "Field",
    "Crest", "Point", "Forge", "Yard", "Deck", "Group", "Collective",
]
BRAND_SUFFIXES = ["", "", "", " Ltd", " Inc", " Co", " Group"]

TAGLINES = [
    "Infrastructure that stays out of your way",
    "Simple tools for busy teams",
    "Built for speed, designed for clarity",
    "Everything your team needs in one place",
    "Reliable hosting, predictable billing",
    "Ship faster with less overhead",
    "Quiet, dependable infrastructure",
    "Small tools, serious uptime",
]
DESCRIPTIONS = [
    "Managed hosting, storage and monitoring for small teams.",
    "A calm place to run your services.",
    "Predictable infrastructure with transparent pricing.",
    "Storage, backups and status pages without the ceremony.",
    "Practical tooling for people who ship.",
    "Hosting and monitoring that just keeps running.",
]
NAV_POOL = [
    "Product", "Pricing", "Docs", "Status", "Support", "Blog", "About",
    "Contact", "Changelog", "Guides",
]
FEATURE_POOL = [
    ("Managed backups", "Nightly snapshots with a fourteen day window and one-click restore."),
    ("Status transparency", "Every incident is posted with a timeline and a follow-up note."),
    ("Predictable pricing", "One rate per environment. No egress surprises at the end of the month."),
    ("Regional storage", "Objects stay in the region you choose, with signed URLs by default."),
    ("Quiet monitoring", "Alerts only when something is actually wrong, grouped by service."),
    ("Simple access control", "Per-project roles, audit trail included, no seat maths."),
    ("Fast provisioning", "New environments come up in under a minute from a saved profile."),
    ("Plain documentation", "Short pages, working examples, no marketing in the reference."),
    ("Scheduled reports", "Weekly usage summaries delivered wherever your team already reads."),
    ("Straightforward migration", "Bring an existing setup across with a guided checklist."),
]
ABOUT_POOL = [
    "We are a small team running infrastructure for other small teams. "
    "The service has been in continuous operation since our first customer, "
    "and we still answer support ourselves.",
    "This service started as an internal tool and grew into a product because "
    "the people who borrowed it kept asking for accounts. We keep it deliberately small.",
    "We build and operate everything ourselves in a handful of regions. "
    "That keeps the surface small and the answers honest when something breaks.",
    "Our focus is boring reliability: fewer features, documented behaviour and "
    "a support queue that a human reads the same day.",
]
STATUS_LINES = [
    ("API", "Operational"),
    ("Dashboard", "Operational"),
    ("Object storage", "Operational"),
    ("Backups", "Operational"),
    ("Monitoring", "Operational"),
]
CTA_POOL = [
    ("Get started", "Create an account"),
    ("Open dashboard", "Sign in"),
    ("Read the docs", "Documentation"),
    ("Talk to us", "Contact support"),
]
FOOTER_NOTES = [
    "All rights reserved.",
    "Operated independently.",
    "Registered service provider.",
    "Thanks for stopping by.",
]
FONT_STACKS = [
    "system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    "'Helvetica Neue', Helvetica, Arial, system-ui, sans-serif",
    "Georgia, 'Times New Roman', Times, serif",
    "'IBM Plex Sans', system-ui, 'Segoe UI', Roboto, sans-serif",
    "Verdana, Geneva, system-ui, sans-serif",
]
LAYOUTS = ["split", "stacked", "panel"]
SECTION_KEYS = ["features", "about", "status", "contact"]
FAVICON_SHAPES = ["rounded", "circle", "hex", "square"]
REFERRER_POLICIES = [
    "no-referrer",
    "same-origin",
    "strict-origin",
    "strict-origin-when-cross-origin",
]
ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


class _Seeded:
    """Independent, reproducible pseudo-random values derived from a seed."""

    def __init__(self, seed: str) -> None:
        if not isinstance(seed, str) or not seed:
            raise AnsibleFilterError("decoy seed must be a non-empty string")
        self.seed = seed

    def _value(self, label: str) -> int:
        digest = hashlib.sha256(f"{self.seed}\x00{label}".encode()).digest()
        return int.from_bytes(digest, "big")

    def integer(self, label: str, low: int, high: int) -> int:
        if high < low:
            raise AnsibleFilterError(f"invalid range for {label}")
        return low + self._value(label) % (high - low + 1)

    def choice(self, label: str, options: Sequence[Any]) -> Any:
        if not options:
            raise AnsibleFilterError(f"no options for {label}")
        return options[self._value(label) % len(options)]

    def boolean(self, label: str, percent: int = 50) -> bool:
        return self.integer(label, 0, 99) < percent

    def shuffled(self, label: str, options: Sequence[Any]) -> list[Any]:
        # Deterministic Fisher-Yates driven by per-position digests.
        result = list(options)
        for index in range(len(result) - 1, 0, -1):
            swap = self.integer(f"{label}#{index}", 0, index)
            result[index], result[swap] = result[swap], result[index]
        return result

    def sample(self, label: str, options: Sequence[Any], count: int) -> list[Any]:
        return self.shuffled(label, options)[:count]

    def token(self, label: str, length: int = 6) -> str:
        digest = self._value(label)
        out = []
        for _ in range(length):
            out.append(ALPHABET[digest % len(ALPHABET)])
            digest //= len(ALPHABET)
        return "".join(out)

    def css_name(self, label: str) -> str:
        # Must start with a letter to be a valid CSS identifier.
        return "c" + self.token(f"class:{label}", 5)


def remnawave_decoy_profile(seed: Any, variant: str = "primary") -> dict[str, Any]:
    """Return the complete description of one node's decoy site."""

    if not isinstance(variant, str) or not variant:
        raise AnsibleFilterError("decoy variant must be a non-empty string")
    rng = _Seeded(f"{seed}\x01{variant}")

    brand = (
        f"{rng.choice('brand.adjective', BRAND_ADJECTIVES)} "
        f"{rng.choice('brand.noun', BRAND_NOUNS)}"
        f"{rng.choice('brand.suffix', BRAND_SUFFIXES)}"
    )
    brand_short = brand.split(" ")[0]
    tagline = rng.choice("tagline", TAGLINES)
    title_shape = rng.integer("title.shape", 0, 2)
    if title_shape == 0:
        title = brand
    elif title_shape == 1:
        title = f"{brand} - {rng.choice('title.word', ['Home', 'Dashboard', 'Portal', 'Cloud', 'Services'])}"
    else:
        title = f"{brand_short} - {tagline}"

    sections = rng.sample("sections", SECTION_KEYS, rng.integer("sections.count", 2, 4))
    css_token = rng.token("asset.css", 8)
    js_token = rng.token("asset.js", 8)
    logo_token = rng.token("asset.logo", 6)
    include_js = rng.boolean("asset.js.include", 65)
    cta_primary, cta_secondary = rng.choice("cta", CTA_POOL)

    return {
        "seed": rng.seed,
        "brand": brand,
        "brand_short": brand_short,
        "tagline": tagline,
        "description": rng.choice("description", DESCRIPTIONS),
        "title": title,
        "layout": rng.choice("layout", LAYOUTS),
        "sections": sections,
        "nav": rng.sample("nav", NAV_POOL, rng.integer("nav.count", 3, 5)),
        "features": [
            {"title": title_text, "text": body}
            for title_text, body in rng.sample(
                "features", FEATURE_POOL, rng.integer("features.count", 3, 4)
            )
        ],
        "about": rng.choice("about", ABOUT_POOL),
        "status": rng.sample("status", STATUS_LINES, rng.integer("status.count", 3, 5)),
        "cta_primary": cta_primary,
        "cta_secondary": cta_secondary,
        "footer_note": rng.choice("footer", FOOTER_NOTES),
        "established": rng.integer("established", 2009, 2021),
        "palette": {
            "hue": rng.integer("palette.hue", 0, 359),
            "accent_shift": rng.choice("palette.shift", [-140, -90, -45, 45, 90, 140]),
            "saturation": rng.integer("palette.saturation", 28, 74),
            "surface_lightness": rng.integer("palette.surface", 94, 99),
            "ink_lightness": rng.integer("palette.ink", 12, 26),
            "radius": rng.integer("palette.radius", 0, 14),
            "spacing": rng.integer("palette.spacing", 14, 30),
            "measure": rng.integer("palette.measure", 58, 76),
            "font_stack": rng.choice("palette.font", FONT_STACKS),
            "uppercase_nav": rng.boolean("palette.nav.uppercase", 40),
        },
        "classes": {
            key: rng.css_name(key)
            for key in (
                "page", "bar", "nav", "hero", "lead", "grid", "card",
                "section", "status", "row", "foot", "cta", "badge", "logo",
            )
        },
        "assets": {
            "css": f"{rng.choice('asset.css.name', ['style', 'main', 'app', 'site'])}.{css_token}.css",
            "js": f"{rng.choice('asset.js.name', ['app', 'main', 'site', 'bundle'])}.{js_token}.js"
            if include_js
            else "",
            "logo": f"{rng.choice('asset.logo.name', ['logo', 'mark', 'brand'])}-{logo_token}.svg",
        },
        "favicon": {
            "shape": rng.choice("favicon.shape", FAVICON_SHAPES),
            "hue": rng.integer("favicon.hue", 0, 359),
            "letter": brand_short[0].upper(),
            "show_letter": rng.boolean("favicon.letter", 55),
        },
        "headers": {
            "cache_max_age": rng.choice("headers.cache", [60, 300, 600, 1800, 3600]),
            "asset_max_age": rng.choice("headers.asset_cache", [3600, 86400, 604800]),
            "referrer_policy": rng.choice("headers.referrer", REFERRER_POLICIES),
            "frame_options": rng.boolean("headers.frame", 70),
            "content_type_options": rng.boolean("headers.cto", 80),
            "etag": rng.boolean("headers.etag", 60),
        },
        "filler": {
            "bytes": rng.integer("filler.bytes", 180, 1400),
            "token": rng.token("filler.token", 24),
        },
        "robots_allow_all": rng.boolean("robots.allow", 70),
    }


def remnawave_decoy_filler(profile: Any) -> str:
    """Return deterministic comment filler that varies the response size."""

    if not isinstance(profile, dict) or "filler" not in profile:
        raise AnsibleFilterError("remnawave_decoy_filler needs a decoy profile")
    token = str(profile["filler"]["token"])
    size = int(profile["filler"]["bytes"])
    if size <= 0:
        return ""
    repeated = (token * (size // len(token) + 1))[:size]
    return "\n".join(repeated[index:index + 96] for index in range(0, size, 96))


class FilterModule:
    """Ansible filter registration."""

    def filters(self) -> dict[str, Any]:
        return {
            "remnawave_decoy_profile": remnawave_decoy_profile,
            "remnawave_decoy_filler": remnawave_decoy_filler,
        }
