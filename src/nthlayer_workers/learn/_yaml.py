"""Internal YAML helpers for the Learn → Spec workflow (jmy.6).

ruamel.yaml round-trip mode is used for the read+write path so
operator-authored comments survive deep-merge writes. Pure-function
helpers — no I/O — to enable focused unit-test coverage.
"""
from __future__ import annotations

from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from nthlayer_workers.learn.recommendations import OutcomeKind, Recommendation


# Singleton sentinel for "path doesn't resolve in this document".
# Using a singleton object (not None) lets callers distinguish absent
# from a real None-valued leaf.
PATH_MISSING = object()

# Suffix on a Recommendation.field that switches apply + classify into
# list-append mode (opensrm-jmy.21). Centralised so the three call sites
# — _apply.resolve_path strip, apply_at_path dispatch, classify_outcome
# dispatch — stay in sync. Search this symbol to find every site that
# implements the append convention.
LIST_APPEND_SIGIL = "[+]"


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

    The ``[+]`` sigil on the trailing segment (e.g.
    ``spec.dependencies[+]``) requests list-append at the un-sigil'd
    path. The path must point to a list, OR be absent (in which case
    it is created as an empty list and then appended). ``value`` is a
    single item to append. TypeError if the existing leaf is not a list.

    Both branches (set-path and ``[+]`` append) share the structural-
    error contract: ``doc`` must be a mapping, intermediates must be
    mappings, and a non-empty dotted_path is required.
    """
    if not dotted_path:
        raise ValueError("apply_at_path requires a non-empty dotted_path")

    # An empty / null manifest (e.g. a file with `---` only) loads to
    # None. Guard up-front so both branches below get a clear structural
    # TypeError instead of the cryptic `'NoneType' is not iterable`.
    if not isinstance(doc, dict):
        raise TypeError(
            f"cannot apply path to non-mapping document "
            f"(got {type(doc).__name__})"
        )

    if dotted_path.endswith(LIST_APPEND_SIGIL):
        # List-append sigil (opensrm-jmy.21). Walk to the leaf's parent,
        # creating intermediate mappings as needed (same convention as
        # the set-path branch below), then append to / create the list.
        base_path = dotted_path[: -len(LIST_APPEND_SIGIL)]
        if not base_path:
            raise ValueError("apply_at_path '[+]' sigil requires a non-empty base path")
        keys = base_path.split(".")
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
        leaf = keys[-1]
        if leaf not in current:
            current[leaf] = []
        if not isinstance(current[leaf], list):
            raise TypeError(
                f"cannot append to non-list at {leaf!r} "
                f"(found {type(current[leaf]).__name__})"
            )
        current[leaf].append(value)
        return

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


def classify_outcome(manifest_value: Any, rec: Recommendation) -> OutcomeKind:
    """Two-table state machine from jmy.6 design § 5.

    For recommendations WITH current_value (modifying existing):
      manifest path missing       → target_path_missing
      manifest path = proposed    → already_applied
      manifest path = current     → apply_clean
      manifest path = other       → drift_detected

    For recommendations WITHOUT current_value (adding new):
      manifest path missing       → apply_clean (create)
      manifest path = proposed    → already_applied
      manifest path = other       → drift_detected

    For list-append recommendations (rec.field ending in ``[+]``,
    opensrm-jmy.21): manifest_value is the list at the un-sigil'd
    path (caller responsible for stripping the sigil before
    resolve_path).
      manifest path missing       → apply_clean (create + append)
      manifest is a list and contains proposed → already_applied
      manifest is a list, no match → apply_clean (append)
      manifest is not a list       → drift_detected

    Type-tolerant scalar comparison via normalize_scalar; structural
    (dict/list) comparison is exact.
    """
    if rec.field and rec.field.endswith(LIST_APPEND_SIGIL):
        # List-append table (opensrm-jmy.21 add_dependency). current_value
        # is always None for append recs (the path either doesn't exist
        # yet or holds a list of items; "current" is not a scalar).
        if manifest_value is PATH_MISSING:
            return OutcomeKind.APPLY_CLEAN
        if not isinstance(manifest_value, list):
            # Operator turned what should be a list into a different
            # shape — we don't silently overwrite that.
            return OutcomeKind.DRIFT_DETECTED
        if _list_contains_proposed(manifest_value, rec.proposed_value):
            return OutcomeKind.ALREADY_APPLIED
        return OutcomeKind.APPLY_CLEAN

    proposed_norm = _normalize_for_compare(rec.proposed_value)

    if rec.current_value is None:
        # Adding-new table
        if manifest_value is PATH_MISSING:
            return OutcomeKind.APPLY_CLEAN
        if _normalize_for_compare(manifest_value) == proposed_norm:
            return OutcomeKind.ALREADY_APPLIED
        return OutcomeKind.DRIFT_DETECTED

    # Modifying-existing table
    if manifest_value is PATH_MISSING:
        return OutcomeKind.TARGET_PATH_MISSING

    current_norm = _normalize_for_compare(rec.current_value)
    manifest_norm = _normalize_for_compare(manifest_value)

    if manifest_norm == proposed_norm:
        return OutcomeKind.ALREADY_APPLIED
    if manifest_norm == current_norm:
        return OutcomeKind.APPLY_CLEAN
    return OutcomeKind.DRIFT_DETECTED


def _normalize_for_compare(value: Any) -> Any:
    """Normalise scalars; pass non-scalars through. For dict/list, the
    caller's == does structural comparison."""
    if isinstance(value, (dict, list)):
        return value
    return normalize_scalar(value)


def _list_contains_proposed(manifest_list: list, proposed: Any) -> bool:
    """Membership check for the ``[+]`` append-rec already_applied branch.

    If ``proposed`` is a dict carrying a ``"name"`` key, match any item
    in ``manifest_list`` that is also a dict with the same ``"name"``
    value — this is the OpenSRM convention for keyed list entries (e.g.
    Dependency objects). Otherwise fall back to deep ``==`` equality
    against each item.
    """
    if isinstance(proposed, dict) and "name" in proposed:
        proposed_name = proposed["name"]
        for item in manifest_list:
            if isinstance(item, dict) and item.get("name") == proposed_name:
                return True
        return False
    for item in manifest_list:
        if item == proposed:
            return True
    return False
