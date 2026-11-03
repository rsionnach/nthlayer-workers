"""NthLayer workers — Tier 2 background computation modules.

Internal modules: observe, measure, correlate, respond, learn.
Communicates with nthlayer-core exclusively via HTTP API.
"""

import logging
from importlib.metadata import version

__version__ = version("nthlayer-workers")

# The OTel service.name every CLI entry point passes to nthlayer-common's
# init_telemetry, which each calls after parsing its arguments and before
# running a command, so the command's events export (opensrm-quqo). Without
# an OTLP endpoint that call is a no-op.
TELEMETRY_SERVICE_NAME = "nthlayer-workers"


def log_telemetry_status(status) -> None:
    """Log what init_telemetry did, so "are events exporting?" is answerable
    from the process's own debug log. SDK_MISSING already warns in common."""
    logging.getLogger(__name__).debug("telemetry: %s", getattr(status, "value", status))
