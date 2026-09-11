"""Device and source catalog used by every build entry point.

The catalog is deliberately small and declarative.  A device name is resolved
once at the beginning of a build and the resulting :class:`DeviceSpec` is
carried through the build manifest.  This keeps the CLI, web actions and
background workers from growing separate lists of supported boards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib
from typing import Any, Mapping

from .paths import catalog_path as default_catalog_path


class CatalogError(ValueError):
    """Raised when the device catalog is malformed or a device is unknown."""


def _normalise(value: str) -> str:
    return re.sub(r"\s+", "", str(value).strip().lower())


@dataclass(frozen=True)
class SourceSpec:
    """An immutable source snapshot from which a build may be prepared."""

    id: str
    url: str
    branch: str
    snapshot: str | None
    platform: str

    def __post_init__(self) -> None:
        for name in ("id", "url", "branch", "platform"):
            if not getattr(self, name):
                raise CatalogError(f"source field {name!r} must not be empty")
        if self.snapshot is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", self.snapshot):
            raise CatalogError(
                f"source {self.id!r} snapshot must be a full 40-character git SHA"
            )

    @property
    def snapshot_id(self) -> str:
        """Stable public identifier for the source snapshot."""

        return self.snapshot.lower() if self.snapshot else ""

    def to_dict(self) -> dict[str, str | None]:
        return {
            "id": self.id,
            "url": self.url,
            "branch": self.branch,
            "snapshot": self.snapshot_id or None,
            "platform": self.platform,
        }


@dataclass(frozen=True)
class DeviceSpec:
    """Build properties for one supported device."""

    key: str
    aliases: tuple[str, ...]
    platform: str
    source_id: str
    profile: str
    config: str
    packager: str
    target: str
    description: str = ""
    default_packages: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.key or not self.source_id or not self.profile:
            raise CatalogError("device key, source_id and profile are required")
        if self.packager not in {"mediatek", "arm"}:
            raise CatalogError(
                f"device {self.key!r} has unsupported packager {self.packager!r}"
            )
        if self.platform not in {"mediatek", "arm"}:
            raise CatalogError(
                f"device {self.key!r} has unsupported platform {self.platform!r}"
            )
        if not self.config.endswith(".config"):
            raise CatalogError(f"device {self.key!r} config must end in .config")

    @property
    def all_aliases(self) -> tuple[str, ...]:
        values = [self.key, *self.aliases]
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            normalised = _normalise(value)
            if normalised and normalised not in seen:
                result.append(normalised)
                seen.add(normalised)
        return tuple(result)

    def to_dict(self, source: SourceSpec | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "key": self.key,
            "aliases": list(self.aliases),
            "platform": self.platform,
            "source_id": self.source_id,
            "profile": self.profile,
            "config": self.config,
            "packager": self.packager,
            "target": self.target,
            "description": self.description,
            "default_packages": list(self.default_packages),
        }
        if source is not None:
            result["source"] = source.to_dict()
        return result


@dataclass(frozen=True)
class DeviceCatalog:
    """Parsed catalog with stable resolution helpers."""

    path: Path
    default_config: str
    sources: Mapping[str, SourceSpec]
    devices: Mapping[str, DeviceSpec]

    def resolve(self, name: str) -> DeviceSpec:
        query = _normalise(name)
        for spec in self.devices.values():
            if query in spec.all_aliases:
                return spec
        available = ", ".join(sorted(self.devices))
        raise CatalogError(f"unsupported device {name!r}; choose one of: {available}")

    def source_for(self, device: DeviceSpec) -> SourceSpec:
        try:
            return self.sources[device.source_id]
        except KeyError as exc:
            raise CatalogError(
                f"device {device.key!r} references unknown source {device.source_id!r}"
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "default_config": self.default_config,
            "sources": {key: value.to_dict() for key, value in self.sources.items()},
            "devices": {
                key: value.to_dict(self.sources.get(value.source_id))
                for key, value in self.devices.items()
            },
        }


def _as_string(data: Mapping[str, Any], key: str, *, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _as_strings(data: Mapping[str, Any], key: str, *, where: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CatalogError(f"{where}.{key} must be a list of strings")
    return tuple(item.strip() for item in value if item.strip())


def load_catalog(path: str | Path | None = None) -> DeviceCatalog:
    """Load and validate ``configs/devices.toml``.

    ``path`` is optional so callers from the web service and the CLI get the
    same repository-relative default.  No user-supplied source URL or commit
    is accepted by this function; snapshots are part of the reviewed catalog.
    """

    catalog_file = Path(path) if path is not None else default_catalog_path()
    catalog_file = catalog_file.expanduser().resolve()
    try:
        with catalog_file.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise CatalogError(f"device catalog not found: {catalog_file}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"invalid device catalog {catalog_file}: {exc}") from exc

    default_config = raw.get("default_config", "armv8.config")
    if not isinstance(default_config, str) or not default_config.endswith(".config"):
        raise CatalogError("default_config must name a .config file")

    source_rows = raw.get("sources")
    if not isinstance(source_rows, list) or not source_rows:
        raise CatalogError("devices.toml must contain at least one [[sources]] row")
    sources: dict[str, SourceSpec] = {}
    for index, row in enumerate(source_rows):
        where = f"sources[{index}]"
        if not isinstance(row, dict):
            raise CatalogError(f"{where} must be a table")
        source = SourceSpec(
            id=_as_string(row, "id", where=where),
            url=_as_string(row, "url", where=where),
            branch=_as_string(row, "branch", where=where),
            snapshot=(
                _as_string(row, "snapshot", where=where)
                if row.get("snapshot") is not None
                else None
            ),
            platform=_as_string(row, "platform", where=where),
        )
        if source.id in sources:
            raise CatalogError(f"duplicate source id {source.id!r}")
        sources[source.id] = source

    device_rows = raw.get("devices")
    if not isinstance(device_rows, list) or not device_rows:
        raise CatalogError("devices.toml must contain at least one [[devices]] row")
    devices: dict[str, DeviceSpec] = {}
    aliases: dict[str, str] = {}
    for index, row in enumerate(device_rows):
        where = f"devices[{index}]"
        if not isinstance(row, dict):
            raise CatalogError(f"{where} must be a table")
        spec = DeviceSpec(
            key=_as_string(row, "key", where=where),
            aliases=_as_strings(row, "aliases", where=where),
            platform=_as_string(row, "platform", where=where),
            source_id=_as_string(row, "source", where=where),
            profile=_as_string(row, "profile", where=where),
            config=_as_string(row, "config", where=where),
            packager=_as_string(row, "packager", where=where),
            target=_as_string(row, "target", where=where),
            description=str(row.get("description", "")).strip(),
            default_packages=_as_strings(row, "default_packages", where=where),
        )
        if spec.key in devices:
            raise CatalogError(f"duplicate device key {spec.key!r}")
        if spec.source_id not in sources:
            raise CatalogError(
                f"device {spec.key!r} references unknown source {spec.source_id!r}"
            )
        if sources[spec.source_id].platform != spec.platform:
            raise CatalogError(
                f"device {spec.key!r} platform does not match source {spec.source_id!r}"
            )
        config_path = catalog_file.parent / spec.config
        if not config_path.is_file():
            raise CatalogError(f"device {spec.key!r} config not found: {config_path}")
        for alias in spec.all_aliases:
            previous = aliases.get(alias)
            if previous is not None and previous != spec.key:
                raise CatalogError(f"alias {alias!r} is used by {previous!r} and {spec.key!r}")
            aliases[alias] = spec.key
        devices[spec.key] = spec

    default_path = catalog_file.parent / default_config
    if not default_path.is_file():
        raise CatalogError(f"default config not found: {default_path}")
    return DeviceCatalog(
        path=catalog_file,
        default_config=default_config,
        sources=sources,
        devices=devices,
    )


def resolve_device(name: str, path: str | Path | None = None) -> DeviceSpec:
    """Resolve a canonical device key or alias from the reviewed catalog."""

    return load_catalog(path).resolve(name)


__all__ = [
    "CatalogError",
    "DeviceCatalog",
    "DeviceSpec",
    "SourceSpec",
    "load_catalog",
    "resolve_device",
]
