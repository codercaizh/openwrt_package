"""Persistent, device-scoped OpenWrt compiler caches.

The source snapshot is immutable, but an OpenWrt build tree is deliberately
mutable and expensive to recreate.  This module keeps one current build tree
per canonical device, records its provenance atomically, serializes same
device access, and evicts only old compiler caches when the build filesystem
does not have enough room for a new tree.

Downloads, source snapshots, task evidence, logs and artifacts live outside
the cache root and are never candidates for eviction here.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
from pathlib import PurePosixPath
import stat as stat_module
import time
import uuid
from typing import Any, Callable, Iterable, Iterator, Mapping

from .sources import SourceError, stage_download_seeds


LogCallback = Callable[[str], None]

# A first build has no local sample from which to infer the size of the
# compiler tree.  Eight GiB is intentionally conservative for a small
# personal builder while still allowing the caller to continue with a warning
# on a smaller filesystem, as documented by ``ensure_capacity``.
DEFAULT_BUILD_ESTIMATE_BYTES = 8 * 1024**3
CACHE_SCHEMA_VERSION = 1
_DEVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")


class BuildCacheError(RuntimeError):
    """The device compiler cache cannot be acquired safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_device(value: str) -> str:
    value = str(value)
    if not _DEVICE_RE.fullmatch(value):
        raise ValueError(f"invalid build cache device: {value!r}")
    return value


def _safe_task(value: str) -> str:
    value = str(value)
    if not _DEVICE_RE.fullmatch(value):
        raise ValueError(f"invalid build cache task: {value!r}")
    return value


def _json_write_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
        except (AttributeError, OSError):
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_tree(path: Path) -> None:
    """Remove a cache entry without ever following a top-level symlink."""

    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _tree_size(path: Path) -> int:
    """Return filesystem blocks occupied by a cache entry.

    ``st_size`` is a logical byte count and substantially underestimates an
    OpenWrt tree containing many small files.  ``st_blocks`` is the POSIX
    allocation count exposed by Python; count it for directories and
    symlinks too, while never following a symlink into the download cache.
    """

    if not (path.exists() or path.is_symlink()):
        return 0

    total = 0
    seen_inodes: set[tuple[int, int]] = set()
    pending = [path]
    while pending:
        item = pending.pop()
        try:
            item_stat = item.lstat()
        except OSError:
            continue
        inode = (int(getattr(item_stat, "st_dev", 0)), int(getattr(item_stat, "st_ino", 0)))
        if inode in seen_inodes:
            continue
        seen_inodes.add(inode)
        if hasattr(item_stat, "st_blocks"):
            total += int(item_stat.st_blocks) * 512
        else:
            total += int(item_stat.st_size)
        if not stat_module.S_ISDIR(item_stat.st_mode) or stat_module.S_ISLNK(item_stat.st_mode):
            continue
        try:
            with os.scandir(item) as entries:
                pending.extend(Path(entry.path) for entry in entries)
        except OSError:
            continue
    return total


@dataclass(frozen=True)
class BuildCacheEntry:
    device: str
    path: Path
    metadata_path: Path
    created_at: str
    last_used_at: str
    size_bytes: int
    source_id: str = ""
    snapshot_id: str = ""
    source_commit: str = ""
    config_sha256: str = ""
    last_task_id: str = ""
    ready: bool = False
    schema_version: int = 0
    # Legacy entries are task-id build directories from releases before the
    # device cache was introduced.  Their ``path`` is the task build dir;
    # only ``path/openwrt`` is compiler cache and may be removed.
    legacy: bool = False
    origin_task_id: str = ""

    @property
    def fifo_key(self) -> tuple[str, str]:
        # This is deliberately FIFO.  Cache refreshes may update
        # ``last_used_at`` for observability, but that timestamp must never
        # turn an old entry into an LRU entry.  The path is only a stable tie
        # breaker when two entries were published in the same second.
        return (
            self.created_at,
            f"{self.device}:{self.path}",
        )


@dataclass
class BuildCacheLease:
    manager: "BuildCacheManager"
    device: str
    task_id: str
    root: Path
    reused: bool
    previous: BuildCacheEntry | None
    _lock_handle: Any
    _lock_context: Any = None
    _released: bool = False

    @property
    def source_root(self) -> Path:
        return self.root / "openwrt"

    @property
    def metadata_path(self) -> Path:
        return self.root / "cache.json"

    def __enter__(self) -> "BuildCacheLease":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.manager._release(self)


