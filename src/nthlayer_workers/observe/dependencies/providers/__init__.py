"""
Dependency discovery providers.

Providers discover service dependencies from various sources
like Prometheus metrics, Kubernetes, Backstage, Consul, Zookeeper, etcd, etc.
"""

from nthlayer_workers.observe.dependencies.providers.backstage import (
    BackstageDepProvider,
    BackstageDepProviderError,
)
from nthlayer_workers.observe.dependencies.providers.base import BaseDepProvider, ProviderHealth
from nthlayer_workers.observe.dependencies.providers.consul import (
    ConsulDepProvider,
    ConsulDepProviderError,
)

# Optional providers - import errors are handled within the modules
try:
    from nthlayer_workers.observe.dependencies.providers.zookeeper import (
        ZookeeperDepProvider,
        ZookeeperDepProviderError,
    )
except ImportError:
    ZookeeperDepProvider = None  # type: ignore
    ZookeeperDepProviderError = None  # type: ignore

try:
    from nthlayer_workers.observe.dependencies.providers.etcd import (
        EtcdDepProvider,
        EtcdDepProviderError,
    )
except ImportError:
    EtcdDepProvider = None  # type: ignore
    EtcdDepProviderError = None  # type: ignore

__all__ = [
    "BaseDepProvider",
    "ProviderHealth",
    "BackstageDepProvider",
    "BackstageDepProviderError",
    "ConsulDepProvider",
    "ConsulDepProviderError",
    "ZookeeperDepProvider",
    "ZookeeperDepProviderError",
    "EtcdDepProvider",
    "EtcdDepProviderError",
]
