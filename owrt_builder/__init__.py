"""OpenWrt builder control plane package.

The web application lives in :mod:`owrt_builder.web`; the build, source and
catalog implementations are deliberately kept in separate modules so the CLI
and the worker use the same core implementation.
"""

from .build import BuildEngine, BuildRequest, BuildResult
from .devices import DeviceCatalog, DeviceSpec, SourceSpec, load_catalog, resolve_device
from .paths import catalog_path, repository_root
from .sources import PreparedSource

__all__ = [
    "web",
    "BuildEngine",
    "BuildRequest",
    "BuildResult",
    "PreparedSource",
    "DeviceCatalog",
    "DeviceSpec",
    "SourceSpec",
    "catalog_path",
    "load_catalog",
    "repository_root",
    "resolve_device",
]
