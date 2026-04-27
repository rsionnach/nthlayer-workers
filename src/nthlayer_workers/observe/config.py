"""Configuration for nthlayer-observe."""

from dataclasses import dataclass


@dataclass
class ObserveConfig:
    """Runtime configuration for nthlayer-observe."""

    prometheus_url: str = "http://localhost:9090"
    store_path: str = "assessments.db"
