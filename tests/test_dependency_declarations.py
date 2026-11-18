"""The declared dependency ranges must admit the versions actually in use.

Reads pyproject.toml, the source of truth, and fails locally the moment a
sibling outgrows the declared range.

PORTED FROM nthlayer-core [opensrm-ir5m]. This package went without it until
nthlayer-common 3.0.0 shipped, and the gap was measured rather than supposed:
reverting this repo's declared range to `>=2.1.2,<3.0.0` — false, since 3.0.0
is installed and under test — left the full 1958-test suite GREEN, while the
identical mutation went red in all four siblings that had this file. workers is
the repo 3.0.0 actually broke, so it was the worst one to be missing it.

The OTHER half of nthlayer-core's pair is
tests/release-smoke/test_resolved_dependencies.py [opensrm-p62o]: it reads the
BUILT artifact's own metadata and is decisive in the release container, where
deps come from PyPI rather than the sibling checkout. This file does not cover
the container case; that one does.

The rule both enforce is this repo's CLAUDE.md hard rule "A declared dependency
range must admit exactly the versions tested" (cited by title: rule numbers
move; test_the_rule_this_file_cites_is_in_claude_md checks it is there). It
came from opensrm-p3bm in nthlayer-core. The short version: `tool.uv.sources`
points nthlayer-common at
the sibling checkout, a path source REPLACES registry resolution rather than
being filtered by the version specifier, and nothing warned that the declared
range and the tested version had diverged.

The guarded set is DISCOVERED from project.dependencies, never hand-listed. A
hand-maintained roster can fall behind pyproject, and a roster that empties
turns every parametrised assertion below into skips with exit 0 — the same
class of bug this file exists to catch, one coordinate over.
The module-scope `assert SIBLINGS` is the non-vacuity floor.
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


# Evaluated at collection time. Non-vacuity floor for every parametrised test
# here: an empty parametrise list SKIPS and exits 0, so without the assert the
# file could go quiet instead of red. At module scope, so an empty roster is a
# collection ERROR rather than one failing test beside a row of skips
# [opensrm-p62o]. It replaced test_at_least_one_sibling_is_guarded, which a
# module-scope assert makes unreachable.
SIBLINGS = sorted(_siblings())
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
def test_ceiling_excludes_the_next_major(name):
    """An upper bound that EXISTS is not the same as one that bounds.

    test_declared_range_has_an_upper_bound accepts any bounding operator, so
    `<4.0.0` and `<9.0.0` pass every other check in this file identically while
    declaring support for five majors nothing has run. (EXPECTED_MAJORS in
    tests/release-smoke/test_resolved_dependencies.py does not close this
    either: it pins the INSTALLED major.) Measured against the live range:

        next-major probe 4.0.0 vs `<4.0.0` -> excluded (correct)
        next-major probe 4.0.0 vs `<9.0.0` -> ADMITTED (the hole)

    Pure specifier arithmetic, so no unreleased version need exist — the same
    technique as UNBOUNDED_PROBE above, and it inherits that constant's
    EPOCH-BLINDNESS, which is stated here rather than left implied: for an
    installed `1!3.0.0` the probe is `4.0.0`, which even a completely unbounded
    `>=1!3.0.0` already excludes, so this assertion would pass vacuously. Left
    alone for the same reason UNBOUNDED_PROBE is — no sibling has ever used an
    epoch — and test_declared_range_has_an_upper_bound still catches an outright
    missing ceiling in that case. This is the dependency-range rule's "not a
    future major" (CLAUDE.md; see the module docstring) stated as an
    assertion rather than prose [opensrm-ir5m, from the opensrm-p62o review].

    It deliberately does NOT assert anything about the range's upper edge
    WITHIN the current major: `<3.5.0` would pass here. That edge is
    test_declared_ceiling_is_exactly_the_next_major's.
    """
    specifier = _siblings()[name].specifier
    next_major = Version(f"{Version(version(name)).major + 1}.0.0")

    assert next_major not in specifier, (
        f"'{name}{specifier}' admits {next_major}, a major this repo has never "
        f"run. A ceiling that does not exclude the next major declares "
        f"compatibility untested — the shape of opensrm-p3bm, one major up."
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


@pytest.mark.parametrize("name", SIBLINGS)
def test_declared_ceiling_is_exactly_the_next_major(name):
    """The ceiling must exclude the first release of the NEXT major, and nothing more.

    test_declared_range_has_an_upper_bound only asks that SOME bound exists, so
    `<N+2.0.0` while N.x is under test passes it (measured: suite stays green) and
    admits all of major N+1, which nothing here has run. Mirror image of the floor
    test: the floor admits nothing below the tested version, this admits nothing
    above its major. Like the floor, it must be bumped deliberately when the
    sibling's major advances.
    """
    specifier = _siblings()[name].specifier
    installed = Version(version(name))

    ceilings = [s.version for s in specifier if s.operator == "<"]
    assert len(ceilings) == 1, (
        f"'{name}{specifier}' declares {len(ceilings)} '<' bounds; this guard "
        f"reads exactly one"
    )
    expected = Version(f"{installed.major + 1}.0.0")
    assert Version(ceilings[0]) == expected, (
        f"{name} ceiling is declared '<{ceilings[0]}' but the installed version "
        f"is {installed}, so the ceiling must be '<{expected}'. A wider ceiling "
        f"publishes a major nothing here has run."
    )


# The title of the CLAUDE.md hard rule this file and pyproject.toml's comment
# cite. By title, because the number is not stable: the same rule is 10, 11 and
# 16 in sibling repos, and inserting a rule above it here would move it.
CITED_RULE = "**A declared dependency range must admit exactly the versions tested.**"


def test_the_rule_this_file_cites_is_in_claude_md():
    """A pointer nobody checks rots. This file and pyproject.toml used to cite
    "nthlayer-core CLAUDE.md hard rule 10": another repo's file, by a number
    that differs in every repo, with nothing here to notice it moving
    [opensrm-v929]. The citation is now to this repo's own CLAUDE.md, and
    this is the check: the rule is there, once, as a numbered hard rule."""
    claude_md = (PYPROJECT.parent / "CLAUDE.md").read_text()

    numbered = [
        line for line in claude_md.splitlines()
        if line.split(". ", 1)[0].isdigit() and line.split(". ", 1)[-1].startswith(CITED_RULE)
    ]
    assert len(numbered) == 1, (
        f"CLAUDE.md should carry exactly one numbered hard rule titled "
        f"{CITED_RULE}; found {len(numbered)}. pyproject.toml and this "
        f"file cite it by that title."
    )


def test_pyproject_cites_the_rule_by_the_same_title():
    """The other citation, in the comment above the nthlayer-common range.
    The comment wraps, so its `#` markers and line breaks are folded away
    before looking for the title."""
    folded = " ".join(PYPROJECT.read_text().replace("#", " ").split())

    assert CITED_RULE.strip("*.") in folded


def test_uv_lock_records_this_projects_version():
    """uv.lock's own [[package]] entry must carry pyproject's version.

    release-please bumps pyproject.toml; a toml extra-files updater in
    release-please-config.json bumps this entry in the same release PR. That
    updater only WARNS when its jsonpath matches nothing (a renamed package, a
    lock-format change), so without this test the lock would silently go stale
    again and every `uv sync` after a release would dirty it [opensrm-g35i].
    Release PRs run CI, so a skipped bump fails here, on the release PR.
    """
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((root / "uv.lock").read_text())
    entries = [p for p in lock.get("package", []) if p.get("name") == project["name"]]
    assert len(entries) == 1, (
        f"uv.lock has {len(entries)} [[package]] entries named {project['name']!r}"
    )
    assert entries[0].get("version") == project["version"], (
        f"uv.lock records {project['name']} {entries[0].get('version')}, pyproject says "
        f"{project['version']}: run `uv lock` (and check release-please-config.json extra-files)"
    )


def test_common_is_declared_with_the_otel_extra():
    """nthlayer-common's ``otel`` extra carries the OTel SDK and OTLP log
    exporter. Without it, ``init_telemetry`` finds an endpoint but no SDK, and
    every event workers emits is dropped (opensrm-quqo)."""
    assert "otel" in _siblings()["nthlayer-common"].extras
