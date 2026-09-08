"""OpenWrt builder control plane package.

The web application lives in :mod:`owrt_builder.web`; the build, source and
catalog implementations are deliberately kept in separate modules so the CLI
and the worker use the same core implementation.
"""

from .build import BuildEngine, BuildRequest, BuildResult
from .devices import DeviceSpec, SourceSpec, load_catalog, resolve_device
from .sources import PreparedSource

__all__ = [
    "web",
    "BuildEngine",
    "BuildRequest",
    "BuildResult",
    "PreparedSource",
    "DeviceSpec",
    "SourceSpec",
    "load_catalog",
    "resolve_device",
]
