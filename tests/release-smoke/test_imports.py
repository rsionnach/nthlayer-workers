"""Smoke test: import every module under nthlayer_workers and verify
each symbol declared in its ``__all__`` actually resolves.

Runs against the *installed wheel* in a clean container — not the dev
source tree. Catches:
  - public symbol missing from a module's ``__all__`` after a rename
    (the bug class that motivated the smoke gate, e.g.
    ``write_decision_verdict`` removed but still referenced)
  - module file missing from the wheel (MANIFEST.in / package data gap)
  - dependency declared in dev but not in the wheel's runtime deps
  - circular import that only manifests post-install
"""
from __future__ import annotations

import importlib
import pkgutil

import pytest


PACKAGE_NAME = "nthlayer_workers"


def _walk_modules() -> list[str]:
    """Return every importable module under the package."""
    package = importlib.import_module(PACKAGE_NAME)
    paths = list(getattr(package, "__path__", []))
    if not paths:
        return [PACKAGE_NAME]

    names = [PACKAGE_NAME]
    for module_info in pkgutil.walk_packages(paths, prefix=f"{PACKAGE_NAME}."):
        # Skip legacy fastapi-dependent module from the deprecated
        # nthlayer-measure repo (consolidated 2026-04-26). The
        # project's own tests pytest.importorskip("fastapi") to
        # skip it; the module is superseded by core's HTTP API.
        # Tracked under opensrm-t5yr for production-side deletion.
        if module_info.name == f"{PACKAGE_NAME}.measure.api.server":
            continue
        names.append(module_info.name)
    return names


@pytest.fixture(scope="session")
def all_modules() -> list[str]:
    return _walk_modules()


def test_package_imports():
    importlib.import_module(PACKAGE_NAME)


@pytest.mark.parametrize("module_name", _walk_modules())
def test_each_module_imports(module_name: str):
    """Every module under the package must import cleanly post-install."""
    importlib.import_module(module_name)


def test_all_declared_symbols_resolve(all_modules: list[str]):
    """For modules that declare ``__all__``, every name in it must
    resolve via ``getattr`` — catching the stale-export bug class."""
    failures: list[str] = []
    for name in all_modules:
        module = importlib.import_module(name)
        declared = getattr(module, "__all__", None)
        if declared is None:
            continue
        for symbol in declared:
            try:
                getattr(module, symbol)
            except AttributeError as e:
                failures.append(f"{name}.__all__ → {symbol!r}: {e}")
    assert not failures, "Symbols in __all__ failed to resolve:\n  " + "\n  ".join(failures)
