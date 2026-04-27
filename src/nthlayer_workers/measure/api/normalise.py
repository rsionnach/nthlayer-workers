"""Input normalisation — converts simplified external format to internal evaluation format.

This is where the 'adapter moves inside nthlayer-measure' principle lives.
External clients send minimal JSON; this layer fills defaults and validates.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvaluationRequest:
    """Normalised evaluation request — the internal representation of an external POST."""

    agent_name: str
    task_id: str
    output: str
    context: str | None = None
    service: str | None = None
    environment: str = "production"
    callback_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def normalise_input(body: dict) -> EvaluationRequest:
    """Convert simplified external JSON to EvaluationRequest.

    Required: agent, output
    Optional with defaults: task_id (uuid4), environment ("production"),
    context (None), service (None), callback_url (None), metadata ({})

    Raises ValueError if required fields are missing.
    """
    if not body.get("agent", "").strip():
        raise ValueError("Missing or empty required field: 'agent'")
    if not body.get("output", "").strip():
        raise ValueError("Missing or empty required field: 'output'")

    return EvaluationRequest(
        agent_name=body["agent"],
        task_id=body.get("task_id", str(uuid.uuid4())),
        output=body["output"],
        context=body.get("context"),
        service=body.get("service"),
        environment=body.get("environment", "production"),
        callback_url=body.get("callback_url"),
        metadata=body.get("metadata", {}),
    )
