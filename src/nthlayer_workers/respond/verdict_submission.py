"""Worker-mode verdict submission to core (P3-E.1).

Wraps a Verdict dataclass in a CloudEvents v1.0 envelope and submits it to
core via CoreAPIClient. Submission failure is logged and metered but does
not crash the cycle — the Verdict object is already constructed locally
and lineage is intact in IncidentContext.verdict_chain, so the next agent's
verdict will still chain correctly.
"""
from __future__ import annotations

import structlog
from nthlayer_common.api_client import CoreAPIClient
from nthlayer_common.cloudevents import wrap_verdict
from nthlayer_common.metrics import errors_total
from nthlayer_common.verdicts import Verdict, to_dict

logger = structlog.get_logger(__name__)


async def submit_verdict_to_core(
    client: CoreAPIClient,
    verdict: Verdict,
    *,
    deployment_id: str | None = None,
) -> bool:
    """Submit a verdict to core via the CloudEvents-wrapped /verdicts endpoint.

    Returns True on success, False on submission failure. The caller is
    responsible for deciding whether to chain subsequent verdicts to a
    failed one — current respond callers (``_emit_verdict``) skip appending
    on failure so ``verdict_chain[-1]`` always references a verdict that
    actually exists in core. This keeps the in-memory chain consistent with
    the core-side lineage graph; otherwise consumers walking ancestry via
    ``parent_ids`` would dead-end on dangling references.

    Failures are logged and counted; this function never raises. Operator
    visibility comes from the errors_total Prometheus metric
    (component="respond", error_type="verdict_submit").
    """
    envelope = wrap_verdict(
        to_dict(verdict),
        component="respond",
        deployment_id=deployment_id,
    )
    # Submit full envelope; core auto-detects and unwraps.
    # See docs/superpowers/decisions/envelope-contract-auto-detect-to-mandatory.md
    result = await client.submit_verdict(envelope)
    if not result.ok:
        logger.warning(
            "respond_verdict_submit_failed",
            verdict_id=verdict.id,
            status_code=result.status_code,
            error=result.error,
        )
        errors_total.labels(component="respond", error_type="verdict_submit").inc()
        return False
    return True
