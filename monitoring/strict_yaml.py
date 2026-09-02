"""A YAML loader that refuses the documents PyYAML is happy to accept.

Every rule here exists because the default loader turns a specific human
mistake into a silently wrong capacity figure rather than an error:

  * **Duplicate mapping keys.** PyYAML keeps the last one. Two ``DE-01:``
    blocks, or a pool listed twice, means the reviewed value is not the value
    that runs - and the diff looks fine.
  * **NaN and Infinity.** ``.nan`` and ``.inf`` are valid YAML floats. A NaN
    capacity poisons every sum it touches into NaN; an infinite one makes free
    capacity infinite and the fleet permanently green.
  * **Negative numbers.** A negative rating is not a small rating, it is a
    number that makes a total smaller than its parts.
  * **Numbers written as strings.** ``mbps: "1000"`` looks right and compares
    wrong; ``mbps: 1_000`` and ``mbps: 1e3`` parse to something the reader did
    not intend.

The loader raises StrictYamlError, which the caller turns into a validation
blocker. Nothing here reads the network and nothing is Ansible-specific.
"""

from __future__ import annotations

import math
from typing import Any

import yaml


class StrictYamlError(ValueError):
    """A document that parses but must not be trusted."""

    def __init__(self, message: str, mark: Any = None) -> None:
        if mark is not None:
            message = f"{message} (line {mark.line + 1}, column {mark.column + 1})"
        super().__init__(message)


# Keys whose value is a capacity or a limit. These are the ones checked for
# NaN, Infinity, negatives and string-shaped numbers, because these are the ones
# that end up in arithmetic.
NUMERIC_KEYS = frozenset(
    {
        "mbps",
        "mbps_min",
        "mbps_max",
        "session_limit",
        "quota_bytes",
        "port",
    }
)


class StrictLoader(yaml.SafeLoader):
    """SafeLoader plus the four refusals above."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:  # type: ignore[override]
        seen: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise StrictYamlError(f"duplicate key {key!r}", key_node.start_mark)
            seen[key] = key_node
        mapping = super().construct_mapping(node, deep=deep)
        for key, value in mapping.items():
            if key in NUMERIC_KEYS:
                _check_number(key, value, node.start_mark)
        return mapping


def _check_number(key: str, value: Any, mark: Any) -> None:
    if isinstance(value, bool):
        raise StrictYamlError(f"{key} must be a number, got a boolean", mark)
    if isinstance(value, str):
        raise StrictYamlError(
            f"{key} is quoted ({value!r}); a capacity has to be a number, not a string", mark
        )
    if value is None:
        raise StrictYamlError(f"{key} is empty; state a number or remove the key", mark)
    if not isinstance(value, (int, float)):
        raise StrictYamlError(f"{key} must be a number, got {type(value).__name__}", mark)
    if isinstance(value, float) and not math.isfinite(value):
        raise StrictYamlError(f"{key} is {value}; NaN and Infinity are not capacities", mark)
    if value < 0:
        raise StrictYamlError(f"{key} is negative ({value})", mark)


# YAML 1.1 spells NaN and Infinity as .nan / .inf and PyYAML resolves them to
# floats. The keys above are checked by value, but a stray one anywhere else in
# the document would still propagate, so the resolver is removed outright.
def _no_special_floats(loader: yaml.Loader, node: yaml.ScalarNode) -> Any:
    text = str(node.value)
    lowered = text.lower().lstrip("+-")
    if lowered in (".nan", ".inf", "nan", "inf", "infinity"):
        raise StrictYamlError(f"{text!r} is not a number this file may contain", node.start_mark)
    return yaml.SafeLoader.construct_yaml_float(loader, node)  # type: ignore[arg-type]


StrictLoader.add_constructor("tag:yaml.org,2002:float", _no_special_floats)


def load(text: str) -> Any:
    """Parse a document, refusing everything described in the module docstring."""
    try:
        return yaml.load(text, Loader=StrictLoader)  # noqa: S506 - StrictLoader is a SafeLoader
    except StrictYamlError:
        raise
    except yaml.YAMLError as error:
        raise StrictYamlError(f"not valid YAML: {error}") from error
