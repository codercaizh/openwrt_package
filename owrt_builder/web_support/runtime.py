"""Runtime configuration and typed integration points for the Web service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..build import BuildEngine, BuildRequest, logical_cpu_count
from ..catalog import Catalog, scan_catalog
from ..devices import DeviceCatalog, load_catalog
from ..paths import catalog_path, repository_root
from ..sources import SourceManager


def system_logical_cpus() -> int:
    """Return the logical CPU ceiling used by both UI and API validation."""

    return logical_cpu_count()


class RuntimeNotReady(RuntimeError):
    """The typed core modules are unavailable or have no prepared source."""


class SourceSnapshotUnavailable(RuntimeError):
    """A persisted source snapshot cannot be consumed by a build worker."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


@dataclass
class Runtime:
    """Typed integration points used by the web worker.

    The concrete classes/functions are imported from the fixed modules named
    in ``docs/architecture.md``.  The build engine exposes
    ``build(request, on_log=..., cancel_event=...)`` and ``request_type`` is
    the core ``BuildRequest`` DTO.
    """

    devices: DeviceCatalog
    sources: SourceManager
    build: BuildEngine
    scan_catalog: Callable[[str | Path], Catalog] = scan_catalog
    request_type: type[BuildRequest] = BuildRequest


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("OWRT_DATA_DIR", "data")))
    db_path: Path | None = None
    log_dir: Path | None = None
    artifact_dir: Path | None = None
    config_dir: Path = field(default_factory=lambda: Path(os.getenv("OWRT_CONFIG_DIR", "configs")))
    bind_host: str = field(default_factory=lambda: os.getenv("OWRT_BIND_HOST", "127.0.0.1"))
    bind_port: int = field(default_factory=lambda: int(os.getenv("OWRT_BIND_PORT", "8000")))
    worker_poll_seconds: float = 0.5
    source_refresh_timeout_seconds: int = 6 * 60 * 60

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.db_path = Path(self.db_path or os.getenv("OWRT_DB_PATH", str(self.data_dir / "state.sqlite3")))
        self.log_dir = Path(self.log_dir or os.getenv("OWRT_LOG_DIR", str(self.data_dir / "logs")))
        self.artifact_dir = Path(self.artifact_dir or os.getenv("OWRT_ARTIFACT_DIR", str(self.data_dir / "artifacts")))
        self.config_dir = Path(self.config_dir)


def load_runtime(settings: Settings | None = None) -> Runtime:
    """Load the explicitly agreed core modules; never fabricate catalog data."""

    settings = settings or Settings()
    repo_root = Path(os.getenv("OWRT_REPO_ROOT", str(repository_root()))).expanduser().resolve()
    device_catalog = load_catalog(catalog_path(repo_root))
    workspace = Path(os.getenv("OWRT_WORKSPACE", str(settings.data_dir / "workspace")))
    return Runtime(
        devices=device_catalog,
        # BuildEngine resolves prepared snapshots from ``workspace/sources``;
        # use the same root for Web preparation so the pinned snapshot id is
        # consumable by the worker process and by CLI builds.
        sources=SourceManager(workspace / "sources", source_specs=device_catalog.sources),
        build=BuildEngine(repo_root, workspace, catalog=device_catalog),
        scan_catalog=scan_catalog,
        request_type=BuildRequest,
    )


__all__ = [
    "Runtime",
    "RuntimeNotReady",
    "Settings",
    "SourceSnapshotUnavailable",
    "load_runtime",
    "system_logical_cpus",
]
