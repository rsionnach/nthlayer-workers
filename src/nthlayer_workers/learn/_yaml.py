"""Internal YAML helpers for the Learn → Spec workflow (jmy.6).

ruamel.yaml round-trip mode is used for the read+write path so
operator-authored comments survive deep-merge writes. Pure-function
helpers — no I/O — to enable focused unit-test coverage.
"""
from __future__ import annotations

from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


# Singleton sentinel for "path doesn't resolve in this document".
# Using a singleton object (not None) lets callers distinguish absent
# from a real None-valued leaf.
PATH_MISSING = object()


def get_yaml_round_trip() -> YAML:
    """Factory for a YAML() configured for comment-preserving round-trip.

    Configuration:
    - typ="rt": round-trip mode preserves comments, key order, and
      anchor/alias structure
    - preserve_quotes=True: literal scalar quoting survives writes
    - indent(mapping=2, sequence=4, offset=2): matches operator-authored
      OpenSRM v2 manifest style
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def resolve_path(doc: Any, dotted_path: str) -> Any:
    """Descend dotted_path through doc; return value or PATH_MISSING sentinel.

    Empty dotted_path returns the doc unchanged (root reference).
    Returns PATH_MISSING when any intermediate key is absent or the
    traversal would index into a non-mapping value.
    """
    if not dotted_path:
        return doc

    current = doc
    for key in dotted_path.split("."):
        if not isinstance(current, dict):
            return PATH_MISSING
        if key not in current:
            return PATH_MISSING
        current = current[key]
    return current


def apply_at_path(doc: Any, dotted_path: str, value: Any) -> None:
    """Write value at dotted_path; create missing intermediates as needed.

    Modifies doc in place. Comments on sibling keys and intermediate
    mappings are preserved (ruamel.yaml CommentedMap holds comments
    on the parent node, not on the keys themselves).

    Missing intermediate keys are created as CommentedMap instances
    so subsequent operations against those keys round-trip cleanly.

    Raises TypeError if a non-leaf path segment is already bound to
    a non-mapping value (we won't silently overwrite scalars with
    mappings — that's a structural change the engine doesn't produce).
    """
    if not dotted_path:
        raise ValueError("apply_at_path requires a non-empty dotted_path")

    keys = dotted_path.split(".")
    current = doc
    for key in keys[:-1]:
        if key not in current:
            current[key] = CommentedMap()
        if not isinstance(current[key], dict):
            raise TypeError(
                f"cannot descend into non-mapping at {key!r} "
                f"(found {type(current[key]).__name__})"
            )
        current = current[key]

    current[keys[-1]] = value


class _BoolScalar:
    """Opaque wrapper that keeps bool values distinct from numeric floats.

    Python's bool subclasses int, so True == 1 and False == 0 at the
    language level.  Wrapping in this sentinel lets callers assert that
    normalize_scalar(True) != normalize_scalar(1) without fighting the
    type hierarchy.
    """

    __slots__ = ("value",)

    def __init__(self, value: bool) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _BoolScalar) and self.value == other.value

    def __repr__(self) -> str:  # pragma: no cover
        return f"_BoolScalar({self.value!r})"

    def __hash__(self) -> int:
        return hash((self.__class__, self.value))


def normalize_scalar(value: Any) -> Any:
    """Normalise scalars for type-tolerant equality comparison (jmy.6 § 6.1).

    int(98) / float(98.0) / str("98") / str("98.0") all normalise to the
    same float IFF they round-trip cleanly. Used by classify_outcome to
    decide already_applied vs drift_detected against operator-authored
    manifests where YAML quoting is the operator's stylistic choice.

    Non-numeric scalars and non-scalar values pass through unchanged so
    equality comparison can do its own structural check. Booleans are
    NOT coerced to numeric (bool subclasses int in Python; we keep the
    distinction by wrapping them in _BoolScalar).
    """
    # Booleans first — bool is a subclass of int, so isinstance(True, int) is True.
    # Wrap in _BoolScalar so normalize_scalar(True) != normalize_scalar(1).
    if isinstance(value, bool):
        return _BoolScalar(value)

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value

    return value