class BuildCacheManager:
    """Manage the current compiler cache for each canonical device."""

    def __init__(
        self,
        workspace: str | os.PathLike[str],
        *,
        default_estimate_bytes: int = DEFAULT_BUILD_ESTIMATE_BYTES,
        device_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.cache_root = self.workspace / "cache" / "builds"
        self.lock_root = self.workspace / "cache" / "locks"
        self.metadata_lock_path = self.workspace / "cache" / ".metadata.lock"
        self.eviction_lock_path = self.workspace / "cache" / ".eviction.lock"
        self.active_path = self.workspace / "cache" / "active.json"
        self.default_estimate_bytes = max(1, int(default_estimate_bytes))
        self.device_resolver = device_resolver

    @property
    def download_cache(self) -> Path:
        return self.workspace / "cache" / "dl"

    def _canonical_device(self, value: str) -> str:
        raw = str(value).strip()
        if self.device_resolver is not None:
            raw = str(self.device_resolver(raw)).strip()
        return _safe_device(raw)

    def _try_canonical_device(self, value: str) -> str | None:
        try:
            return self._canonical_device(value)
        except (KeyError, TypeError, ValueError):
            return None

    def _entry_path(self, device: str) -> Path:
        return self.cache_root / self._canonical_device(device)

    def _device_lock_path(self, device: str) -> Path:
        return self.lock_root / f"{self._canonical_device(device)}.lock"

    @contextmanager
    def _device_lock(self, device: str, *, timeout: float = 0.0) -> Iterator[Any]:
        """Serialize a device and provide a lock usable by eviction too."""

        with self._locked_file(self._device_lock_path(device), timeout=timeout) as handle:
            yield handle

    @contextmanager
    def _locked_file(self, path: Path, *, timeout: float = 0.0) -> Iterator[Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+")
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BuildCacheError(f"build cache lock is busy: {path}")
                    time.sleep(min(0.25, max(0.01, deadline - time.monotonic())))
            yield handle
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _read_active(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.active_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        if not isinstance(value, Mapping):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for task, record in value.items():
            if not isinstance(record, Mapping):
                continue
            try:
                _safe_task(str(task))
                device = self._try_canonical_device(str(record.get("device", "")))
                pid = int(record.get("pid", 0))
            except (TypeError, ValueError):
                continue
            if device is None:
                continue
            # A process that died before releasing its lease must not pin a
            # cache forever.  PID 0 is treated as stale (and is useful in
            # deterministic tests).
            if pid <= 0:
                continue
            try:
                os.kill(pid, 0)
            except OSError:
                continue
            result[str(task)] = {
                "device": device,
                "pid": pid,
                "started_at": str(record.get("started_at", "")),
                "path": str(record.get("path", "")),
            }
        return result

    def _with_metadata_lock(self) -> Any:
        return self._locked_file(self.metadata_lock_path)

    def _mark_active(self, device: str, task_id: str) -> None:
        with self._with_metadata_lock():
            active = self._read_active()
            active[task_id] = {
                "device": device,
                "pid": os.getpid(),
                "started_at": _utc_now(),
                "path": str(self._entry_path(device)),
            }
            _json_write_atomic(self.active_path, active)

    def _unmark_active(self, task_id: str) -> None:
        with self._with_metadata_lock():
            active = self._read_active()
            if task_id in active:
                active.pop(task_id, None)
                _json_write_atomic(self.active_path, active)

    def _active_devices(self) -> set[str]:
        with self._with_metadata_lock():
            return {str(item["device"]) for item in self._read_active().values()}

    @staticmethod
    def _read_json(path: Path) -> Mapping[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _legacy_device_names(value: Any) -> set[str]:
        if not isinstance(value, str):
            return set()
        text = value.strip().lower()
        return {text, text.replace("-", "_")}

    def _legacy_manifest(self, task_id: str) -> Mapping[str, Any]:
        """Read final task evidence without opening the large build tree."""

        task_dir = self.workspace / "tasks" / task_id
        result = self._read_json(task_dir / "result.json")
        if result:
            return result
        # A manifest is written immediately before result.json.  It is useful
        # for discovering an interrupted old task, but its final status is
        # still checked below before we consider the tree safe to adopt.
        artifacts = self.workspace / "artifacts"
        try:
            candidates = artifacts.glob(f"*/{task_id}/manifest.json")
            for path in candidates:
                value = self._read_json(path)
                if value:
                    return value
        except OSError:
            pass
        return {}

    def _legacy_entry_from_path(
        self,
        path: Path,
        *,
        callback: LogCallback | None = None,
        with_size: bool = True,
    ) -> BuildCacheEntry | None:
        """Describe a pre-device-cache build tree when its task is finished.

        A task directory with no final result/manifest may still be running;
        it is intentionally excluded and never deleted.  This makes legacy
        migration conservative even when the old process did not maintain an
        active registry.
        """

        if not path.is_dir() or path.is_symlink():
            return None
        source_tree = path / "openwrt"
        if not source_tree.is_dir() or source_tree.is_symlink():
            return None
        task_id = path.name
        evidence = self._legacy_manifest(task_id)
        if not evidence:
            if callback:
                callback(f"跳过无完成证据的旧编译目录: {path}")
            return None
        status = str(evidence.get("status", "")).lower()
        if status not in {"success", "succeeded", "failed", "cancelled", "canceled"}:
            if callback:
                callback(f"跳过状态不安全的旧编译缓存: {path} status={status}")
            return None

        device_value = evidence.get("device")
        if isinstance(device_value, Mapping):
            device_value = device_value.get("key")
        if not isinstance(device_value, str) or not device_value.strip():
            request = self._read_json(self.workspace / "tasks" / task_id / "request.json")
            device_value = request.get("device")
        if not isinstance(device_value, str) or not device_value.strip():
            if callback:
                callback(f"跳过无法识别设备的旧编译缓存: {path}")
            return None
        device = self._try_canonical_device(device_value.strip())
        if device is None:
            if callback:
                callback(f"跳过设备名不安全的旧编译缓存: {path}")
            return None

        declared_schema = evidence.get("cache_schema_version")
        if declared_schema is not None:
            try:
                if int(declared_schema) != CACHE_SCHEMA_VERSION:
                    if callback:
                        callback(f"跳过旧 schema 的 legacy 编译缓存: {path}")
                    return None
            except (TypeError, ValueError):
                if callback:
                    callback(f"跳过无效 schema 的 legacy 编译缓存: {path}")
                return None

        source = evidence.get("source")
        source = source if isinstance(source, Mapping) else {}
        source_id = str(evidence.get("source_id") or source.get("source_id") or source.get("id") or "")
        snapshot_id = str(evidence.get("snapshot_id") or source.get("snapshot_id") or "")
        source_commit = str(evidence.get("source_commit") or source.get("source_commit") or "")
        config = evidence.get("config")
        config = config if isinstance(config, Mapping) else {}
        config_sha256 = str(evidence.get("config_sha256") or config.get("sha256") or "")
        created_at = str(evidence.get("started_at") or "")
        last_used_at = str(evidence.get("finished_at") or evidence.get("updated_at") or "")
        try:
            stat = path.stat()
            fallback_time = datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat()
        except OSError:
            fallback_time = ""
        created_at = created_at or fallback_time
        last_used_at = last_used_at or created_at
        return BuildCacheEntry(
            device=device,
            path=path,
            metadata_path=self.workspace / "tasks" / task_id / "result.json",
            created_at=created_at,
            last_used_at=last_used_at,
            size_bytes=_tree_size(source_tree) if with_size else 0,
            source_id=source_id,
            snapshot_id=snapshot_id,
            source_commit=source_commit,
            config_sha256=config_sha256,
            last_task_id=task_id,
            ready=True,
            legacy=True,
            origin_task_id=task_id,
        )

    def _entry_from_path(self, path: Path) -> BuildCacheEntry | None:
        if not path.is_dir() or path.is_symlink():
            return None
        device = self._try_canonical_device(path.name)
        if device is None:
            return None
        metadata_path = path / "cache.json"
        value: Mapping[str, Any] = {}
        try:
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(raw, Mapping):
                value = raw
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        try:
            created_at = str(value.get("created_at") or datetime.fromtimestamp(path.stat().st_ctime, timezone.utc).isoformat())
        except OSError:
            created_at = ""
        last_used_at = str(value.get("last_used_at") or created_at)
        declared_schema = value.get("schema_version")
        try:
            schema_version = int(declared_schema)
        except (TypeError, ValueError):
            schema_version = 0
        metadata_device = value.get("device")
        metadata_canonical = (
            self._try_canonical_device(str(metadata_device))
            if isinstance(metadata_device, str) and metadata_device.strip()
            else None
        )
        schema_valid = schema_version == CACHE_SCHEMA_VERSION and metadata_canonical == device
        return BuildCacheEntry(
            device=device,
            path=path,
            metadata_path=metadata_path,
            created_at=created_at,
            last_used_at=last_used_at,
            size_bytes=_tree_size(path),
            source_id=str(value.get("source_id", "")),
            snapshot_id=str(value.get("snapshot_id", "")),
            source_commit=str(value.get("source_commit", "")),
            config_sha256=str(value.get("config_sha256", "")),
            last_task_id=str(value.get("last_task_id", "")),
            ready=bool(value.get("ready", False)) and schema_valid,
            schema_version=schema_version,
            legacy=False,
            origin_task_id=str(value.get("origin_task_id", "")),
        )

    def entries(
        self,
        *,
        include_legacy: bool = True,
        callback: LogCallback | None = None,
        with_legacy_size: bool = True,
    ) -> list[BuildCacheEntry]:
        result: list[BuildCacheEntry] = []
        if self.cache_root.is_dir():
            for path in self.cache_root.iterdir():
                entry = self._entry_from_path(path)
                if entry is not None:
                    result.append(entry)
        if include_legacy:
            builds = self.workspace / "builds"
            if builds.is_dir():
                for path in builds.iterdir():
                    entry = self._legacy_entry_from_path(
                        path,
                        callback=callback,
                        with_size=with_legacy_size,
                    )
                    if entry is not None:
                        result.append(entry)
        return result

    def estimate_required_bytes(
        self,
        device: str | None = None,
        *,
        aliases: Iterable[str] = (),
    ) -> int:
        """Estimate a fresh tree from actual local cache occupancy.

        A completed cache for the requested device is the best predictor.  If
        it does not exist, use the largest known compiler tree (a conservative
        cross-device/platform fallback) before falling back to the documented
        first-build estimate.
        """

        entries = self.entries()
        same_device: set[str] = set()
        if device is not None:
            for item in (device, *aliases):
                canonical = self._try_canonical_device(str(item))
                if canonical is not None:
                    same_device.add(canonical.lower())
        selected = [entry for entry in entries if entry.device.lower() in same_device]
        if not selected:
            selected = entries
        sizes = [entry.size_bytes for entry in selected if entry.size_bytes > 0]
        if not sizes:
            return self.default_estimate_bytes
        # Add 25% headroom for a config that enables a larger package set than
        # the existing sample.  The configured first-build estimate remains a
        # floor for unusually small test/dev trees.
        return max(self.default_estimate_bytes, int(max(sizes) * 1.25))

    def _disk_free(self) -> int:
        # ``cache_root`` is created on the same filesystem as the eventual
        # build tree; do not accidentally measure the source or artifact mount.
        self.cache_root.mkdir(parents=True, exist_ok=True)
        try:
            return max(0, int(shutil.disk_usage(self.cache_root).free))
        except OSError:
            # Keep a direct POSIX fallback for unusual filesystems where
            # shutil.disk_usage cannot stat the mounted cache path.
            stats = os.statvfs(self.cache_root)
            block_size = int(stats.f_frsize or stats.f_bsize or 1)
            return max(0, int(stats.f_bavail) * block_size)

    def ensure_capacity(
        self,
        required_bytes: int,
        *,
        protected_devices: set[str] | None = None,
        callback: LogCallback | None = None,
    ) -> bool:
        """Evict idle cache entries until ``required_bytes`` is available.

        A false return is deliberately non-fatal: a filesystem may report
        less free space than the estimate while the compiler can still fit.
        The warning is emitted and the caller proceeds, as requested.
        """

        required = max(1, int(required_bytes))
        protected = {
            canonical
            for item in (protected_devices or set())
            if (canonical := self._try_canonical_device(str(item))) is not None
        }
        # The workspace lock currently serializes normal builds, but this
        # second lock keeps eviction correct for direct users and future
        # per-device parallelism.  A candidate's device lock is held again
        # while that candidate is removed; active.json is only advisory.
        with self._locked_file(self.eviction_lock_path, timeout=30.0):
            free = self._disk_free()
            if free >= required:
                return True
            entries = sorted(
                (
                    entry
                    for entry in self.entries(callback=callback)
                    if entry.device not in protected
                ),
                key=lambda entry: entry.fifo_key,
            )
            for entry in entries:
                if free >= required:
                    break
                try:
                    candidate_lock = self._device_lock(entry.device, timeout=0.0)
                    candidate_lock.__enter__()
                except BuildCacheError:
                    if callback:
                        callback(f"跳过正在使用的编译缓存（设备锁忙）: {entry.path}")
                    continue
                try:
                    # Re-read the entry after obtaining its lock.  Another
                    # process may have refreshed or removed it since the FIFO
                    # snapshot was taken.
                    current = (
                        self._entry_from_path(entry.path)
                        if not entry.legacy
                        else self._legacy_entry_from_path(entry.path, callback=callback)
                    )
                    if current is None:
                        continue
                    before = free
                    size = current.size_bytes
                    removable = current.path / "openwrt" if current.legacy else current.path
                    _remove_tree(removable)
                    free = self._disk_free()
                    if callback:
                        callback(
                            "删除编译缓存: "
                            f"{removable} size={size} bytes, "
                            f"free_before={before} bytes, free_after={free} bytes, "
                            f"estimated_need={required} bytes"
                        )
                finally:
                    candidate_lock.__exit__(None, None, None)
        if free < required:
            if callback:
                callback(
                    "警告: 编译缓存空间估算仍不足，继续尝试构建: "
                    f"free={free} bytes, estimated_need={required} bytes"
                )
            return False
        return True

    def _legacy_candidate(
        self,
        device: str,
        aliases: set[str],
        source_id: str,
        snapshot_id: str,
        *,
        callback: LogCallback | None = None,
    ) -> BuildCacheEntry | None:
        builds = self.workspace / "builds"
        if not builds.is_dir():
            return None
        names = {
            canonical.lower()
            for item in (device, *aliases)
            if (canonical := self._try_canonical_device(str(item))) is not None
        }
        candidates: list[BuildCacheEntry] = []
        for path in builds.iterdir():
            entry = self._legacy_entry_from_path(path, callback=callback, with_size=False)
            if entry is None:
                continue
            entry_device = self._try_canonical_device(entry.device)
            if entry_device is None or entry_device.lower() not in names:
                continue
            if entry.source_id != source_id or entry.snapshot_id != snapshot_id:
                continue
            if not self._legacy_links_safe(path / "openwrt", path.name, callback=callback):
                continue
            candidates.append(entry)
        # Multiple old task trees can describe the same snapshot.  Reuse the
        # newest completed tree; FIFO ordering is reserved for eviction.
        return max(
            candidates,
            key=lambda item: (
                item.last_used_at or item.created_at,
                item.created_at,
                str(item.path),
            ),
        ) if candidates else None

    def _legacy_links_safe(
        self,
        source_tree: Path,
        task_id: str,
        *,
        callback: LogCallback | None = None,
    ) -> bool:
        """Reject links whose old container target cannot be rewritten safely."""

        old_prefix = f"/workspace/work/builds/{task_id}/openwrt"
        old_cache = "/workspace/work/cache/dl"
        try:
            walker = os.walk(source_tree, followlinks=False)
            for root, dirs, files in walker:
                for name in (*dirs, *files):
                    link = Path(root) / name
                    if not link.is_symlink():
                        continue
                    target = os.readlink(link)
                    if not os.path.isabs(target):
                        resolved = (link.parent / target).resolve(strict=False)
                        if source_tree.resolve() not in (resolved, *resolved.parents):
                            if callback:
                                callback(
                                    f"跳过不安全 legacy cache（相对链接越界）: {link} -> {target}"
                                )
                            return False
                        continue
                    if target == old_cache:
                        continue
                    if target.startswith(old_prefix + "/"):
                        suffix = target[len(old_prefix) + 1 :]
                        relative = PurePosixPath(suffix)
                        if (
                            relative.is_absolute()
                            or not relative.parts
                            or any(part in {"", ".", ".."} for part in relative.parts)
                        ):
                            if callback:
                                callback(
                                    f"跳过不安全 legacy cache（旧源码链接越界）: {link} -> {target}"
                                )
                            return False
                        resolved = (source_tree / Path(*relative.parts)).resolve(strict=False)
                        source_root = source_tree.resolve()
                        if resolved != source_root and source_root not in resolved.parents:
                            if callback:
                                callback(
                                    f"跳过不安全 legacy cache（旧源码链接越界）: {link} -> {target}"
                                )
                            return False
                        continue
                    # These links are generated inside the reviewed builder
                    # image (tool aliases, procfs and resolver/timezone
                    # links).  They do not escape into the host workspace and
                    # remain valid when the migrated cache is opened by the
                    # same image.  Arbitrary paths such as /work/package are
                    # still rejected below.
                    if (
                        target == "/proc/mounts"
                        or target in {"/sbin/kmodloader", "/tmp/TZ", "/tmp/localtime"}
                        or target.startswith((
                            "/usr/",
                            "/bin/",
                            "/sbin/",
                            "/lib/",
                            "/lib64/",
                            "/etc/",
                            "/proc/",
                            "/tmp/resolv.conf",
                        ))
                    ):
                        continue
                    if callback:
                        callback(f"跳过不安全 legacy cache（未知绝对链接）: {link} -> {target}")
                    return False
        except OSError as exc:
            if callback:
                callback(f"跳过不安全 legacy cache（链接检查失败）: {source_tree}: {exc}")
            return False
        return True

    def _repair_legacy_links(
        self,
        source_tree: Path,
        destination_tree: Path,
        task_id: str,
    ) -> None:
        old_prefix = f"/workspace/work/builds/{task_id}/openwrt"
        old_cache = "/workspace/work/cache/dl"
        for root, dirs, files in os.walk(destination_tree, followlinks=False):
            for name in (*dirs, *files):
                link = Path(root) / name
                if not link.is_symlink():
                    continue
                target = os.readlink(link)
                mapped: str | None = None
                if target == old_cache:
                    mapped = str(self.download_cache)
                elif target.startswith(old_prefix + "/"):
                    suffix = target[len(old_prefix) + 1 :]
                    relative = PurePosixPath(suffix)
                    if (
                        relative.is_absolute()
                        or not relative.parts
                        or any(part in {"", ".", ".."} for part in relative.parts)
                    ):
                        continue
                    mapped = str(destination_tree / Path(*relative.parts))
                if mapped is None:
                    continue
                link.unlink()
                link.symlink_to(os.path.relpath(mapped, link.parent))

    def _adopt_legacy(
        self,
        entry: BuildCacheEntry,
        root: Path,
        *,
        source_id: str,
        snapshot_id: str,
        task_id: str,
        callback: LogCallback | None = None,
    ) -> BuildCacheEntry:
        source_tree = entry.path / "openwrt"
        if not source_tree.is_dir() or source_tree.is_symlink():
            raise BuildCacheError(f"legacy compiler tree disappeared: {source_tree}")
        if not self._legacy_links_safe(source_tree, entry.path.name, callback=callback):
            raise BuildCacheError(f"legacy compiler tree has unsafe links: {source_tree}")
        if root.exists() or root.is_symlink():
            _remove_tree(root)
        root.mkdir(parents=True, exist_ok=True)
        destination = root / "openwrt"
        # Both paths are in the workspace filesystem, so this is an atomic
        # rename of the large tree rather than a second multi-gigabyte copy.
        os.replace(source_tree, destination)
        self._repair_legacy_links(source_tree, destination, entry.path.name)
        self._write_metadata(
            root,
            device=root.name,
            source_id=source_id,
            snapshot_id=snapshot_id,
            source_commit=entry.source_commit,
            config_sha256=entry.config_sha256,
            task_id=task_id,
            created_at=entry.created_at,
            ready=True,
            origin_task_id=entry.origin_task_id or entry.path.name,
        )
        if callback:
            callback(
                "迁移 legacy 编译缓存（原地移动，不复制源码树）: "
                f"{entry.path / 'openwrt'} -> {destination}; "
                f"source={source_id} snapshot={snapshot_id}"
            )
        migrated = self._entry_from_path(root)
        if migrated is None:
            raise BuildCacheError(f"migrated compiler cache metadata missing: {root}")
        return migrated

    def _remove_same_device_legacy(
        self,
        device: str,
        aliases: set[str],
        *,
        callback: LogCallback | None = None,
        estimated_need: int,
        held_device: str | None = None,
    ) -> None:
        names = {
            canonical.lower()
            for item in (device, *aliases)
            if (canonical := self._try_canonical_device(str(item))) is not None
        }
        held = self._try_canonical_device(held_device) if held_device is not None else None
        builds = self.workspace / "builds"
        if not builds.is_dir():
            return
        for path in builds.iterdir():
            entry = self._legacy_entry_from_path(path, callback=callback)
            entry_device = self._try_canonical_device(entry.device) if entry is not None else None
            if entry is None or entry_device is None or entry_device.lower() not in names:
                continue
            # The current task holds the canonical device lock.  An old task
            # with final evidence is not active, so its compiler subtree can
            # be reclaimed while preserving the task log/config evidence.
            lock = None
            if entry_device != held:
                lock = self._device_lock(entry_device, timeout=0.0)
                try:
                    lock.__enter__()
                except BuildCacheError:
                    if callback:
                        callback(f"跳过正在使用的旧编译缓存（设备锁忙）: {path}")
                    continue
            try:
                before = self._disk_free()
                size = entry.size_bytes
                _remove_tree(path / "openwrt")
                after = self._disk_free()
                if callback:
                    callback(
                        "删除当前设备旧编译缓存: "
                        f"{path / 'openwrt'} size={size} bytes, "
                        f"free_before={before} bytes, free_after={after} bytes, "
                        f"estimated_need={estimated_need} bytes"
                    )
            finally:
                if lock is not None:
                    lock.__exit__(None, None, None)

    def acquire(
        self,
        device: str,
        task_id: str,
        *,
        source_id: str,
        snapshot_id: str,
        reuse: bool = True,
        legacy_aliases: Iterable[str] = (),
        callback: LogCallback | None = None,
        lock_timeout: float = 0.0,
    ) -> BuildCacheLease:
        """Acquire a device cache and reserve it from eviction."""

        device = self._canonical_device(device)
        task_id = _safe_task(task_id)
        aliases = {
            canonical
            for item in legacy_aliases
            if (canonical := self._try_canonical_device(str(item).strip())) is not None
        }
        aliases.update({device, device.replace("-", "_")})
        try:
            device_lock = self._device_lock(device, timeout=lock_timeout)
            lock_handle = device_lock.__enter__()
            self.cache_root.mkdir(parents=True, exist_ok=True)
            self._mark_active(device, task_id)
            root = self._entry_path(device)
            previous = self._entry_from_path(root)
            matching = bool(
                previous
                and previous.ready
                and (root / "openwrt").is_dir()
                and previous.source_id == source_id
            )
            reused = bool(reuse and matching)
            if reuse and not reused:
                legacy = self._legacy_candidate(
                    device,
                    aliases,
                    source_id,
                    snapshot_id,
                    callback=callback,
                )
                if legacy is not None:
                    previous = self._adopt_legacy(
                        legacy,
                        root,
                        source_id=source_id,
                        snapshot_id=snapshot_id,
                        task_id=task_id,
                        callback=callback,
                    )
                    reused = True
            if reused:
                if callback:
                    if previous and previous.snapshot_id and previous.snapshot_id != snapshot_id:
                        callback(
                            f"复用 {device} 编译缓存并准备更新源码快照: "
                            f"{previous.snapshot_id} -> {snapshot_id}; {root}"
                        )
                    else:
                        callback(f"复用 {device} 编译缓存: {root}")
                self.touch(
                    device,
                    task_id=task_id,
                    source_id=source_id,
                    snapshot_id=previous.snapshot_id if previous and previous.snapshot_id else snapshot_id,
                    source_commit=previous.source_commit if previous else "",
                    config_sha256=previous.config_sha256 if previous else "",
                    ready=True,
                )
            else:
                # Compute the estimate before removing the old same-device
                # tree, so a large real cache remains the most useful sample.
                required = self.estimate_required_bytes(device, aliases=aliases)
                self._remove_same_device_legacy(
                    device,
                    aliases,
                    callback=callback,
                    estimated_need=required,
                    held_device=device,
                )
                if root.exists() or root.is_symlink():
                    before = self._disk_free()
                    size = _tree_size(root)
                    reason = "未启用缓存复用" if not reuse else "源码来源已变化或缓存元数据不匹配"
                    _remove_tree(root)
                    after = self._disk_free()
                    if callback:
                        callback(
                            "删除当前设备旧编译缓存: "
                            f"{root} ({reason}) size={size} bytes, "
                            f"free_before={before} bytes, free_after={after} bytes, "
                            f"estimated_need={required} bytes"
                        )
                self.ensure_capacity(
                    required,
                    protected_devices={device, *aliases},
                    callback=callback,
                )
                root.mkdir(parents=True, exist_ok=True)
                if callback:
                    callback(f"创建 {device} 编译缓存: {root}; estimated_need={required} bytes")
                self.touch(
                    device,
                    task_id=task_id,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    source_commit="",
                    config_sha256="",
                    ready=False,
                )
            return BuildCacheLease(
                self,
                device,
                task_id,
                root,
                reused,
                previous,
                lock_handle,
                device_lock,
            )
        except Exception:
            try:
                self._unmark_active(task_id)
            except Exception:
                pass
            if "device_lock" in locals():
                try:
                    device_lock.__exit__(None, None, None)
                except Exception:
                    pass
            raise

    def _write_metadata(
        self,
        root: Path,
        *,
        device: str,
        source_id: str,
        snapshot_id: str,
        source_commit: str,
        config_sha256: str,
        task_id: str,
        created_at: str | None = None,
        ready: bool = False,
        origin_task_id: str = "",
    ) -> None:
        existing: dict[str, Any] = {}
        try:
            value = json.loads((root / "cache.json").read_text(encoding="utf-8"))
            if isinstance(value, Mapping):
                existing = dict(value)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        now = _utc_now()
        created = str(created_at or existing.get("created_at") or now)
        _json_write_atomic(
            root / "cache.json",
            {
                "schema_version": CACHE_SCHEMA_VERSION,
                "device": device,
                "source_id": source_id,
                "snapshot_id": snapshot_id,
                "source_commit": source_commit or str(existing.get("source_commit", "")),
                "config_sha256": config_sha256 or str(existing.get("config_sha256", "")),
                "created_at": created,
                "last_used_at": now,
                "last_task_id": task_id,
                "ready": bool(ready),
                "origin_task_id": origin_task_id or str(existing.get("origin_task_id", "")),
            },
        )

    def touch(
        self,
        device: str,
        *,
        task_id: str,
        source_id: str,
        snapshot_id: str,
        source_commit: str,
        config_sha256: str,
        ready: bool | None = None,
    ) -> None:
        device = self._canonical_device(device)
        task_id = _safe_task(task_id)
        root = self._entry_path(device)
        existing: dict[str, Any] = {}
        try:
            value = json.loads((root / "cache.json").read_text(encoding="utf-8"))
            if isinstance(value, Mapping):
                existing = dict(value)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        self._write_metadata(
            root,
            device=device,
            source_id=source_id,
            snapshot_id=snapshot_id,
            source_commit=source_commit,
            config_sha256=config_sha256,
            task_id=task_id,
            ready=bool(existing.get("ready", False) if ready is None else ready),
            origin_task_id=str(existing.get("origin_task_id", "")),
        )

    def update_source(
        self,
        lease: BuildCacheLease,
        *,
        source_id: str,
        snapshot_id: str,
        source_commit: str,
        config_sha256: str,
        ready: bool = True,
    ) -> None:
        self.touch(
            lease.device,
            task_id=lease.task_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            source_commit=source_commit,
            config_sha256=config_sha256,
            ready=ready,
        )

    def _ensure_download_link(self, destination: Path) -> None:
        """Point a cached source tree at the workspace download cache.

        Older worker containers used an absolute ``/workspace/work`` target,
        which is a dangling link when inspected on the host.  Always publish
        a relative link so the cache remains valid in both host and worker
        mount namespaces.  If a legacy tree contains a real ``dl`` directory,
        move its files into the shared cache before replacing it.
        """

        download_cache = self._ensure_download_cache_directory()
        dl = destination / "dl"
        if dl.is_symlink():
            try:
                resolved = dl.resolve(strict=False)
            except OSError:
                resolved = Path()
            try:
                raw_target = Path(os.readlink(dl))
            except OSError:
                raw_target = Path()
            if resolved == download_cache.resolve() and not raw_target.is_absolute():
                return
            dl.unlink(missing_ok=True)
        elif dl.is_dir():
            for item in list(dl.iterdir()):
                target = download_cache / item.name
                if target.is_symlink():
                    _remove_tree(target)
                # A legacy ``dl`` directory is untrusted mutable state.  In
                # particular, moving a symlink here would turn it into a
                # shared-cache entry and make later workers follow an
                # attacker-controlled target.  Keep only regular files.
                if item.is_symlink() or not item.is_file():
                    _remove_tree(item)
                    continue
                if target.exists() or target.is_symlink():
                    if target.is_dir() or not target.is_file():
                        raise BuildCacheError(
                            f"download cache entry is not a regular file: {target}"
                        )
                    _remove_tree(item)
                else:
                    os.replace(item, target)
            dl.rmdir()
        elif dl.exists():
            dl.unlink(missing_ok=True)
        dl.symlink_to(os.path.relpath(download_cache, destination))

    def _ensure_download_cache_directory(self) -> Path:
        """Create the shared download root only when it is a real directory."""

        download_cache = self.download_cache
        cache_parent = download_cache.parent
        if cache_parent.is_symlink():
            raise BuildCacheError(f"download cache parent must not be a symlink: {cache_parent}")
        if cache_parent.exists() and not cache_parent.is_dir():
            raise BuildCacheError(f"download cache parent must be a directory: {cache_parent}")
        if download_cache.is_symlink():
            raise BuildCacheError(f"download cache must not be a symlink: {download_cache}")
        if download_cache.exists() and not download_cache.is_dir():
            raise BuildCacheError(f"download cache must be a directory: {download_cache}")
        cache_parent.mkdir(parents=True, exist_ok=True)
        if cache_parent.is_symlink() or not cache_parent.is_dir():
            raise BuildCacheError(f"download cache parent must be a real directory: {cache_parent}")
        download_cache.mkdir(parents=True, exist_ok=True)
        if download_cache.is_symlink() or not download_cache.is_dir():
            raise BuildCacheError(f"download cache must be a real directory: {download_cache}")
        return download_cache

    def refresh_source(
        self,
        lease: BuildCacheLease,
        snapshot_path: Path,
        *,
        source_id: str,
        snapshot_id: str,
        source_commit: str,
        callback: LogCallback | None = None,
    ) -> None:
        """Refresh source files while retaining OpenWrt compiler state.

        ``build_dir``, ``staging_dir`` and the shared ``dl`` symlink contain
        expensive incremental state.  All other source files are replaced by
        the immutable prepared snapshot, which also removes files deleted by
        an upstream update.  The device lock is held by ``lease`` throughout.
        """

        if not snapshot_path.is_dir():
            raise BuildCacheError(f"prepared source path missing: {snapshot_path}")
        destination = lease.source_root
        if not destination.is_dir() or destination.is_symlink():
            raise BuildCacheError(f"cached source tree missing: {destination}")
        try:
            stage_download_seeds(snapshot_path, self.download_cache)
        except SourceError as exc:
            raise BuildCacheError(f"invalid tracked download seed: {exc}") from exc
        self.touch(
            lease.device,
            task_id=lease.task_id,
            source_id=source_id,
            snapshot_id=lease.previous.snapshot_id if lease.previous else "",
            source_commit=lease.previous.source_commit if lease.previous else "",
            config_sha256=lease.previous.config_sha256 if lease.previous else "",
            ready=False,
        )
        preserve = {"build_dir", "staging_dir", "dl"}
        if callback:
            callback(
                f"更新 {lease.device} 编译缓存源码: "
                f"{lease.previous.snapshot_id if lease.previous else ''} -> {snapshot_id}；"
                "保留 build_dir/staging_dir/dl"
            )
        for item in list(destination.iterdir()):
            if item.name in preserve:
                continue
            _remove_tree(item)
        for item in snapshot_path.iterdir():
            if item.name in preserve:
                continue
            target = destination / item.name
            if item.is_dir() and not item.is_symlink():
                shutil.copytree(item, target, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target, follow_symlinks=False)
        self._ensure_download_link(destination)
        self.touch(
            lease.device,
            task_id=lease.task_id,
            source_id=source_id,
            snapshot_id=snapshot_id,
            source_commit=source_commit,
            config_sha256=lease.previous.config_sha256 if lease.previous else "",
            ready=False,
        )

    def _release(self, lease: BuildCacheLease) -> None:
        try:
            self._unmark_active(lease.task_id)
        finally:
            if lease._lock_context is not None:
                lease._lock_context.__exit__(None, None, None)
            else:
                try:
                    fcntl.flock(lease._lock_handle.fileno(), fcntl.LOCK_UN)
                finally:
                    lease._lock_handle.close()


__all__ = [
    "BuildCacheEntry",
    "BuildCacheError",
    "BuildCacheLease",
    "BuildCacheManager",
    "CACHE_SCHEMA_VERSION",
    "DEFAULT_BUILD_ESTIMATE_BYTES",
]
