"""Shared precedence rule for resolving the incident's trigger service.

opensrm-dpws: used by both retrospective code paths (CLI
``build_retrospective`` and worker ``LearnRetrospectiveModule``) so
the precedence rule has a single home — the correlator's grouping
is always preferred over the incident's primary-service field.
"""
from __future__ import annotations


def resolve_trigger_service(
    correlation_candidates: list[str | None],
    fallback: str | None,
) -> str | None:
    """Return the first non-empty string from ``correlation_candidates``,
    else ``fallback`` if non-empty, else ``None``.

    Correlation-first reflects the correlator's grouping IS the trigger
    context (its ``subject.service`` is literally "the service the
    correlator anchored a session window on"). The ``fallback`` is the
    incident verdict's ``subject.service`` (CLI) or the snapshot's
    top-level ``service`` field (worker) — both name the primary service
    of the incident when no correlation context exists.

    Returning ``None`` is the signal to OMIT the trigger_service key
    from the retrospective payload entirely (back-compat with jmy.21's
    ``log.debug + []`` no-rec path in ``_add_dependency_recommendations``).

    Whitespace-only strings (e.g. ``" "``) are normalised to ``None`` —
    they cannot be a valid service identity, and treating them as
    truthy would let pathological producer data poison the
    ``declared_map.get(trigger)`` lookup downstream (no match, but
    silent).
    """
    for candidate in correlation_candidates:
        if candidate and candidate.strip():
            return candidate.strip()
    if fallback and fallback.strip():
        return fallback.strip()
    return None
