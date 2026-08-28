"""Operator-facing CLI output shared across worker modules."""

from __future__ import annotations

import sys


def warn_parse_failures(parse_failures: int, specs_dir: object) -> None:
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
