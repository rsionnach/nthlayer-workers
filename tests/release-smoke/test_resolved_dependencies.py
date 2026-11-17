"""Smoke test: the version actually installed, against what the artifact asked for.

PORTED FROM nthlayer-core's tests/smoke/test_resolved_dependencies.py
[opensrm-p62o]. It is the second half of a pair; the first,
tests/test_dependency_declarations.py, arrived in opensrm-ir5m.

Runs in the normal suite AND in the release container. It is DECISIVE in the
container, because that is the only place in this pipeline where the published
ranges are the ones in force: the wheel is installed with
`pip install /dist/*.whl` and every dependency comes from PyPI, whereas
everywhere else `tool.uv.sources` substitutes the sibling checkout.

Reads installed metadata rather than pyproject.toml because that metadata is
what consumers actually get, in both environments. Its sibling
tests/test_dependency_declarations.py reads the source of truth instead, so the
two disagree exactly when a build is stale.

Why this file exists: resolving from the registry is not the same as checking
WHAT it resolved. The container's other smoke tests assert no version, so a
range that is wrong but satisfiable installs cleanly and stays green. That is
the shape of opensrm-p3bm in nthlayer-core, and this repo's container had the
same hole until this file.
"""
from __future__ import annotations

from importlib.metadata import distribution, version

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

DISTRIBUTION = "nthlayer-workers"

# Siblings are dependencies under this prefix plus the front door itself:
# canonicalize_name("nthlayer") is "nthlayer" and does NOT start with
# "nthlayer-", so prefix matching alone would miss a real member.
SIBLING_PREFIX = "nthlayer-"
FRONT_DOOR = "nthlayer"

# Each sibling mapped to the MAJOR this repo is developed against. Stated
# independently rather than derived from the declared range: deriving it would
# make the test agree with whatever the range says, including a wrong one. Only
# the major, so ordinary minor and patch upgrades flow through untouched.
#
# This is NOT the roster of what gets checked. The roster is discovered from the
# artifact's own metadata, and test_every_discovered_sibling_declares_a_major
# fails if a sibling appears in the metadata without an entry here, so adding a
# dependency forces someone to state its expected major rather than quietly
# leaving it unguarded.
EXPECTED_MAJORS = {"nthlayer-common": 5}


def _unconditional_requirements() -> dict[str, Requirement]:
    """Canonical-name -> requirement for every unconditional dep of the artifact.

    Names are canonicalised: a requirement spelled `nthlayer_common` reports
    that verbatim and would otherwise read as a different package. An extra
    (this repo declares `nthlayer-common[otel]`) is not part of the name.
    """
    reqs = (Requirement(r) for r in (distribution(DISTRIBUTION).requires or []))
    return {canonicalize_name(r.name): r for r in reqs if not r.marker}


def _sibling_requirements() -> dict[str, Requirement]:
    """The unconditional ranges the built wheel shipped, per its own metadata.

    Marker-guarded requirements are dropped: they apply only on some
    interpreters or extras, so "the installed version satisfies this" is not a
    claim that holds environment-independently. Every sibling in
    EXPECTED_MAJORS is unconditional, and the `name in declared` assertion below
    fails loudly if one ever stops being.
    """
    return {
        n: r
        for n, r in _unconditional_requirements().items()
        if n == FRONT_DOOR or n.startswith(SIBLING_PREFIX)
    }


# Evaluated at collection time, from the installed artifact's metadata.
# Non-vacuity floor: an empty parametrise list SKIPS and exits 0, and a guard
# against silent drift must not be able to go silent itself. At module scope,
# so an empty roster is a collection ERROR and the container's pytest exits
# non-zero. If it fires, the artifact's metadata lost its sibling requirements:
# a packaging fault, not a version fault. (nthlayer-core's copy uses a test
# function for this; the gate's round-0 check reads the module-scope form.)
SIBLINGS = sorted(_sibling_requirements())
assert SIBLINGS, (
    f"{DISTRIBUTION} declares no unconditional dependency under "
    f"'{SIBLING_PREFIX}' or named '{FRONT_DOOR}'; every version check here "
    f"would silently skip"
)


@pytest.mark.parametrize("name", SIBLINGS)
def test_every_discovered_sibling_declares_a_major(name):
    """Adding a sibling dependency must force a decision about its major.

    Without this, EXPECTED_MAJORS could fall behind the dependency list and a
    newly added sibling would be unguarded by construction: the same drift
    this file exists to catch, one coordinate over.
    """
    assert name in EXPECTED_MAJORS, (
        f"{name} is an unconditional dependency of {DISTRIBUTION} but has no "
        f"entry in EXPECTED_MAJORS, so nothing checks which major resolves"
    )


@pytest.mark.parametrize("name", SIBLINGS)
def test_installed_version_satisfies_the_artifact_metadata(name):
    """Catches a stale build.

    Editing pyproject.toml and re-locking without re-syncing leaves the
    installed dist-info carrying the old range.
    """
    declared = _sibling_requirements()
    assert name in declared, (
        f"{DISTRIBUTION} does not declare {name} at all; check that the wheel "
        f"metadata survived the build"
    )
    assert version(name) in declared[name].specifier


@pytest.mark.parametrize("name", SIBLINGS)
def test_installed_sibling_is_the_major_this_code_was_written_against(name):
    """The assertion the container gate was missing.

    Decisive in the release container, where deps come from PyPI: a successful
    `pip install nthlayer_workers-*.whl` says only that the declared range is
    satisfiable, not that it is right. Pinning the expected MAJOR here, and
    nothing narrower, is what distinguishes "a version was installed" from
    "the version this code was developed and tested against was installed",
    while still allowing ordinary minor and patch upgrades to flow through
    without touching this test.
    """
    installed = version(name)
    # Version().major, not int(split(".")[0]): an epoch version like 1!2.0.0
    # makes the naive split yield "1!2" and raise ValueError, so the gate would
    # die on a traceback instead of the assertion message below.
    major = Version(installed).major
    expected = EXPECTED_MAJORS[name]
    assert major == expected, (
        f"{name} resolved to {installed} (major {major}), but this release is "
        f"built and tested against major {expected}. Either the declared range "
        f"in pyproject.toml admits a major nothing here has run, or "
        f"EXPECTED_MAJORS was not updated with a deliberate major upgrade."
    )
