"""OpenWrt source selection and immutable snapshot management.

This module owns the two source trees used by the project.  A build receives a
snapshot path and never follows the moving ``current`` link, so an automatic
feed update cannot alter a build already in progress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import uuid
from typing import Any, Callable, Mapping, Sequence

from .devices import DeviceSpec, SourceSpec, load_catalog, resolve_device as _resolve_device


StatusCallback = Callable[[Mapping[str, Any]], None]

# Bump this whenever the set of files allowed in a published source changes.
# It prevents an older snapshot (created before generated-tree cleanup was
# introduced) from being reused solely because its git/feed fingerprint is
# unchanged.
# Bump when the source publication contract or generated catalogue parser
# changes.  Version 3 invalidated early snapshots that included generated
# ``dl``/``tmp`` trees; version 4 invalidated catalogues made before
# ``menuconfig PACKAGE_*`` block boundaries were recognized; version 5 also
# removes feed indexes and preparation-container symlinks from the payload.
# Version 6 invalidates snapshots created while Web native defconfig
# validation could write into the published tree.  Such snapshots may contain
# ``staging_dir``/``tmp`` and must never be handed to a worker again.
# Version 7 preserves and validates source archives which are tracked by the
# OpenWrt repository under ``dl``.  Earlier snapshots deleted those archives
# while cleaning preparation-container state.  Version 8 adds the feed
# toolchain compatibility gate for prepared Go package modules.
PREPARATION_VERSION = 8
_GENERATED_TREE_NAMES = ("build_dir", "staging_dir", "tmp", "dl", "logs", "bin")


class SourceError(RuntimeError):
    """A source/feed operation failed before a new snapshot was published."""


@dataclass(frozen=True)
class DownloadSeed:
    """A regular file tracked by the source repository under ``dl``."""

    relative_path: PurePosixPath
    source_path: Path
    object_id: str
    mode: int
    sha256: str


@dataclass(frozen=True)
class PreparedSource:
    """An immutable source snapshot returned to a build task."""

    source_id: str
    snapshot_id: str
    path: Path
    catalog_path: Path
    source_commit: str
    feed_commits: Mapping[str, str] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "snapshot_id": self.snapshot_id,
            "path": str(self.path),
            "catalog_path": str(self.catalog_path),
            "source_commit": self.source_commit,
            "feed_commits": dict(self.feed_commits),
            "created_at": self.created_at,
        }


class _LazyCatalogMap(Mapping[str, Any]):
    """Expose the reviewed devices.toml without duplicating its constants."""

    def __init__(self, field: str) -> None:
        self.field = field

    def _mapping(self) -> Mapping[str, Any]:
        catalog = load_catalog()
        return getattr(catalog, self.field)

    def __getitem__(self, key: str) -> Any:
        return self._mapping()[key]

    def __iter__(self):
        return iter(self._mapping())

    def __len__(self) -> int:
        return len(self._mapping())


# These views are intentionally backed by configs/devices.toml.  Keeping them
# as lazy mappings prevents a second hard-coded device/source list here while
# preserving a convenient API for existing CLI callers.
SOURCE_SPECS: Mapping[str, SourceSpec] = _LazyCatalogMap("sources")
DEVICE_SPECS: Mapping[str, DeviceSpec] = _LazyCatalogMap("devices")


def resolve_device(value: str | DeviceSpec) -> DeviceSpec:
    """Resolve a canonical device key or alias.

    ``s905`` intentionally does not resolve to ``s905d``: the two packagers
    represent different hardware and silently treating them as aliases can
    produce an unbootable image.
    """

    if isinstance(value, DeviceSpec):
        return value
    try:
        return _resolve_device(str(value))
    except Exception as exc:
        raise KeyError(f"unsupported device: {value}") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _emit(callback: StatusCallback | None, phase: str, message: str, **extra: Any) -> None:
    if callback is None:
        return
    event: dict[str, Any] = {"phase": phase, "message": message, "at": _utc_now()}
    event.update(extra)
    try:
        callback(event)
    except Exception:
        # A progress subscriber must never leave a half-built snapshot behind.
        return


def _json_load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise SourceError(f"invalid snapshot manifest: {path}")
    return value


def _remove_path(path: Path) -> None:
    """Remove one path without following a top-level symlink."""

    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _safe_download_relative_path(raw: str) -> PurePosixPath:
    """Validate a Git path before using it below a source/cache root."""

    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.parts[0] != "dl"
        or len(relative.parts) == 1
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise SourceError(f"invalid tracked download seed path: {raw!r}")
    return relative


def _git_run(source_path: Path, args: Sequence[str], *, text: bool = False) -> bytes | str:
    """Run a read-only Git query for a prepared source tree."""

    try:
        result = subprocess.run(
            ["git", "-C", str(source_path), *args],
            capture_output=True,
            check=True,
            text=text,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise SourceError(f"git {' '.join(args)} failed: {detail}") from exc
    return result.stdout


def _tracked_download_entries(source_path: Path) -> dict[PurePosixPath, tuple[int, str]]:
    """Return regular-file entries tracked by ``HEAD`` below ``dl``.

    The source snapshots retain their Git metadata, which gives us an
    authoritative allow-list even when the preparation commands have left
    ignored or untracked files in the working tree.  A source without Git
    metadata cannot have trusted seeds; callers handle that as an empty list
    during cleanup and as an error when a ``dl`` tree is present at validation.
    """

    git_metadata = source_path / ".git"
    if not git_metadata.exists():
        return {}
    if git_metadata.is_symlink() or not git_metadata.is_dir():
        raise SourceError(f"source Git metadata must be a private directory: {git_metadata}")
    output = _git_run(
        source_path,
        ["ls-tree", "-r", "-z", "--full-tree", "HEAD", "--", "dl"],
    )
    assert isinstance(output, bytes)
    entries: dict[PurePosixPath, tuple[int, str]] = {}
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_name = record.split(b"\t", 1)
            mode_text, object_type, object_id = metadata.decode("ascii").split()
            name = raw_name.decode("utf-8", errors="surrogateescape")
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceError(f"invalid Git tree entry below dl in {source_path}") from exc
        relative = _safe_download_relative_path(name)
        mode = int(mode_text, 8)
        if object_type != "blob" or not stat.S_ISREG(mode):
            raise SourceError(f"tracked download seed is not a regular file: {name}")
        entries[relative] = (mode, object_id)
    return entries


def _git_blob(source_path: Path, object_id: str) -> bytes:
    output = _git_run(source_path, ["cat-file", "blob", object_id])
    assert isinstance(output, bytes)
    return output


def _git_hash(source_path: Path, relative: PurePosixPath) -> str:
    output = _git_run(source_path, ["hash-object", "--", relative.as_posix()], text=True)
    assert isinstance(output, str)
    return output.strip()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_download_seeds(source_path: str | os.PathLike[str]) -> dict[PurePosixPath, DownloadSeed]:
    """Validate and return the exact tracked files present below ``dl``.

    A source snapshot may contain ``dl`` only when every file in it is a
    regular file tracked by the source repository's ``HEAD``.  The working
    tree is checked for extra files, symlinks, path escapes, missing files and
    content changes.  An absent ``dl`` directory is valid for sources that do
    not vendor download seeds.
    """

    root = Path(source_path)
    if root.is_symlink() or not root.is_dir():
        raise SourceError(f"prepared source path missing: {root}")
    dl = root / "dl"
    has_dl = dl.exists() or dl.is_symlink()
    entries = _tracked_download_entries(root)
    if not entries:
        if has_dl:
            raise SourceError("source snapshot contains an untracked or empty dl tree")
        return {}
    if dl.is_symlink() or not dl.is_dir():
        raise SourceError("source snapshot dl tree must be a regular directory")

    seeds: dict[PurePosixPath, DownloadSeed] = {}
    expected = set(entries)
    root_resolved = root.resolve()
    dl_resolved = dl.resolve()
    for relative, (mode, object_id) in entries.items():
        path = root.joinpath(*relative.parts)
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root_resolved)
            resolved.relative_to(dl_resolved)
        except (OSError, ValueError) as exc:
            raise SourceError(f"download seed is missing: {relative}") from exc
        parent = path.parent
        while parent != root:
            if parent.is_symlink():
                raise SourceError(f"download seed path contains a symlink: {relative}")
            parent = parent.parent
        if path.is_symlink() or not path.is_file():
            raise SourceError(f"download seed is not a regular file: {relative}")
        actual_object_id = _git_hash(root, relative)
        if actual_object_id != object_id:
            raise SourceError(f"download seed content differs from Git HEAD: {relative}")
        seeds[relative] = DownloadSeed(
            relative_path=relative,
            source_path=path,
            object_id=object_id,
            mode=mode,
            sha256=_file_sha256(path),
        )

    seen: set[PurePosixPath] = set()
    for current, directories, files in os.walk(dl, followlinks=False):
        current_path = Path(current)
        for name in directories:
            item = current_path / name
            relative = PurePosixPath(item.relative_to(root).as_posix())
            if item.is_symlink():
                raise SourceError(f"source snapshot dl contains a symlink: {relative}")
            if not item.is_dir():
                raise SourceError(f"source snapshot dl contains a non-directory: {relative}")
            try:
                item.resolve(strict=True).relative_to(dl_resolved)
            except (OSError, ValueError) as exc:
                raise SourceError(f"source snapshot dl path escapes its root: {relative}") from exc
        for name in files:
            item = current_path / name
            relative = PurePosixPath(item.relative_to(root).as_posix())
            if item.is_symlink():
                raise SourceError(f"source snapshot dl contains a symlink: {relative}")
            if not item.is_file():
                raise SourceError(f"source snapshot dl contains a non-regular file: {relative}")
            if relative not in expected:
                raise SourceError(f"source snapshot contains an untracked dl file: {relative}")
            seen.add(relative)
    missing = expected - seen
    if missing:
        names = ", ".join(sorted(path.as_posix() for path in missing))
        raise SourceError(f"source snapshot is missing tracked dl seed(s): {names}")
    return seeds


def _restore_tracked_download_seeds(
    source_path: Path,
    entries: Mapping[PurePosixPath, tuple[int, str]],
) -> None:
    """Replace ``source_path/dl`` with the exact Git-tracked seed files."""

    dl = source_path / "dl"
    if not entries:
        _remove_path(dl)
        return
    temporary = source_path / f".dl-seeds.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir()
    try:
        for relative, (mode, object_id) in entries.items():
            destination = temporary.joinpath(*relative.parts[1:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_git_blob(source_path, object_id))
            os.chmod(destination, mode & 0o777)
        _remove_path(dl)
        os.replace(temporary, dl)
    finally:
        _remove_path(temporary)


@contextmanager
def _download_seed_lock(download_cache: Path):
    """Serialize seed imports without locking the mutable OpenWrt tree."""

    lock_path = download_cache.parent / ".dl-seeds.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError) as exc:
            raise SourceError(f"unable to lock download cache: {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _cache_target(download_cache: Path, relative: PurePosixPath) -> Path:
    """Create and validate cache parents without following symlinks."""

    if download_cache.is_symlink() or not download_cache.is_dir():
        raise SourceError(f"download cache must be a regular directory: {download_cache}")
    parent = download_cache
    for part in relative.parts[1:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise SourceError(f"download cache path contains a symlink: {relative}")
        if parent.exists() and not parent.is_dir():
            raise SourceError(f"download cache path is not a directory: {relative}")
        parent.mkdir(exist_ok=True)
    return parent / relative.parts[-1]


def _install_download_seed(seed: DownloadSeed, download_cache: Path) -> None:
    target = _cache_target(download_cache, seed.relative_path)
    if target.is_symlink():
        raise SourceError(f"download cache contains a symlink for {seed.relative_path}")
    if target.is_dir():
        raise SourceError(f"download cache contains a directory for {seed.relative_path}")
    if target.is_file() and _file_sha256(target) == seed.sha256:
        return

    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.seed.",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(temporary_fd, "wb") as output, seed.source_path.open("rb") as source:
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, seed.mode & 0o777)
        os.replace(temporary, target)
        if _file_sha256(target) != seed.sha256:
            raise SourceError(f"download cache seed verification failed: {seed.relative_path}")
    finally:
        temporary.unlink(missing_ok=True)


def stage_download_seeds(
    source_path: str | os.PathLike[str],
    download_cache: str | os.PathLike[str],
) -> tuple[DownloadSeed, ...]:
    """Atomically import validated source seeds into the shared download cache."""

    seeds = validate_download_seeds(source_path)
    cache = Path(download_cache)
    cache_parent = cache.parent
    if cache_parent.is_symlink():
        raise SourceError(f"download cache parent must not be a symlink: {cache_parent}")
    if cache_parent.exists() and not cache_parent.is_dir():
        raise SourceError(f"download cache parent must be a directory: {cache_parent}")
    if cache.is_symlink():
        raise SourceError(f"download cache must not be a symlink: {cache}")
    if cache.exists() and not cache.is_dir():
        raise SourceError(f"download cache must be a regular directory: {cache}")
    cache_parent.mkdir(parents=True, exist_ok=True)
    if cache_parent.is_symlink() or not cache_parent.is_dir():
        raise SourceError(f"download cache parent must be a real directory: {cache_parent}")
    cache.mkdir(parents=True, exist_ok=True)
    if cache.is_symlink() or not cache.is_dir():
        raise SourceError(f"download cache must be a regular directory: {cache}")
    if not seeds:
        return ()
    with _download_seed_lock(cache):
        for seed in seeds.values():
            _install_download_seed(seed, cache)
    return tuple(seeds.values())


class SourceManager:
    """Prepare and publish source/feed snapshots atomically.

    The default root is suitable for a container volume.  Tests and local
    development should pass a temporary directory.  The manager does not
    delete an existing snapshot on failure; old snapshots can be garbage
    collected later by a separate retention job.
    """

    def __init__(
        self,
        root: str | os.PathLike[str] = "/var/lib/owrt-builder/sources",
        *,
        source_specs: Mapping[str, SourceSpec] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.root = Path(root)
        self.source_specs = dict(source_specs or SOURCE_SPECS)
        self._run = command_runner or self._default_run

    @staticmethod
    def _default_run(
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        capture_output: bool = True,
        text: bool = True,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            cwd=str(cwd) if cwd is not None else None,
            capture_output=capture_output,
            text=text,
            check=check,
        )

    def _base(self, source_id: str) -> Path:
        return self.root / source_id

    def _snapshots_dir(self, source_id: str) -> Path:
        return self._base(source_id) / "snapshots"

    def _current_link(self, source_id: str) -> Path:
        return self._base(source_id) / "current"

    def _run_git(self, args: Sequence[str], *, cwd: Path | None = None) -> str:
        try:
            result = self._run(["git", *args], cwd=cwd)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            raise SourceError(f"git {' '.join(args)} failed: {detail}") from exc
        return (result.stdout or "").strip()

    def _clone(self, spec: SourceSpec, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--branch", spec.branch, "--single-branch"]
        depth = getattr(spec, "depth", 1)
        if depth:
            args += ["--depth", str(depth)]
        args += [spec.url, str(destination)]
        self._run_git(args)

    def _source_commit(self, path: Path) -> str:
        commit = self._run_git(["rev-parse", "HEAD"], cwd=path)
        if not commit or any(ch.isspace() for ch in commit):
            raise SourceError(f"repository has no usable HEAD: {path}")
        return commit

    def _snapshot_location_is_valid(self, source_id: str, snapshot_dir: Path) -> bool:
        """Ensure a published snapshot stays below its source's snapshots dir.

        ``current`` is an internal symlink, but it is stored in a writable
        workspace and can be left dangling or redirected after an interrupted
        run.  Resolving it without checking the parent would let a malformed
        manifest outside the source cache become a build input.  Resolve both
        sides so a deliberately shared source-cache symlink (used by the Web
        acceptance setup) remains valid while an external target is rejected.
        """

        try:
            resolved = snapshot_dir.resolve()
            expected_parent = self._snapshots_dir(source_id).resolve()
        except OSError:
            return False
        return resolved.parent == expected_parent

    def _read_prepared(
        self,
        snapshot_dir: Path,
        *,
        expected_source_id: str | None = None,
    ) -> PreparedSource:
        try:
            manifest = _json_load(snapshot_dir / "manifest.json")
        except (OSError, ValueError) as exc:
            raise SourceError(f"invalid snapshot manifest: {snapshot_dir}") from exc
        try:
            preparation_version = int(manifest["preparation_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"snapshot has no valid preparation version: {snapshot_dir}") from exc
        if preparation_version != PREPARATION_VERSION:
            raise SourceError(
                f"snapshot preparation version {preparation_version} is not supported "
                f"(expected {PREPARATION_VERSION}): {snapshot_dir}"
            )
        try:
            source_id = str(manifest["source_id"])
            snapshot_id = str(manifest["snapshot_id"])
            source_commit = str(manifest["source_commit"])
            feed_commits = {
                str(k): str(v) for k, v in dict(manifest.get("feed_commits", {})).items()
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceError(f"invalid snapshot manifest: {snapshot_dir}") from exc
        if expected_source_id is not None and source_id != expected_source_id:
            raise SourceError(
                f"snapshot source id {source_id!r} does not match {expected_source_id!r}: "
                f"{snapshot_dir}"
            )
        if snapshot_dir.resolve().name != snapshot_id:
            raise SourceError(
                f"snapshot id {snapshot_id!r} does not match directory {snapshot_dir.name!r}: "
                f"{snapshot_dir}"
            )
        source_path = snapshot_dir / "source"
        catalog_path = snapshot_dir / "catalog.json"
        if not source_path.is_dir() or not catalog_path.is_file():
            raise SourceError(f"incomplete source snapshot: {snapshot_dir}")
        stale_generated = [
            name
            for name in _GENERATED_TREE_NAMES
            if name != "dl"
            if (source_path / name).exists() or (source_path / name).is_symlink()
        ]
        feeds_path = source_path / "feeds"
        if feeds_path.is_dir():
            stale_generated.extend(
                f"feeds/{item.name}"
                for item in feeds_path.iterdir()
                if (
                    item.name.endswith(".tmp")
                    or item.name == "base"
                    or item.name.endswith((".index", ".targetindex"))
                )
            )
        if stale_generated:
            raise SourceError(
                f"source snapshot contains preparation-runtime directories: "
                f"{', '.join(stale_generated)}"
            )
        validate_download_seeds(source_path)
        return PreparedSource(
            source_id=source_id,
            snapshot_id=snapshot_id,
            path=source_path,
            catalog_path=catalog_path,
            source_commit=source_commit,
            feed_commits=feed_commits,
            created_at=str(manifest.get("created_at", "")),
        )

    def current(self, source_id_or_device: str | DeviceSpec) -> PreparedSource | None:
        source_id = (
            resolve_device(source_id_or_device).source_id
            if isinstance(source_id_or_device, DeviceSpec) or source_id_or_device in DEVICE_SPECS
            else str(source_id_or_device)
        )
        link = self._current_link(source_id)
        if not link.exists() and not link.is_symlink():
            return None
        try:
            resolved = link.resolve()
            if not self._snapshot_location_is_valid(source_id, resolved):
                return None
            return self._read_prepared(resolved, expected_source_id=source_id)
        except SourceError:
            return None

    def get_snapshot(self, snapshot_id: str) -> PreparedSource:
        """Find an immutable snapshot by its opaque id."""

        if not snapshot_id or Path(snapshot_id).name != snapshot_id:
            raise KeyError("invalid snapshot id")
        for source_dir in self.root.iterdir() if self.root.exists() else ():
            snapshots = source_dir / "snapshots"
            candidate = snapshots / snapshot_id
            if candidate.is_dir():
                source_id = source_dir.name
                if not self._snapshot_location_is_valid(source_id, candidate):
                    raise SourceError(f"snapshot is outside source cache: {candidate}")
                prepared = self._read_prepared(candidate, expected_source_id=source_id)
                if prepared.snapshot_id != snapshot_id:
                    raise SourceError(f"snapshot id mismatch: {candidate}")
                return prepared
        raise KeyError(f"unknown snapshot: {snapshot_id}")

    def _publish(self, source_id: str, snapshot_dir: Path) -> None:
        base = self._base(source_id)
        base.mkdir(parents=True, exist_ok=True)
        current = self._current_link(source_id)
        if current.exists() and not current.is_symlink():
            raise SourceError(f"refusing to replace non-symlink current path: {current}")
        # A process killed after creating the temporary link can leave it
        # behind.  Remove only our own publication temporaries before making
        # the next atomic replacement.
        for stale in base.glob(".current.*.tmp"):
            if stale.is_symlink() or stale.is_file():
                stale.unlink(missing_ok=True)
            elif stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
        temporary = base / f".current.{uuid.uuid4().hex}.tmp"
        relative_target = os.path.relpath(snapshot_dir, base)
        os.symlink(relative_target, temporary)
        try:
            os.replace(temporary, current)
        finally:
            if temporary.is_symlink() or temporary.exists():
                temporary.unlink(missing_ok=True)

    def _remove_stale_staging(self, source_id: str) -> None:
        """Remove staging trees left by a process that was killed mid-clone."""

        base = self._base(source_id)
        if not base.is_dir():
            return
        for candidate in base.glob(".staging-*"):
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink(missing_ok=True)
            elif candidate.is_dir():
                shutil.rmtree(candidate, ignore_errors=True)

    @staticmethod
    def _clean_generated_tree(source_path: Path) -> None:
        """Drop host/runtime-generated OpenWrt state before publishing.

        ``make prepare-tmpinfo`` creates host tools under ``staging_dir`` and
        caches under ``tmp``/``dl``.  Those files are tied to the preparation
        container (and may contain absolute interpreter symlinks), so copying
        them into a later build container can make an otherwise valid snapshot
        fail before Kconfig starts.  The exception is a source repository's
        tracked download seeds under ``dl``; those are restored from Git below.
        The authoritative package catalogue has already been serialized beside
        the source tree at this point; the build worker recreates its own
        metadata/build directories.
        """

        tracked_downloads = _tracked_download_entries(source_path)
        for name in _GENERATED_TREE_NAMES:
            if name == "dl":
                continue
            path = source_path / name
            if not (path.exists() or path.is_symlink()):
                continue
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)

        # ``dl`` is normally generated runtime state, but this source tree
        # deliberately tracks several MTK archives there.  Recreate only the
        # exact Git objects so preparation leftovers cannot enter a snapshot.
        _restore_tracked_download_seeds(source_path, tracked_downloads)

        # scripts/feeds leaves these indexes and a ``feeds/base`` symlink in
        # the source tree.  The latter often points at the staging container's
        # absolute path; both are recreated by the worker as needed and must
        # not cross the immutable snapshot boundary.
        feeds_path = source_path / "feeds"
        if not feeds_path.is_dir():
            return
        for item in feeds_path.iterdir():
            remove = (
                item.name.endswith(".tmp")
                or item.name == "base"
                or item.name.endswith((".index", ".targetindex"))
            )
            if not remove:
                continue
            if item.is_symlink() or item.is_file():
                item.unlink()
            else:
                shutil.rmtree(item)

    @contextmanager
    def _prepare_lock(self):
        """Serialize CLI/Web preparation across processes."""

        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".prepare.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                # OpenWrt builds run on Linux; this fallback keeps unit tests
                # and non-POSIX development usable when flock is unavailable.
                pass
            try:
                yield
            finally:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass

    def prepare_source(
        self,
        source_id_or_device: str | DeviceSpec,
        *,
        update: bool = True,
        status: StatusCallback | None = None,
        feed_preparer: Callable[[Path, StatusCallback | None], Mapping[str, str]] | None = None,
        catalog_builder: Callable[[Path], Any] | None = None,
    ) -> PreparedSource:
        """Serialize and prepare one immutable source snapshot."""

        with self._prepare_lock():
            return self._prepare_source(
                source_id_or_device,
                update=update,
                status=status,
                feed_preparer=feed_preparer,
                catalog_builder=catalog_builder,
            )

    def _prepare_source(
        self,
        source_id_or_device: str | DeviceSpec,
        *,
        update: bool = True,
        status: StatusCallback | None = None,
        feed_preparer: Callable[[Path, StatusCallback | None], Mapping[str, str]] | None = None,
        catalog_builder: Callable[[Path], Any] | None = None,
    ) -> PreparedSource:
        """Clone, prepare feeds, scan metadata, then atomically publish.

        ``feed_preparer`` is injectable so the Web layer can share its feed
        cache and tests can use local repositories.  It must return the actual
        feed commits it prepared.  If ``update=False`` an existing current
        snapshot is reused; if none exists, preparation still occurs.
        """

        if isinstance(source_id_or_device, DeviceSpec):
            source_id = source_id_or_device.source_id
        else:
            try:
                source_id = resolve_device(str(source_id_or_device)).source_id
            except KeyError:
                source_id = str(source_id_or_device)
        try:
            spec = self.source_specs[source_id]
        except KeyError as exc:
            raise SourceError(f"unsupported source: {source_id}") from exc

        if not update:
            current = self.current(source_id)
            if current is not None:
                return current

        base = self._base(source_id)
        snapshots = self._snapshots_dir(source_id)
        base.mkdir(parents=True, exist_ok=True)
        snapshots.mkdir(parents=True, exist_ok=True)
        self._remove_stale_staging(source_id)
        staging = base / f".staging-{uuid.uuid4().hex}"
        source_path = staging / "source"
        feed_commits: Mapping[str, str] = {}
        try:
            _emit(status, "source", f"cloning {spec.url} ({spec.branch})", source_id=source_id)
            self._clone(spec, source_path)
            source_commit = self._source_commit(source_path)
            _emit(status, "source", f"checked out {source_commit}", commit=source_commit)

            if feed_preparer is None:
                from .feeds import prepare_feeds_sync

                feed_commits = prepare_feeds_sync(source_path, status=status)
            else:
                from .feeds import validate_go_compatibility

                feed_commits = dict(feed_preparer(source_path, status))
                # Custom feed preparers are used by tests and integrations;
                # keep them behind the same publication gate as the built-in
                # preparer so an incompatible staging tree can never become
                # the new ``current`` snapshot.
                validate_go_compatibility(source_path, status=status)

            if catalog_builder is None:
                from .catalog import scan_catalog

                catalog = scan_catalog(source_path)
            else:
                catalog = catalog_builder(source_path)
            if not hasattr(catalog, "to_dict"):
                raise SourceError("catalog builder returned an unsupported object")
            if not bool(getattr(catalog, "authoritative", False)):
                raise SourceError(
                    "source preparation produced no authoritative OpenWrt package catalog; "
                    "run native feeds install and make package/metadata first"
                )

            # ``catalog`` is serialized outside the source tree below.  Only
            # after that authoritative scan succeeds is it safe to remove
            # generated build metadata and host tools from the source copy.
            self._clean_generated_tree(source_path)

            fingerprint = {
                "preparation_version": PREPARATION_VERSION,
                "source_id": source_id,
                "source_commit": source_commit,
                "branch": spec.branch,
                "feed_commits": dict(feed_commits),
            }
            # The public snapshot id is an opaque content identifier derived
            # from the complete prepared source/feed fingerprint.  It is not
            # a user-selectable commit id; the manifest separately records
            # the actual source and feed HEADs for diagnostics.
            digest = hashlib.sha256(
                json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            final = snapshots / digest
            if final.exists() or final.is_symlink():
                try:
                    prepared = self._read_prepared(final)
                except SourceError:
                    # A crashed process can leave a directory at the content
                    # address without a complete manifest/catalog.  It is
                    # safe to regenerate that address because it cannot be a
                    # valid published snapshot.  A valid current snapshot is
                    # never removed: _read_prepared would have succeeded.
                    if final.is_dir() and not final.is_symlink():
                        shutil.rmtree(final)
                    else:
                        final.unlink(missing_ok=True)
                else:
                    shutil.rmtree(staging, ignore_errors=True)
                    self._publish(source_id, final)
                    _emit(status, "ready", f"reusing snapshot {digest}", snapshot_id=digest)
                    return prepared

            catalog_path = staging / "catalog.json"
            catalog_path.write_text(
                json.dumps(catalog.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            created_at = _utc_now()
            manifest = {
                **fingerprint,
                "snapshot_id": digest,
                "created_at": created_at,
                "catalog": "catalog.json",
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(staging, final)
            prepared = self._read_prepared(final, expected_source_id=source_id)
            self._publish(source_id, final)
            _emit(status, "ready", f"published snapshot {digest}", snapshot_id=digest)
            return prepared
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            _emit(status, "error", str(exc), source_id=source_id)
            if isinstance(exc, SourceError):
                raise
            raise SourceError(f"source preparation failed: {exc}") from exc


def prepare_source(*args: Any, **kwargs: Any) -> PreparedSource:
    """Convenience wrapper used by small CLI callers."""

    return SourceManager(kwargs.pop("root", "/var/lib/owrt-builder/sources")).prepare_source(
        *args, **kwargs
    )


__all__ = [
    "DEVICE_SPECS",
    "DownloadSeed",
    "SOURCE_SPECS",
    "DeviceSpec",
    "PreparedSource",
    "PREPARATION_VERSION",
    "SourceError",
    "SourceManager",
    "SourceSpec",
    "stage_download_seeds",
    "validate_download_seeds",
    "prepare_source",
    "resolve_device",
]
