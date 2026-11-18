"""Operator-facing CLI output shared across worker modules."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from nthlayer_common.manifest import SuffixCollision


def warn_parse_failures(parse_failures: int, specs_dir: str | Path) -> None:
    """Tell the operator when the SLOs that follow cover only part of a directory.

    Surfaced on stderr rather than only logged: whoever reads SLO output is
    the one who needs to know the view is partial, and a warning in a log
    they are not tailing does not reach them (opensrm-3470).

    Takes the count, not a ``LoadedSpecs`` — observe and measure each have
    their own, carrying differently-shaped SLOs under differently-named
    fields (``service_slos`` vs ``slos``). The only thing they share is this
    number, so that is the whole parameter (opensrm-fxln).
    """
    if parse_failures:
        print(
            f"Warning: {parse_failures} manifest(s) in {specs_dir} "
            f"failed to parse and were not evaluated",
            file=sys.stderr,
        )


def suffix_collision_lines(collisions: Sequence[SuffixCollision]) -> list[str]:
    """One operator-facing line per file a directory scan set aside.

    Each names the file that was NOT read first, then the one read in its
    place. Kept apart from the printing so what is reported can be checked
    as data.
    """
    return [
        f"Warning: {c.dropped} was not read: {c.kept.name} has the same name "
        f"and was read instead. If they declare different services, the one "
        f"in {c.dropped.name} is not evaluated"
        for c in collisions
    ]


def warn_suffix_collisions(collisions: Sequence[SuffixCollision]) -> None:
    """Tell the operator which manifest files a scan set aside, and why.

    ``svc.yaml`` beside ``svc.yml``: nthlayer-common reads the ``.yaml`` and
    sets the other aside. Nothing checks that the two declare the same
    service, so the SLOs that follow may be missing one. On stderr for the
    reason ``warn_parse_failures`` is, and from returned data rather than a
    Python warning, so it is said on every run (opensrm-j9wq).
    """
    for line in suffix_collision_lines(collisions):
        print(line, file=sys.stderr)
