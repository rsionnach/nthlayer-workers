"""The declared dependency ranges must admit the versions actually in use.

Reads pyproject.toml, the source of truth, and fails locally the moment a
sibling outgrows the declared range.

PORTED FROM nthlayer-core [opensrm-ir5m]. This package went without it until
nthlayer-common 3.0.0 shipped, and the gap was measured rather than supposed:
reverting this repo's declared range to `>=2.1.2,<3.0.0` — false, since 3.0.0
is installed and under test — left the full 1958-test suite GREEN, while the
identical mutation went red in all four siblings that had this file. workers is
the repo 3.0.0 actually broke, so it was the worst one to be missing it.

The OTHER half of nthlayer-core's pair is still absent here: it has a
tests/smoke/test_resolved_dependencies.py that reads the BUILT artifact's own
metadata and is decisive in the release container, where deps come from PyPI
rather than the sibling checkout. This repo's tests/release-smoke/ holds only
test_cli.py and test_imports.py. That remains opensrm-p62o — do not read this
file's presence as covering the container case.

The full account of why both exist is nthlayer-core CLAUDE.md hard rule 10
[opensrm-p3bm]. The short version: `tool.uv.sources` points nthlayer-common at
the sibling checkout, a path source REPLACES registry resolution rather than
being filtered by the version specifier, and nothing warned that the declared
range and the tested version had diverged.

The guarded set is DISCOVERED from project.dependencies, never hand-listed. A
hand-maintained roster can fall behind pyproject, and a roster that empties
turns every parametrised assertion below into `1 skipped, exit 0` — the same
class of bug this file exists to catch, one coordinate over.
test_at_least_one_sibling_is_guarded is the non-vacuity floor.
"""
from __future__ import annotations

import tomllib
from collections import Counter
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# Ecosystem siblings are the dependencies under this prefix, plus the front door
# itself: canonicalize_name("nthlayer") is "nthlayer", which does NOT start with
# "nthlayer-", so prefix matching alone would miss a real member. Discovery
# rather than a hand-listed roster means a newly added sibling is guarded the day
# it is declared. (`opensrm` is deliberately absent — it ships no Python package,
# so it can never be a dependency here.)
SIBLING_PREFIX = "nthlayer-"
FRONT_DOOR = "nthlayer"


def _is_sibling(canonical_name: str) -> bool:
    return canonical_name == FRONT_DOOR or canonical_name.startswith(SIBLING_PREFIX)

# Operators that bound a range from above. "~=" is here on measurement, not on
# reasoning: packaging exposes a compatible-release specifier as the single
# operator "~=" and never decomposes it into ">=" plus "<", so a predicate
# looking only for "<" rejects `~=2.1` — which does exclude 3.0.0 and IS
# bounded. test_bounding_operators_match_packaging_behaviour pins that, because
# an earlier revision of this file asserted the decomposition in a comment and
# was wrong.
BOUNDING_OPERATORS = ("<", "<=", "==", "===", "~=")

# A version no realistic range admits. Lets the cross-check below ask a
# specifier what it actually does about the far future, instead of hardcoding
# one major and false-failing any case that lives at another.
#
# Epoch-blind by decision: "9999.0.0" is below any 1!x version, so a bounded
# epoch ceiling such as `<1!3.0` would false-fail the cross-check. Left alone
# because no sibling has ever used an epoch and there is no honest way to
# exercise the branch that would handle it.
UNBOUNDED_PROBE = "9999.0.0"


def _declared() -> list[Requirement]:
    data = tomllib.loads(PYPROJECT.read_text())
    return [Requirement(d) for d in data["project"]["dependencies"]]


def _siblings() -> dict[str, Requirement]:
    """Canonical-name -> requirement, for every declared ecosystem sibling.

    Names are canonicalised on the way in: `Requirement("nthlayer_common>=2")`
    reports its name verbatim as `nthlayer_common`, so an underscore spelling
    would otherwise read as a different, undeclared package.
    """
    return {
        canonicalize_name(r.name): r
        for r in _declared()
        if _is_sibling(canonicalize_name(r.name))
    }


# Evaluated at collection time. If it is ever empty the parametrised tests below
# skip rather than fail, which test_at_least_one_sibling_is_guarded prevents.
SIBLINGS = sorted(_siblings())


def test_at_least_one_sibling_is_guarded():
    """Non-vacuity floor for every parametrised test here.

    An empty parametrise list reports `1 skipped` and exits 0 (measured), so
    without this the file could go quiet instead of red.
    """
    assert SIBLINGS, (
        f"no dependency under '{SIBLING_PREFIX}' or named '{FRONT_DOOR}' "
        f"found in {PYPROJECT.name}; "
        f"every check in this file would silently skip"
    )


def test_no_sibling_is_declared_twice():
    """A duplicate silently last-wins, so the range checked need not be shipped.

    Measured: `[nthlayer-common>=2.1.2,<3.0.0, nthlayer-common<2.0.0]` keyed by
    name collapses to `<2.0.0`.
    """
    names = [canonicalize_name(r.name) for r in _declared()]
    duplicated = sorted(n for n, count in Counter(names).items() if count > 1)
    assert not duplicated, (
        f"declared more than once in {PYPROJECT.name}: {duplicated}. The later "
        f"entry wins silently, so the range checked here need not be the range "
        f"shipped."
    )


