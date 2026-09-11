"""Stable paths for the checked-in project layout.

The command line entry point, Web service and source preparer all run from
different working directories (and the Web service may run in a container).
Keep paths to repository-owned assets in one small module so callers do not
each make a slightly different ``__file__.parents[...]`` assumption.

Environment-controlled runtime paths such as the workspace and Web data
directory intentionally remain with their owning components.  This module is
only for immutable assets that are part of the project layout.
"""

from __future__ import annotations

from pathlib import Path


def package_root() -> Path:
    """Return the installed ``owrt_builder`` package directory."""

    return Path(__file__).resolve().parent


def repository_root() -> Path:
    """Return the repository root that contains ``configs`` and ``scripts``."""

    return package_root().parent


def catalog_path(root: str | Path | None = None) -> Path:
    """Return the reviewed device catalog for *root* or this repository."""

    return (Path(root) if root is not None else repository_root()) / "configs" / "devices.toml"


def static_path() -> Path:
    """Return the Web static asset directory bundled with the package."""

    return package_root() / "static"


def rust_patch_path(root: str | Path | None = None) -> Path:
    """Return the optional Rust Makefile patch shipped by the repository."""

    return (Path(root) if root is not None else repository_root()) / "scripts" / "fix_bugs" / "rust_Makefile"


__all__ = [
    "catalog_path",
    "package_root",
    "repository_root",
    "rust_patch_path",
    "static_path",
]
