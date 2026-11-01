"""provider_for_model: the gen_ai.provider.name workers reports (opensrm-ju8r J4).

OpenSRM Addendum A1 §5 P4 names the provider by gen_ai.provider.name, whose
upstream well-known values include "anthropic" and "openai". Workers used to
send the sentinel "unknown" whenever a model string carried no
"provider/" prefix, so a bare Claude model was reported as an unknown provider.
"""

import pytest

from nthlayer_workers.llm_provider import provider_for_model


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        # An explicit provider/ prefix is authoritative, whatever follows.
        ("anthropic/claude-sonnet-4-20250514", "anthropic"),
        ("openai/gpt-4o", "openai"),
        ("ollama/claude-lookalike", "ollama"),
        # An empty prefix names nothing.
        ("/claude-sonnet", "unknown"),
        # Bare model names from the two families whose provider is unambiguous.
        ("claude-sonnet-4-20250514", "anthropic"),
        ("gpt-4o", "openai"),
        ("o1-mini", "openai"),
        ("o3", "openai"),
        # Nothing to go on: say so rather than guess.
        ("llama3", "unknown"),
        ("default", "unknown"),
        ("", "unknown"),
    ],
)
def test_provider_for_model(model: str, provider: str) -> None:
    assert provider_for_model(model) == provider
