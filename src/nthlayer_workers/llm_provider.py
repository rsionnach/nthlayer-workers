"""The provider an LLM call is reported against (opensrm-ju8r, ruling J4).

OpenSRM Addendum A1 §5 P4 identifies the provider by ``gen_ai.provider.name``.
Workers knows only the model string it called, so it derives the provider
from that: an explicit ``provider/`` prefix wins; otherwise only the model
families whose provider is unambiguous are mapped. Anything else is reported
as ``"unknown"`` rather than guessed (nthlayer-common's ``_guess_provider``
defaults to ``openai``, which would be a confident wrong answer here).
"""

from __future__ import annotations

UNKNOWN_PROVIDER = "unknown"

# Bare-model prefixes whose provider is not in doubt.
_FAMILY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
)


def provider_for_model(model: str) -> str:
    """Return the ``gen_ai.provider.name`` value for *model*."""
    if "/" in model:
        prefix = model.split("/", 1)[0]
        return prefix or UNKNOWN_PROVIDER
    for prefix, provider in _FAMILY_PREFIXES:
        if model.startswith(prefix):
            return provider
    return UNKNOWN_PROVIDER