@pytest.mark.parametrize("name", SIBLINGS)
def test_declared_range_admits_the_installed_sibling(name):
    """The range we publish must admit the build we test against.

    Fails the moment a sibling outgrows the ceiling, in the developer's own
    suite rather than at some consumer's install.
    """
    installed = version(name)
    specifier = _siblings()[name].specifier

    assert installed in specifier, (
        f"{name} {installed} is installed and tested against, but "
        f"project.dependencies declares '{name}{specifier}', which excludes "
        f"it. Publishing this means consumers resolve a version no test here "
        f"has ever run. Widen the declared range, or pin the sibling back."
    )


@pytest.mark.parametrize("name", SIBLINGS)
def test_declared_floor_is_the_version_under_test(name):
    """The floor must admit nothing this repo has never run.

    test_declared_range_admits_the_installed_sibling checks the range does not
    EXCLUDE the tested version; this checks it does not admit versions BELOW it.
    Both are needed. Measured: `>=1.5.0,<3.0.0` passes every other check in this
    file — the ceiling is present, 2.1.2 is inside the range, and the container
    resolves the newest so the major assertion sees 2 — while letting a consumer
    resolve this package against common 1.7.0. That is opensrm-p3bm restored,
    with the guards reading as though it were covered.

    The installed version is the only version under test, so it is the only
    floor that admits nothing untested.

    Consequence, intended: the floor must be deliberately bumped when the
    sibling advances. That friction is the point — the declaration drifted
    silently for four minor releases precisely because nothing demanded the
    edit. Loosen this only on purpose, and record why. In particular do NOT
    relax it to a major-or-minor comparison: `>=2.1.0` while 2.1.2 is tested is
    the same hole as `>=1.5.0`, one coordinate smaller.

    Where the red actually appears, which is not where you would guess:
    .github/workflows/test.yml checks out rsionnach/nthlayer-common with no `ref`,
    so CI builds against that repo's floating main. This test therefore goes red
    when common's version-bump commit lands on ITS main — before any PyPI
    release, triggered by a commit in another repository. If you are debugging
    that failure from inside nthlayer-workers, the change you are looking for
    is not here.
    """
    specifier = _siblings()[name].specifier
    installed = version(name)

    floors = [s.version for s in specifier if s.operator == ">="]
    assert len(floors) == 1, (
        f"'{name}{specifier}' declares {len(floors)} '>=' bounds; this guard "
        f"reads exactly one. A pin or compatible-release form needs its own "
        f"check rather than passing silently."
    )
    assert Version(floors[0]) == Version(installed), (
        f"{name} floor is declared '>={floors[0]}' but the version installed "
        f"and tested against is {installed}. Everything between them is "
        f"published as supported and has never been run here — the shape of "
        f"opensrm-p3bm. Confirm nthlayer-common's CHANGELOG carries no break "
        f"for this repo, THEN bump the floor to {installed} — or pin the "
        f"sibling back to {floors[0]}. Widening the published range to make "
        f"this pass is how the bug got here."
    )


@pytest.mark.parametrize("name", SIBLINGS)
def test_declared_range_has_an_upper_bound(name):
    """A missing ceiling is how core 1.0.0 became the resolver's escape hatch.

    Without an upper bound, a future major of the sibling is silently
    considered compatible, and this package becomes the one the resolver
    reaches for when it needs to satisfy something incompatible — which is
    exactly why `pip install nthlayer-workers==2.0.0 nthlayer-core` yielded
    core 1.0.0 rather than an error.
    """
    specifier = _siblings()[name].specifier
    assert any(s.operator in BOUNDING_OPERATORS for s in specifier), (
        f"'{name}{specifier}' has no upper bound, so every future major of "
        f"{name} is declared compatible without anything testing it"
    )


@pytest.mark.parametrize(
    ("spec", "bounded"),
    [
        (">=2.1.2,<3.0.0", True),
        (">=2.1.2,<=2.9.9", True),
        ("==2.1.2", True),
        ("===2.1.2", True),
        ("==2.*", True),
        ("~=2.1", True),
        ("~=2.1.2", True),
        (">=4.0", False),
        (">=2.1.2", False),
        (">2.0", False),
        ("!=2.0.0", False),
        ("", False),
    ],
)
def test_bounding_operators_match_packaging_behaviour(spec, bounded):
    """BOUNDING_OPERATORS must agree with whether a range really bounds above.

    Derived from what `packaging` does, not from what the operators look like
    they should do. The cross-check asks each specifier about UNBOUNDED_PROBE
    rather than a hardcoded next-major, so a case at any major — see `>=4.0` —
    is judged on its own behaviour. Fails on drift in either direction: an
    operator missing from the tuple that false-fails a bounded range, or a
    spurious one that lets an unbounded range through.
    """
    specifier = SpecifierSet(spec)
    predicate = any(s.operator in BOUNDING_OPERATORS for s in specifier)

    assert predicate is bounded
    assert (UNBOUNDED_PROBE in specifier) is not bounded
