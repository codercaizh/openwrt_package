"""FastAPI control plane for the local OpenWrt builder.

The application owns authentication, persistence, the serialized worker queue
and the browser API.  Source preparation, catalog scanning, configuration
validation and compilation remain in the typed core modules.  ``create_app``
accepts an injected ``Runtime`` so the HTTP/security tests can use deterministic
fakes without making a source checkout or starting Docker.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator

from .auth import (
    AuthManager,
    hash_password,
    settings_from_env,
    validate_username,
    verify_password,
)
from .build import BuildEngine, BuildRequest, logical_cpu_count
from .catalog import Catalog, scan_catalog
from .configuration import parse_config
from .devices import DeviceCatalog, DeviceSpec, load_catalog
from .notifications import NotificationSettings, PushPlusNotifier
from .sources import PreparedSource, SourceError, SourceManager
from .storage import Storage, utc_now


LOGGER = logging.getLogger("owrt_builder.web")
JOB_STATUSES = {"queued", "running", "succeeded", "failed", "canceled", "interrupted"}
OPTION_TYPES = {"bool", "boolean", "tristate", "choice", "enum", "string", "int", "integer", "hex"}
STATIC_ASSETS = ("style.css", "app.js")


def system_logical_cpus() -> int:
    """Return the logical CPU ceiling used by both UI and API validation."""
    return logical_cpu_count()


def _static_asset_version(static_dir: Path) -> str:
    """Return a content fingerprint used to bust browser asset caches."""

    digest = hashlib.sha256()
    for name in STATIC_ASSETS:
        path = static_dir / name
        digest.update(name.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            # StaticFiles will report the missing asset itself.  Keep the
            # index route available so a deployment can expose that error
            # instead of failing app construction while computing a version.
            digest.update(b"<missing>")
    return digest.hexdigest()[:16]


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


def load_runtime(settings: Settings | None = None) -> Runtime:
    """Load the explicitly agreed core modules; never fabricate catalog data."""
    settings = settings or Settings()
    repo_root = Path(os.getenv("OWRT_REPO_ROOT", str(Path(__file__).resolve().parents[1]))).expanduser().resolve()
    device_catalog = load_catalog(repo_root / "configs" / "devices.toml")
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


class StrictBody(BaseModel):
    """Reject client supplied build/runtime knobs that the server owns."""

    model_config = {"extra": "forbid"}


class LoginBody(StrictBody):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class SettingsBody(StrictBody):
    """Account and PushPlus updates accepted by the settings page.

    Optional fields are inspected through ``model_fields_set`` so changing a
    username does not accidentally clear an existing notification token.
    """

    username: str | None = Field(default=None, min_length=1)
    current_password: str | None = Field(default=None, min_length=1)
    new_password: str | None = Field(default=None, min_length=1)
    pushplus_token: str | None = Field(default=None, max_length=512)
    clear_pushplus: StrictBool = False


class JobBody(StrictBody):
    device: str = Field(min_length=1, max_length=80)
    packages: list[str] = Field(default_factory=list, max_length=512)
    options: dict[str, Any] = Field(default_factory=dict)
    # ``StrictInt`` rejects bools and numeric strings before the dynamic CPU
    # ceiling is checked below.
    parallel_jobs: StrictInt | None = Field(default=None, ge=1)
    reuse_cache: StrictBool = True

    @field_validator("packages")
    @classmethod
    def package_names(cls, values: list[str]) -> list[str]:
        for value in values:
            if not re.fullmatch(r"[A-Za-z0-9_.+@-]{1,180}", value):
                raise ValueError("包名包含非法字符")
        return list(dict.fromkeys(values))

    @field_validator("options")
    @classmethod
    def option_names(cls, values: dict[str, Any]) -> dict[str, Any]:
        if len(values) > 1024:
            raise ValueError("子选项数量过多")
        for key in values:
            if not re.fullmatch(r"[A-Za-z0-9_.+@-]{1,220}", str(key)):
                raise ValueError("子选项名称包含非法字符")
            if not isinstance(values[key], (type(None), bool, int, float, str)):
                raise ValueError("子选项值必须是标量")
            if isinstance(values[key], str) and len(values[key]) > 4096:
                raise ValueError("子选项字符串过长")
        return values


class DefaultsBody(StrictBody):
    packages: list[str] = Field(default_factory=list, max_length=512)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("packages")
    @classmethod
    def package_names(cls, values: list[str]) -> list[str]:
        for value in values:
            if not re.fullmatch(r"[A-Za-z0-9_.+@-]{1,180}", value):
                raise ValueError("包名包含非法字符")
        return list(dict.fromkeys(values))

    @field_validator("options")
    @classmethod
    def option_names(cls, values: dict[str, Any]) -> dict[str, Any]:
        if len(values) > 1024:
            raise ValueError("子选项数量过多")
        for key in values:
            if not re.fullmatch(r"[A-Za-z0-9_.+@-]{1,220}", str(key)):
                raise ValueError("子选项名称包含非法字符")
            if not isinstance(values[key], (type(None), bool, int, float, str)):
                raise ValueError("子选项值必须是标量")
            if isinstance(values[key], str) and len(values[key]) > 4096:
                raise ValueError("子选项字符串过长")
        return values


class ValidateBody(JobBody):
    pass


class SourceService:
    """Runs source/feed preparation off the event loop and persists status."""

    def __init__(self, runtime: Runtime, storage: Storage):
        self.runtime = runtime
        self.storage = storage
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def status(self) -> dict[str, Any]:
        raw_state = self.storage.get_state("source")
        state = dict(raw_state) if isinstance(raw_state, Mapping) else {"status": "not_started", "ready": False}
        state.setdefault("ready", bool(state.get("status") == "ready"))
        return state

    def request(self, force: bool = False, reason: str = "manual") -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(force, reason), name="source-preparation", daemon=True)
            self._thread.start()
            return True

    def _run(self, force: bool, reason: str) -> None:
        started = utc_now()
        raw_previous = self.storage.get_state("source")
        previous = raw_previous if isinstance(raw_previous, Mapping) else {}
        previous_ready = self._previous_state_usable(previous)
        preparing = dict(previous) if previous_ready else {}
        # ``finished_at`` records the latest attempt, including failures.  Keep
        # the last successful completion separately so a refresh in progress or
        # a failed refresh cannot make the current snapshot look newer than it
        # is.  Older state has no such field, so migrate from the immutable
        # snapshot's creation time when it is available.
        last_success_at = _source_last_success_at(previous) if previous_ready else None
        if last_success_at:
            preparing["last_success_at"] = last_success_at
        preparing.update({"status": "preparing", "ready": previous_ready, "reason": reason, "started_at": started})
        self.storage.set_state("source", preparing)

        def progress(event: Mapping[str, Any]) -> None:
            LOGGER.info("source preparation: %s", _safe_log(event.get("message", event)))

        try:
            # Prepare every reviewed source exactly once.  SourceManager owns
            # git/feed staging and atomically publishes each PreparedSource;
            # the callback receives structured status events.
            snapshots: list[tuple[dict[str, Any], list[str]]] = []
            for source_id in self.runtime.devices.sources:
                prepared = self.runtime.sources.prepare_source(source_id, update=force, status=progress)
                snapshot = self._snapshot_dict(prepared)
                devices = [
                    spec.key for spec in self.runtime.devices.devices.values()
                    if spec.source_id == source_id
                ]
                snapshots.append((snapshot, devices))
            if not snapshots:
                raise RuntimeNotReady("源码准备没有返回可用的 source snapshot")
            state_snapshots: dict[str, dict[str, Any]] = {}
            first_snapshot: dict[str, Any] | None = None
            for snapshot, devices in snapshots:
                source_id = str(snapshot.get("snapshot_id") or snapshot.get("id") or "")
                if not source_id:
                    raise RuntimeNotReady("源码准备没有返回 snapshot_id")
                catalog_file = snapshot.get("catalog_path")
                if not catalog_file:
                    raise RuntimeNotReady(f"源码快照 {source_id} 没有返回 catalog_path")
                catalog = Catalog.read(Path(str(catalog_file)))
                items = normalize_catalog(catalog)
                catalog_digest = hashlib.sha256(
                    json.dumps(items, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()[:16]
                for device in devices or ["*"]:
                    # A source can serve more than one device.  Keep one
                    # immutable catalogue row per device so the SQLite key is
                    # stable across devices and repeat refreshes.
                    catalog_id = f"{source_id}:{catalog_digest}:{device}"
                    self.storage.save_catalog(catalog_id, source_id, device, items, {"snapshot": snapshot})
                    if device != "*":
                        state_snapshots[device] = snapshot
                first_snapshot = first_snapshot or snapshot
            finished_at = utc_now()
            self.storage.set_state(
                "source",
                {
                    "status": "ready",
                    "ready": True,
                    "snapshot": first_snapshot,
                    "snapshot_id": first_snapshot.get("snapshot_id") if first_snapshot else None,
                    "snapshots": state_snapshots,
                    "last_attempt_failed": False,
                    "finished_at": finished_at,
                    "last_success_at": finished_at,
                    "reason": reason,
                },
            )
        except Exception as exc:  # keep old current/catalog available on failure
            LOGGER.exception("source preparation failed")
            failed = dict(previous) if previous_ready else {}
            if last_success_at:
                failed["last_success_at"] = last_success_at
            failed.update({
                "status": "failed",
                "ready": previous_ready,
                "refresh_error": _safe_error(exc),
                "last_attempt_failed": True,
                "finished_at": utc_now(),
                "reason": reason,
            })
            self.storage.set_state("source", failed)

    def _previous_state_usable(self, state: Mapping[str, Any]) -> bool:
        """Only expose a persisted catalog whose v6 snapshot is still readable.

        A preparation schema bump deliberately invalidates old snapshots.  If
        a process restarts while the first refresh is offline, blindly
        preserving a historical ``ready`` flag would let Web submit jobs
        pinned to a snapshot that BuildEngine correctly rejects.  Validate the
        persisted immutable snapshots before retaining the old ready state;
        a valid current snapshot is still preserved when a later refresh fails.
        """

        if not bool(state.get("ready")):
            return False
        candidates: list[Mapping[str, Any]] = []
        snapshots = state.get("snapshots")
        if isinstance(snapshots, Mapping):
            candidates.extend(value for value in snapshots.values() if isinstance(value, Mapping))
        snapshot = state.get("snapshot")
        if isinstance(snapshot, Mapping) and not candidates:
            candidates.append(snapshot)
        if not candidates:
            return False
        get_snapshot = getattr(self.runtime.sources, "get_snapshot", None)
        if not callable(get_snapshot):
            return False
        for candidate in candidates:
            snapshot_id = str(candidate.get("snapshot_id") or candidate.get("id") or "")
            if not snapshot_id:
                return False
            try:
                prepared = get_snapshot(snapshot_id)
                if not isinstance(prepared, PreparedSource):
                    return False
            except Exception:
                return False
            expected_source = candidate.get("source_id")
            if expected_source and str(expected_source) != prepared.source_id:
                return False
        return True

    @staticmethod
    def _snapshot_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, PreparedSource):
            return _json_safe(value.to_dict())
        if isinstance(value, Mapping):
            return {str(k): _json_safe(v) for k, v in value.items()}
        raise RuntimeNotReady(f"源码准备返回了未支持的类型：{type(value).__name__}")

    def stop(self) -> None:
        self._stop.set()


class QueueWorker:
    def __init__(self, runtime: Runtime, storage: Storage, settings: Settings, notifier: PushPlusNotifier):
        self.runtime = runtime
        self.storage = storage
        self.settings = settings
        self.notifier = notifier
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._running: dict[str, threading.Event] = {}
        self._running_lock = threading.RLock()

    def start(self) -> None:
        self.reconcile()
        self.thread = threading.Thread(target=self._loop, name="build-worker", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            job = self.storage.claim_next_job()
            if job is None:
                self.stop_event.wait(self.settings.worker_poll_seconds)
                continue
            self._run_job(job)

    def _run_job(self, job: dict[str, Any]) -> None:
        cancel_event = threading.Event()
        with self._running_lock:
            self._running[job["id"]] = cancel_event

        def log(line: str) -> None:
            self.storage.append_log(job["id"], _safe_log(line))

        def is_cancelled() -> bool:
            return cancel_event.is_set() or self.storage.is_cancel_requested(job["id"])

        try:
            if is_cancelled():
                canceled = _canceled_result(None, job)
                self.storage.finish_job(job["id"], "canceled", "管理员取消任务", canceled)
                log("任务已取消")
                self.notifier.send_async(job, "canceled", "管理员取消任务")
                return
            log(
                f"开始构建：设备 {job['device']}，源码快照 {job['source_snapshot'].get('snapshot_id', '')}，"
                f"并行核心 {job.get('parallel_jobs', 1)}，复用缓存 {'开启' if job.get('reuse_cache', True) else '关闭'}"
            )
            request = self.runtime.request_type(
                **_request_kwargs(
                    self.runtime.request_type,
                    task_id=job["id"],
                    device=job["device"],
                    snapshot_id=job["source_snapshot"].get("snapshot_id"),
                    packages=job["packages"],
                    options=job["options"],
                    parallel_jobs=int(job.get("parallel_jobs", 1) or 1),
                    reuse_cache=bool(job.get("reuse_cache", True)),
                )
            )
            result = self.runtime.build.build(request, on_log=log, cancel_event=cancel_event)
            if is_cancelled():
                self.storage.finish_job(job["id"], "canceled", "管理员取消任务", _canceled_result(result, job))
                log("任务已取消")
                self.notifier.send_async(job, "canceled", "管理员取消任务")
                return
            result_dict = _result_with_controls(result, job)
            self._register_artifacts(job, result_dict)
            result_status = str(result_dict.get("status", ""))
            if result_status in {"cancelled", "canceled"}:
                error = result_dict.get("error") or "构建已取消"
                self.storage.finish_job(job["id"], "canceled", error, _canceled_result(result_dict, job, error))
                log("任务已取消")
                self.notifier.send_async(job, "canceled", error)
                return
            success = bool(result_dict.get("success", result_dict.get("ok", result_status in {"success", "succeeded"})))
            if success:
                final_status = self.storage.finish_job(job["id"], "succeeded", None, result_dict)
                if final_status == "canceled":
                    log("任务已取消")
                    self.notifier.send_async(job, "canceled", "管理员取消任务")
                else:
                    log("编译完成")
                    self.notifier.send_async(job, "succeeded")
            else:
                error = _safe_error(result_dict.get("error") or result_dict.get("message") or "构建失败")
                final_status = self.storage.finish_job(job["id"], "failed", error, result_dict)
                if final_status == "canceled":
                    log("任务已取消")
                    self.notifier.send_async(job, "canceled", "管理员取消任务")
                else:
                    log(f"构建失败：{error}")
                    self.notifier.send_async(job, "failed", error)
        except Exception as exc:
            error = _safe_error(exc)
            status_name = "canceled" if is_cancelled() else "failed"
            result = {
                "status": status_name,
                "success": False,
                "ok": False,
                "error": error,
                "parallel_jobs": int(job.get("parallel_jobs", 1) or 1),
                "reuse_cache": bool(job.get("reuse_cache", True)),
            }
            final_status = self.storage.finish_job(
                job["id"],
                status_name,
                error,
                result,
            )
            if final_status == "canceled":
                log("任务已取消：管理员取消任务")
                self.notifier.send_async(job, "canceled", "管理员取消任务")
            else:
                log(f"任务失败：{error}")
                self.notifier.send_async(job, "failed", error)
        finally:
            with self._running_lock:
                self._running.pop(job["id"], None)

    def _register_artifacts(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        artifacts = list(result.get("artifacts") or [])
        # The manifest is part of the reviewable build result even though the
        # core keeps it in a separate field from firmware image paths.  Copy
        # it into the same private, allowlisted download directory so the Web
        # history can provide both the image and its provenance/checksums.
        manifest_path = result.get("manifest_path")
        if manifest_path and all(str(manifest_path) != str(item) for item in artifacts):
            artifacts.append({"path": manifest_path, "name": "manifest.json"})
        output_dir = Path(job["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(artifacts, Mapping):
            artifacts = [artifacts]
        for item in artifacts:
            if isinstance(item, (str, os.PathLike)):
                path = Path(item)
                name = path.name
            elif isinstance(item, Mapping):
                raw = item.get("path") or item.get("file") or item.get("relative_path")
                if not raw:
                    continue
                path = Path(str(raw))
                name = str(item.get("name") or path.name)
            else:
                continue
            try:
                source_path = path.resolve(strict=True) if path.is_absolute() else (output_dir / path).resolve(strict=True)
                if not source_path.is_file() or any(part.is_symlink() for part in _path_parts_between(source_path.parent, source_path)):
                    continue
                # BuildEngine writes into its controlled workspace.  Copy each
                # result into this job's private artifact directory so the
                # download route has one strict allowlisted root and no caller
                # can influence an output path.
                safe_name = re.sub(r"[^A-Za-z0-9_.@+-]", "_", name)[:200] or f"artifact-{uuid.uuid4().hex}"
                destination = output_dir / safe_name
                if destination.exists() or destination.is_symlink():
                    continue
                shutil.copy2(source_path, destination)
                relative = destination.relative_to(output_dir).as_posix()
                digest = _sha256_file(destination)
                self.storage.add_artifact({"id": uuid.uuid4().hex, "job_id": job["id"], "name": safe_name, "relative_path": relative, "size": destination.stat().st_size, "sha256": digest})
            except (OSError, ValueError):
                continue

    def request_cancel(self, job_id: str) -> str | None:
        result = self.storage.request_cancel(job_id)
        if result == "cancel_requested":
            with self._running_lock:
                event = self._running.get(job_id)
                if event:
                    event.set()
            # Set the local predicate and stop the named Docker container
            # independently.  The latter is required when the request races
            # with a worker thread, or when this process lost its in-memory
            # event during a restart.
            try:
                self.runtime.build.cancel(job_id)
            except Exception as exc:
                LOGGER.warning("unable to stop canceled build %s: %s", job_id, _safe_error(exc))
            self.storage.append_log(job_id, "已收到取消请求，正在停止构建容器")
        return result

    def reconcile(self) -> None:
        """Resolve jobs left running by a previous process without duplicates.

        The worker cannot safely attach to an arbitrary Docker process and
        reconstruct its callback stream.  A surviving builder container is
        therefore stopped.  An explicit cancellation request remains
        ``canceled`` across a Web restart; an unrequested orphan is marked
        ``interrupted`` and is never started again.
        """
        for job in self.storage.running_jobs():
            controls = {
                "parallel_jobs": int(job.get("parallel_jobs", 1) or 1),
                "reuse_cache": bool(job.get("reuse_cache", True)),
            }
            cancel_requested = bool(job.get("cancel_requested"))
            state = _inspect_build_runtime(self.runtime, job["id"])
            if state in {"running", "created", "restarting"}:
                try:
                    self.runtime.build.cancel(job["id"])
                except Exception as exc:
                    LOGGER.warning("unable to stop orphaned build %s: %s", job["id"], _safe_error(exc))
                if cancel_requested:
                    self.storage.finish_job(
                        job["id"],
                        "canceled",
                        "管理员取消任务",
                        _canceled_result(None, job),
                    )
                else:
                    self.storage.finish_job(
                        job["id"],
                        "interrupted",
                        "Web 进程重启时检测到遗留构建容器，任务已中断且不会重复启动",
                        controls,
                    )
                continue
            if cancel_requested:
                self.storage.finish_job(
                    job["id"],
                    "canceled",
                    "管理员取消任务",
                    _canceled_result(None, job),
                )
                continue
            if state in {"succeeded", "failed", "canceled"}:
                self.storage.finish_job(job["id"], state, None, controls)
            else:
                self.storage.finish_job(job["id"], "interrupted", "Web 进程重启时未检测到仍在运行的构建容器", controls)

    def stop(self) -> None:
        self.stop_event.set()
        with self._running_lock:
            for event in self._running.values():
                event.set()


class Scheduler:
    def __init__(self, source_service: SourceService):
        self.source_service = source_service
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        # Initial preparation is asynchronous and does not delay FastAPI
        # startup or the first login page.
        self.source_service.request(False, "startup")
        self.thread = threading.Thread(target=self._loop, name="source-scheduler", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        zone = ZoneInfo("Asia/Shanghai")
        while not self.stop_event.is_set():
            now = datetime.now(zone)
            target = now.replace(hour=4, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_seconds = max(1.0, (target - now).total_seconds())
            if self.stop_event.wait(min(wait_seconds, 60.0)):
                return
            if datetime.now(zone) >= target:
                self.source_service.request(True, "daily_04:00_Asia/Shanghai")

    def stop(self) -> None:
        self.stop_event.set()


def normalize_catalog(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    if isinstance(value, Mapping):
        value = value.get("items") or value.get("packages") or value.get("catalog") or []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        item = {str(key): _json_safe(val) for key, val in raw.items()}
        symbol = str(item.get("symbol") or item.get("name") or item.get("package") or "").strip()
        if not symbol:
            continue
        item["symbol"] = symbol
        item.setdefault("name", symbol)
        item.setdefault("title", item.get("prompt") or symbol)
        if not item.get("category"):
            package_name = str(item.get("name") or symbol.removeprefix("CONFIG_PACKAGE_"))
            item["category"] = "luci-app" if package_name.startswith("luci-app-") else ("luci-theme" if package_name.startswith("luci-theme-") else "other")
        item.setdefault("type", "bool")
        item.setdefault("default", False if item["type"] in {"bool", "boolean"} else "")
        item.setdefault("depends", item.get("depends_on") or [])
        item.setdefault("options", item.get("suboptions") or [])
        result.append(item)
    return sorted(result, key=lambda item: (str(item.get("category")), str(item.get("title")), item["symbol"]))


def filter_catalog(items: Iterable[dict[str, Any]], query: str, category: str) -> list[dict[str, Any]]:
    query = query.strip().casefold()
    category = category.strip().casefold()
    result = []
    for item in items:
        item_category = str(item.get("category", "other")).casefold()
        if category and category != "all" and item_category != category:
            continue
        haystack = " ".join(str(item.get(key, "")) for key in ("symbol", "name", "title", "description")).casefold()
        if query and query not in haystack:
            continue
        result.append(item)
    return result


def canonical_package_names(items: Sequence[dict[str, Any]], packages: Sequence[str]) -> list[str]:
    """Resolve accepted package names/symbols to package names.

    The browser always submits names, but accepting ``CONFIG_PACKAGE_*`` here
    keeps the API compatible with small scripts while ensuring the build core
    never receives a Kconfig symbol where it expects a package name.
    Unknown values are intentionally omitted; callers use ``validate_options``
    to return the corresponding structured errors before persisting a job.
    """

    aliases: dict[str, str] = {}
    for item in items:
        name = str(item.get("name") or "").strip()
        symbol = str(item.get("symbol") or "").strip()
        if not name and symbol.startswith("CONFIG_PACKAGE_"):
            name = symbol.removeprefix("CONFIG_PACKAGE_")
        if not name:
            continue
        aliases[name] = name
        if symbol:
            aliases[symbol] = name
        aliases[f"CONFIG_PACKAGE_{name}"] = name
    result: list[str] = []
    for package in packages:
        canonical = aliases.get(str(package))
        if canonical and canonical not in result:
            result.append(canonical)
    return result


def validate_options(items: Sequence[dict[str, Any]], packages: Sequence[str], options: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_symbol: dict[str, dict[str, Any]] = {}
    package_aliases: dict[str, str] = {}
    for item in items:
        symbol = str(item.get("symbol") or "")
        name = str(item.get("name") or symbol.removeprefix("CONFIG_PACKAGE_") or "")
        if symbol:
            by_symbol[symbol] = item
        if name:
            by_symbol[name] = item
            package_aliases[name] = name
        if symbol.startswith("CONFIG_PACKAGE_"):
            package_aliases[symbol] = name
            package_aliases[symbol.removeprefix("CONFIG_PACKAGE_")] = name
    issues: list[dict[str, Any]] = []
    selected: set[str] = set()
    for raw_package in packages:
        symbol = str(raw_package)
        item = by_symbol.get(symbol)
        canonical = package_aliases.get(symbol)
        if item is None or canonical is None:
            issues.append({"kind": "unknown_package", "symbol": symbol, "message": f"目录中不存在包 {symbol}"})
            continue
        selected.add(canonical)
    # Keep the owning package with every option.  A package-local Kconfig
    # symbol is not necessarily prefixed with CONFIG_PACKAGE_<name> (feed
    # metadata can expose symbols such as CONFIG_NODEJS_20), so deriving the
    # owner from the symbol alone would allow an option for an unselected
    # package to pass the HTTP-side checks and fail later in BuildEngine.
    known_options: dict[str, tuple[dict[str, Any], str | None]] = {}
    for item in items:
        for raw in item.get("options", []) or []:
            if not isinstance(raw, Mapping):
                continue
            option = {str(k): _json_safe(v) for k, v in raw.items()}
            key = str(option.get("symbol") or option.get("name") or "")
            if key:
                owner = option.get("package") or option.get("owner") or item.get("name")
                # Accept the native CONFIG_ spelling as well as the short
                # spelling used by a few older clients.  The value sent to
                # BuildEngine is still rendered with the canonical CONFIG_
                # prefix.
                known_options[key] = (option, str(owner) if owner else None)
                if key.startswith("CONFIG_"):
                    known_options[key.removeprefix("CONFIG_")] = (option, str(owner) if owner else None)
    for key, value in options.items():
        option_record = known_options.get(str(key))
        if option_record is None:
            issues.append({"kind": "unknown_option", "symbol": key, "message": f"目录中不存在子选项 {key}"})
            continue
        option, owner = option_record
        if owner:
            owner_name = package_aliases.get(owner, owner.removeprefix("CONFIG_PACKAGE_").removeprefix("PACKAGE_"))
            if owner_name not in selected:
                issues.append({
                    "kind": "option_unselected_package",
                    "symbol": key,
                    "package": owner_name,
                    "message": f"子选项 {key} 属于未选中的包 {owner_name}",
                })
                continue
        type_name = str(option.get("type") or option.get("kind") or "string").lower()
        valid = True
        if type_name in {"bool", "boolean"}:
            valid = isinstance(value, bool)
        elif type_name in {"int", "integer"}:
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif type_name in {"choice", "enum"}:
            choices = option.get("choices") or option.get("values") or []
            allowed = {str(x.get("value") if isinstance(x, Mapping) else x) for x in choices}
            valid = isinstance(value, str) and (not allowed or value in allowed)
        elif type_name == "hex":
            valid = isinstance(value, str) and bool(re.fullmatch(r"0[xX][0-9a-fA-F]+", value))
        elif type_name == "tristate":
            valid = isinstance(value, str) and value in {"y", "m", "n"}
        elif type_name == "string":
            valid = isinstance(value, str)
        if not valid:
            issues.append({"kind": "invalid_value", "symbol": key, "message": f"子选项 {key} 的值类型不正确"})
    # Static dependency feedback is intentionally advisory; authoritative
    # auto-dependency/cannot-remove results come from core defconfig.
    for symbol in selected:
        item = by_symbol.get(symbol)
        for dependency in _dependencies(item.get("depends") if item else None):
            if dependency not in selected and dependency in by_symbol:
                issues.append({"kind": "dependency", "symbol": symbol, "depends_on": dependency, "message": f"{symbol} 依赖 {dependency}，defconfig 可能自动加入"})
    return issues


def public_catalog(items: Sequence[dict[str, Any]], default_packages: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Return selectable UI entries without leaking thousands of internals.

    The authoritative full catalog stays in the snapshot and is used by
    validation/build composition.  The browser receives LuCI apps/themes plus
    the non-LuCI packages present in the reviewed (or saved) device defaults,
    so tools such as tailscale/lsof/iperf3 remain editable without rendering
    every kernel module and library in the feed.
    """

    defaults = {str(name) for name in default_packages}
    result: list[dict[str, Any]] = []
    for raw in items:
        name = str(raw.get("name") or raw.get("symbol") or "")
        if not name:
            continue
        is_plugin = bool(raw.get("is_plugin")) or name.startswith(("luci-app-", "luci-theme-"))
        if not is_plugin and name not in defaults:
            continue
        item = dict(raw)
        item["category"] = "luci-app" if name.startswith("luci-app-") else (
            "luci-theme" if name.startswith("luci-theme-") else "other"
        )
        result.append(item)
    return result


def _dependencies(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in re.findall(r"[A-Za-z0-9_.+/-]+", value) if part not in {"y", "m", "n", "and", "or", "not"}]
    if isinstance(value, Sequence):
        return [str(item) for item in value if isinstance(item, (str, int))]
    return []


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe(v) for v in value]
    return str(value)


def _result_dict(result: Any) -> dict[str, Any]:
    if result is None:
        return {}
    if isinstance(result, Mapping):
        return {str(k): _json_safe(v) for k, v in result.items()}
    fields = (
        "success", "ok", "status", "artifacts", "artifact_paths", "config_path",
        "manifest", "manifest_path", "error", "message", "parallel_jobs", "reuse_cache",
    )
    return {key: _json_safe(getattr(result, key)) for key in fields if hasattr(result, key)}


def _validated_build_controls(parallel_jobs: int | None, reuse_cache: bool) -> tuple[int, bool]:
    """Apply the server-owned CPU ceiling after Pydantic's strict parsing."""

    maximum = system_logical_cpus()
    value = maximum if parallel_jobs is None else parallel_jobs
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=422, detail={"message": "并行核心数必须是整数", "field": "parallel_jobs"})
    if value < 1 or value > maximum:
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"并行核心数必须在 1 到 {maximum} 之间",
                "field": "parallel_jobs",
                "minimum": 1,
                "maximum": maximum,
            },
        )
    if not isinstance(reuse_cache, bool):
        raise HTTPException(status_code=422, detail={"message": "复用编译缓存必须是布尔值", "field": "reuse_cache"})
    return value, reuse_cache


def _request_kwargs(
    request_type: type[Any],
    *,
    task_id: str,
    device: str,
    snapshot_id: str | None,
    packages: Sequence[str],
    options: Mapping[str, Any],
    parallel_jobs: int,
    reuse_cache: bool,
) -> dict[str, Any]:
    """Build request kwargs across old and new core DTO versions.

    The Web control plane is deployed independently from the compiler image
    during upgrades.  Pass the new controls when the DTO advertises them,
    while keeping old test/worker adapters constructible until they are
    upgraded.  ``**kwargs`` adapters receive the canonical names.
    """

    kwargs: dict[str, Any] = {
        "task_id": task_id,
        "device": device,
        "snapshot_id": snapshot_id,
        "options": dict(options),
    }
    try:
        parameters = inspect.signature(request_type).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    if accepts_kwargs or "package_selections" in parameters:
        kwargs["package_selections"] = tuple(packages)
    elif "packages" in parameters:
        kwargs["packages"] = tuple(packages)
    if accepts_kwargs or "parallel_jobs" in parameters:
        kwargs["parallel_jobs"] = parallel_jobs
    elif "jobs" in parameters:
        kwargs["jobs"] = parallel_jobs
    elif "cores" in parameters:
        kwargs["cores"] = parallel_jobs
    if accepts_kwargs or "reuse_cache" in parameters:
        kwargs["reuse_cache"] = reuse_cache
    return kwargs


def _runtime_info() -> dict[str, int]:
    cpus = system_logical_cpus()
    return {
        "logical_cpus": cpus,
        "cpu_count": cpus,
        "default_parallel_jobs": cpus,
        "max_parallel_jobs": cpus,
    }


def _result_with_controls(result: Any, job: Mapping[str, Any]) -> dict[str, Any]:
    value = _result_dict(result)
    value.setdefault("parallel_jobs", int(job.get("parallel_jobs", 1) or 1))
    value.setdefault("reuse_cache", bool(job.get("reuse_cache", True)))
    return value


def _canceled_result(result: Any, job: Mapping[str, Any], error: str = "管理员取消任务") -> dict[str, Any]:
    value = _result_with_controls(result, job)
    value.update({"status": "canceled", "success": False, "ok": False, "error": error})
    return value


def _safe_log(value: Any) -> str:
    text = str(value).replace("\x00", "")
    # Do not allow common secret assignment forms into persistent logs.
    text = re.sub(r"(?i)(token|password|passwd|secret|authorization)\s*[:=]\s*[^\s]+", r"\1=[REDACTED]", text)
    return text[:16_000]


def _safe_error(value: Any) -> str:
    return _safe_log(value)[:2_000]


def _validate_admin_username(value: str) -> str:
    return validate_username(value)


def _mask_secret(value: str | None) -> str:
    return "••••••••" if value else ""


def _public_settings(storage: Storage, user_id: int) -> dict[str, Any]:
    user = storage.get_admin(user_id)
    token = storage.get_pushplus_token()
    return {
        "username": user["username"] if user else "",
        "pushplus": {
            "configured": bool(token),
            "masked": _mask_secret(token),
        },
    }


def _config_symbol(value: str) -> str:
    value = str(value).strip()
    return value if value.startswith("CONFIG_") else f"CONFIG_{value}"


def _config_value(value: Any) -> str:
    if isinstance(value, bool):
        return "y" if value else "n"
    if isinstance(value, int):
        return str(value)
    if value is None:
        return "n"
    text = str(value)
    if text in {"y", "m", "n"} or re.fullmatch(r"[-+]?\d+", text):
        return text
    if re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]+", text) and text.lower().startswith("0x"):
        return text
    return json.dumps(text, ensure_ascii=False)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_parts_between(root: Path, path: Path) -> Iterator[Path]:
    current = root
    try:
        relative = path.relative_to(root)
    except ValueError:
        return
    for part in relative.parts:
        current = current / part
        yield current


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = dict(job)
    result.pop("owner_user_id", None)
    result.pop("output_dir", None)
    try:
        result["parallel_jobs"] = max(1, int(result.get("parallel_jobs", 1) or 1))
    except (TypeError, ValueError):
        result["parallel_jobs"] = 1
    result["reuse_cache"] = bool(result.get("reuse_cache", True))
    snapshot = result.get("source_snapshot")
    if isinstance(snapshot, Mapping):
        result["source_snapshot"] = {
            key: _json_safe(snapshot[key])
            for key in ("source_id", "snapshot_id", "source_commit", "feed_commits", "created_at")
            if key in snapshot
        }
    build_result = result.get("result")
    if isinstance(build_result, Mapping):
        build_result = dict(build_result)
        for key in ("workspace", "config_path", "manifest_path", "log_path"):
            build_result.pop(key, None)
        build_result.pop("artifacts", None)
        build_result.pop("artifact_paths", None)
        build_result.pop("metadata", None)
        if "parallel_jobs" not in build_result:
            build_result["parallel_jobs"] = result["parallel_jobs"]
        if "reuse_cache" not in build_result:
            build_result["reuse_cache"] = result["reuse_cache"]
        result["result"] = build_result
    return result


def _public_source_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    if not state:
        return {"status": "not_started", "ready": False}
    result = {str(key): _json_safe(value) for key, value in state.items()}
    # Keep the API useful for state written before ``last_success_at`` was
    # introduced.  A snapshot's created_at is immutable and therefore safe as
    # a migration fallback; the attempt finished_at is deliberately excluded.
    if result.get("ready") and not result.get("last_success_at"):
        last_success_at = _source_last_success_at(result)
        if last_success_at:
            result["last_success_at"] = last_success_at
    for key in ("snapshot", "snapshots"):
        value = result.get(key)
        if isinstance(value, Mapping):
            result[key] = {
                str(device): {
                    field: snapshot[field]
                    for field in ("source_id", "snapshot_id", "source_commit", "feed_commits", "created_at")
                    if field in snapshot
                }
                for device, snapshot in value.items()
                if isinstance(snapshot, Mapping)
            } if key == "snapshots" else {
                field: value[field]
                for field in ("source_id", "snapshot_id", "source_commit", "feed_commits", "created_at")
                if field in value
            }
    return result


def _source_last_success_at(state: Mapping[str, Any]) -> str | None:
    """Return the stable timestamp for the currently usable source snapshot."""

    value = state.get("last_success_at")
    if isinstance(value, str) and value.strip():
        return value
    candidates: list[str] = []
    snapshot = state.get("snapshot")
    if isinstance(snapshot, Mapping):
        created_at = snapshot.get("created_at")
        if isinstance(created_at, str) and created_at.strip():
            candidates.append(created_at)
    snapshots = state.get("snapshots")
    if isinstance(snapshots, Mapping):
        # This is only a migration fallback for old state without a top-level
        # success timestamp.  ISO-8601 timestamps sort lexicographically when
        # emitted by the builder, so the newest available snapshot is enough.
        candidates.extend(
            value.get("created_at")
            for value in snapshots.values()
            if isinstance(value, Mapping) and isinstance(value.get("created_at"), str)
        )
    candidates = [value for value in candidates if value.strip()]
    return max(candidates) if candidates else None


def _supported_devices(runtime: Runtime) -> list[DeviceSpec]:
    """Return the reviewed device catalogue in its declared order."""

    return list(runtime.devices.devices.values())


def _canonical_device(runtime: Runtime, value: str | None, status_code: int = 404) -> str:
    if not value:
        raise HTTPException(status_code=status_code, detail="必须选择设备")
    try:
        spec = runtime.devices.resolve(value)
    except ValueError as exc:
        raise HTTPException(status_code=status_code, detail="不支持的设备") from exc
    return spec.key


def _fragment_defaults(runtime: Runtime, spec: DeviceSpec, catalog: Sequence[dict[str, Any]] | None) -> tuple[list[str], dict[str, Any]]:
    """Read the reviewed device fragment without duplicating package names."""

    path = (runtime.devices.path.parent / spec.config).resolve()
    root = runtime.devices.path.parent.resolve()
    if path != root and root not in path.parents:
        raise RuntimeNotReady("设备配置路径越界")
    if not path.is_file():
        raise RuntimeNotReady(f"设备配置不存在：{spec.config}")
    document = parse_config(path)
    packages: list[str] = []
    options: dict[str, Any] = {}
    package_names = {
        str(item.get("name") or "")
        for item in (catalog or ())
        if isinstance(item, Mapping)
    }
    base_owners: dict[str, str] = {}
    option_records: dict[str, tuple[str, str]] = {}
    for item in (catalog or ()):
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "")
        symbol = str(item.get("symbol") or "")
        if name and symbol:
            base_owners[symbol] = name
        for raw_option in item.get("options", []) or ():
            if not isinstance(raw_option, Mapping):
                continue
            option_symbol = str(raw_option.get("symbol") or "")
            if option_symbol:
                option_records[option_symbol] = (
                    str(raw_option.get("package") or name),
                    str(raw_option.get("kind") or raw_option.get("type") or ""),
                )

    # Resolve the base package selections first.  Package-local symbols are
    # allowed to have unrelated names (for example CONFIG_PARTED_READLINE),
    # so inferring ownership from a prefix would lose options from reviewed
    # defaults.  The generated catalog is the explicit ownership map.
    selected_packages: set[str] = set()
    for entry in document.entries:
        if entry.value not in {"y", "m"}:
            continue
        name = base_owners.get(entry.symbol)
        if name:
            selected_packages.add(name)
            packages.append(name)

    for entry in document.entries:
        record = option_records.get(entry.symbol)
        if record and entry.value != "n":
            owner, kind = record
            owner = owner.removeprefix("CONFIG_PACKAGE_").removeprefix("PACKAGE_")
            if owner in selected_packages:
                normalized_kind = kind.casefold()
                if normalized_kind in {"bool", "boolean"}:
                    options[entry.symbol] = entry.value == "y"
                elif normalized_kind in {"tristate", "choice", "enum"}:
                    options[entry.symbol] = entry.value
                else:
                    options[entry.symbol] = entry.value
            continue
        if entry.value not in {"y", "m"}:
            continue
        # Before the first authoritative catalog is ready, retain the
        # reviewed LuCI package defaults as a display fallback.
        name = entry.symbol.removeprefix("CONFIG_PACKAGE_")
        if not base_owners and not option_records and (
            name in package_names
            or name.startswith("luci-app-")
            or name.startswith("luci-theme-")
        ):
            packages.append(name)
    return list(dict.fromkeys(packages)), options


def _device_info(runtime: Runtime, storage: Storage, key: str, source_service: SourceService | None = None) -> dict[str, Any]:
    canonical = _canonical_device(runtime, key)
    spec = runtime.devices.resolve(canonical)
    info = _json_safe(spec.to_dict(runtime.devices.sources[spec.source_id]))
    catalog = _catalog_for(storage, source_service, canonical)
    try:
        fragment_packages, fragment_options = _fragment_defaults(runtime, spec, catalog)
    except RuntimeNotReady:
        fragment_packages, fragment_options = list(spec.default_packages), {}
    info["default_fragment"] = Path(spec.config).stem
    info["default_plugins"] = fragment_packages
    info["default_options"] = fragment_options
    saved = storage.get_defaults(canonical)
    if saved:
        info["saved_defaults"] = {"packages": saved["packages"], "options": saved["options"], "updated_at": saved["updated_at"]}
    else:
        info.setdefault("saved_defaults", None)
    return info


def create_app(runtime: Runtime | None = None, settings: Settings | None = None, notifier: PushPlusNotifier | None = None) -> FastAPI:
    settings = settings or Settings()
    storage = Storage(settings.db_path, settings.log_dir, settings.artifact_dir)
    auth = AuthManager(storage, settings_from_env())
    # Migrate an environment-provided token into the persistent settings row
    # once.  A later Web setting can clear it without the environment silently
    # restoring the old secret on every restart.
    storage.ensure_setting("pushplus_token", os.getenv("PUSHPLUS_TOKEN", ""))
    app_runtime = runtime
    runtime_error: str | None = None
    if app_runtime is None:
        try:
            # Runtime construction is deliberately eager: a healthy Web app
            # must expose the reviewed device catalogue immediately, while
            # network source/feed work remains asynchronous in lifespan.
            app_runtime = load_runtime(settings)
        except Exception as exc:
            runtime_error = _safe_error(exc)
            LOGGER.error("Web 核心未就绪：%s", runtime_error)
    source_service: SourceService | None = None
    worker: QueueWorker | None = None
    scheduler: Scheduler | None = None
    app_notifier = notifier or PushPlusNotifier(
        settings=NotificationSettings.from_env(),
        token_provider=storage.get_pushplus_token,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal app_runtime, source_service, worker, scheduler
        # Keep the first-run account deterministic for local deployments.  The
        # operation is atomic and is a no-op for every existing installation.
        storage.ensure_default_admin("admin", hash_password("admin"))
        if app_runtime is not None:
            source_service = SourceService(app_runtime, storage)
            worker = QueueWorker(app_runtime, storage, settings, app_notifier)
            worker.start()
            scheduler = Scheduler(source_service)
            scheduler.start()
        yield
        if scheduler:
            scheduler.stop()
        if source_service:
            source_service.stop()
        if worker:
            worker.stop()
            if worker.thread and worker.thread.is_alive():
                worker.thread.join(timeout=3)

    app = FastAPI(title="OpenWrt Builder", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.storage = storage
    app.state.settings = settings
    app.state.auth = auth
    if runtime_error:
        app.state.runtime_error = runtime_error

    static_dir = Path(__file__).parent / "static"
    static_version = _static_asset_version(static_dir)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[..., Any]) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    def get_auth(request: Request) -> dict[str, Any]:
        return auth.require_session(request)

    def get_mutation_auth(request: Request) -> dict[str, Any]:
        return auth.validate_csrf(request)

    def require_runtime() -> Runtime:
        if app_runtime is None:
            raise HTTPException(status_code=503, detail=getattr(app.state, "runtime_error", "构建核心未就绪"))
        return app_runtime

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        document = (static_dir / "index.html").read_text(encoding="utf-8")
        for asset in STATIC_ASSETS:
            document = document.replace(
                f"/static/{asset}",
                f"/static/{asset}?v={static_version}",
            )
        return HTMLResponse(
            document,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )

    @app.post("/api/auth/login")
    async def login(body: LoginBody, request: Request, response: Response):
        result = auth.login(request, response, body.username, body.password)
        return {"ok": True, "user": {"id": result["id"], "username": result["username"]}, "csrf": result["csrf"], "expires_in": result["expires_in"]}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response, _session: dict[str, Any] = Depends(get_mutation_auth)):
        token = request.cookies.get("owrt_session")
        if token:
            storage.delete_session(token)
        auth.clear_cookies(response)
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(session: dict[str, Any] = Depends(get_auth)):
        return {"ok": True, "user": {"id": int(session["user_id"]), "username": session["username"]}}

    @app.get("/api/settings")
    async def get_settings(session: dict[str, Any] = Depends(get_auth)):
        return {"ok": True, "settings": _public_settings(storage, int(session["user_id"]))}

    @app.put("/api/settings")
    async def update_settings(
        body: SettingsBody,
        request: Request,
        response: Response,
        session: dict[str, Any] = Depends(get_mutation_auth),
    ):
        user_id = int(session["user_id"])
        account_fields = {"username", "new_password"} & body.model_fields_set
        account_changed = bool(account_fields)
        if account_changed:
            user = storage.get_admin(user_id)
            if user is None or body.current_password is None or not verify_password(body.current_password, user["password_hash"]):
                raise HTTPException(status_code=403, detail="当前密码错误")
            try:
                requested_username = body.username if "username" in body.model_fields_set else str(user["username"])
                if requested_username is None:
                    raise ValueError("用户名不能为空")
                username = _validate_admin_username(requested_username)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            password_hash: str | None = None
            if "new_password" in body.model_fields_set and body.new_password is not None:
                try:
                    password_hash = hash_password(body.new_password)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
            try:
                storage.update_admin(user_id, username, password_hash)
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="用户名已存在") from exc
            # Require an explicit login with the new identity/password.  This
            # also invalidates sessions in other browsers and devices.
            auth.clear_cookies(response)

        if body.clear_pushplus or "pushplus_token" in body.model_fields_set:
            token = None if body.clear_pushplus else (body.pushplus_token or "").strip()
            if token and any(ord(character) < 0x20 for character in token):
                raise HTTPException(status_code=422, detail="PushPlus token 包含非法控制字符")
            storage.set_setting("pushplus_token", token or None)

        return {
            "ok": True,
            "requires_login": account_changed,
            "settings": _public_settings(storage, user_id),
        }

    @app.get("/api/health")
    async def health(session: dict[str, Any] = Depends(get_auth)):
        runtime_ready = app_runtime is not None
        source_status = source_service.status() if source_service else {"status": "not_started", "ready": False}
        return {
            "ok": runtime_ready and bool(source_status.get("ready")),
            "runtime_ready": runtime_ready,
            "runtime": _runtime_info(),
            "source": _public_source_state(source_status),
        }

    @app.get("/api/devices")
    async def devices(_session: dict[str, Any] = Depends(get_auth), rt: Runtime = Depends(require_runtime)):
        items = [_device_info(rt, storage, spec.key, source_service) for spec in _supported_devices(rt)]
        runtime_info = _runtime_info()
        for item in items:
            # Keep per-device responses useful to small API clients while the
            # top-level runtime object is the canonical frontend source.
            item["parallel_jobs_max"] = runtime_info["max_parallel_jobs"]
            item["parallel_jobs_default"] = runtime_info["default_parallel_jobs"]
        return {"items": items, "runtime": runtime_info}

    @app.get("/api/devices/{device}/defaults")
    async def get_defaults(device: str, _session: dict[str, Any] = Depends(get_auth), rt: Runtime = Depends(require_runtime)):
        info = _device_info(rt, storage, device, source_service)
        saved = storage.get_defaults(info["key"])
        return {
            "device": info["key"],
            "packages": (saved or {}).get("packages", info.get("default_plugins", [])),
            "options": (saved or {}).get("options", info.get("default_options", {})),
        }

    @app.put("/api/devices/{device}/defaults")
    async def save_defaults(device: str, body: DefaultsBody, request: Request, _session: dict[str, Any] = Depends(get_mutation_auth), rt: Runtime = Depends(require_runtime)):
        info = _device_info(rt, storage, device, source_service)
        catalog = _catalog_for(storage, source_service, info["key"])
        if catalog is None:
            raise HTTPException(status_code=503, detail="源码/feeds 尚未准备完成，暂不能保存设备默认配置")
        issues = validate_options(catalog, body.packages, body.options)
        if any(issue["kind"].startswith("unknown") or issue["kind"] in {"option_unselected_package", "invalid_value"} for issue in issues):
            raise HTTPException(status_code=422, detail={"message": "默认配置未通过目录校验", "issues": issues})
        packages = canonical_package_names(catalog, body.packages)
        storage.save_defaults(info["key"], packages, body.options, int(request.state.user_id))
        return {"ok": True, "device": info["key"], "packages": packages, "options": body.options}

    @app.get("/api/sources")
    async def source_status(_session: dict[str, Any] = Depends(get_auth), _rt: Runtime = Depends(require_runtime)):
        return _public_source_state(source_service.status() if source_service else None)

    @app.post("/api/sources/refresh", status_code=202)
    async def refresh_sources(_session: dict[str, Any] = Depends(get_mutation_auth), _rt: Runtime = Depends(require_runtime)):
        if source_service is None:
            raise HTTPException(status_code=503, detail="源码服务未启动")
        accepted = source_service.request(True, "manual")
        if not accepted:
            raise HTTPException(status_code=409, detail="已有源码更新正在进行")
        return {"ok": True, "status": "preparing"}

    @app.get("/api/catalog")
    async def catalog(
        q: str = Query(default="", max_length=200), category: str = Query(default="luci-app", max_length=80), device: str | None = Query(default=None, max_length=80),
        _session: dict[str, Any] = Depends(get_auth), _rt: Runtime = Depends(require_runtime),
    ):
        canonical = _canonical_device(_rt, device) if device else None
        items = _catalog_for(storage, source_service, canonical)
        if items is None:
            raise HTTPException(status_code=503, detail={"message": "源码/feeds 尚未准备完成，目录暂不可用", "source": _public_source_state(source_service.status() if source_service else None)})
        selected_snapshot = _source_snapshot(storage, source_service, canonical) if canonical else None
        defaults: list[str] = []
        if canonical:
            info = _device_info(_rt, storage, canonical, source_service)
            defaults.extend(info.get("default_plugins") or [])
            saved = info.get("saved_defaults") or {}
            defaults.extend(saved.get("packages") or [])
        visible_items = public_catalog(items, defaults)
        return {
            "items": filter_catalog(visible_items, q, category),
            "category": category,
            "query": q,
            "source_snapshot_id": selected_snapshot.get("snapshot_id") if selected_snapshot else None,
        }

    @app.post("/api/configuration/validate")
    async def validate_configuration(body: ValidateBody, request: Request, _session: dict[str, Any] = Depends(get_mutation_auth), rt: Runtime = Depends(require_runtime)):
        canonical = _canonical_device(rt, body.device)
        parallel_jobs, reuse_cache = _validated_build_controls(body.parallel_jobs, body.reuse_cache)
        items = _catalog_for(storage, source_service, canonical)
        if items is None:
            raise HTTPException(status_code=503, detail="源码/feeds 尚未准备完成")
        issues = validate_options(items, body.packages, body.options)
        packages = canonical_package_names(items, body.packages)
        source = _source_snapshot(storage, source_service, canonical)
        if source is None:
            raise HTTPException(status_code=503, detail="源码快照尚未准备完成")

        # Web validation is intentionally fast and read-only.  The prepared
        # catalog already contains the authoritative .packageinfo and
        # .config-package.in scan, so package/option/type/ownership checks can
        # run without copying a 500MB+ source tree or invoking make.  Native
        # defconfig remains part of the actual BuildEngine task.
        try:
            prepared = _validated_snapshot(rt, source, canonical)
        except SourceSnapshotUnavailable as exc:
            refresh_requested = False
            if source_service is not None:
                refresh_requested = source_service.request(True, f"validation-{exc.kind}-snapshot")
            core = {
                "authoritative": False,
                "validation_mode": "catalog_static",
                "catalog_authoritative": False,
                "native_defconfig": False,
                "issues": [{
                    "kind": "source_snapshot_unavailable",
                    "code": f"source_snapshot_{exc.kind}",
                    "message": f"静态目录校验已完成，但当前源码快照不可用于构建：{exc}",
                    "refresh_requested": refresh_requested,
                }],
            }
        else:
            try:
                catalog_authoritative = bool(Catalog.read(prepared.catalog_path).authoritative)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                catalog_authoritative = False
                issues.append({
                    "kind": "catalog_unavailable",
                    "code": "catalog_read_failed",
                    "message": f"无法读取权威插件目录：{_safe_error(exc)}",
                })
            core = {
                "authoritative": False,
                "validation_mode": "catalog_static",
                "catalog_authoritative": catalog_authoritative,
                "native_defconfig": False,
                "issues": [],
            }
        if isinstance(core, Mapping):
            issues.extend(core.get("issues") or [])
        return {
            "authoritative": False,
            "validation_mode": "catalog_static",
            "catalog_authoritative": bool(core.get("catalog_authoritative")) if isinstance(core, Mapping) else False,
            "native_defconfig": False,
            "issues": issues,
            "core": core,
        }

    @app.post("/api/jobs", status_code=202)
    async def submit_job(body: JobBody, request: Request, _session: dict[str, Any] = Depends(get_mutation_auth), rt: Runtime = Depends(require_runtime)):
        canonical = _canonical_device(rt, body.device, status_code=422)
        parallel_jobs, reuse_cache = _validated_build_controls(body.parallel_jobs, body.reuse_cache)
        items = _catalog_for(storage, source_service, canonical)
        if items is None:
            raise HTTPException(status_code=503, detail="源码/feeds 尚未准备完成，不能提交任务")
        issues = validate_options(items, body.packages, body.options)
        blocking = [issue for issue in issues if issue["kind"] in {"unknown_package", "unknown_option", "option_unselected_package", "invalid_value", "invalid_type"}]
        if blocking:
            raise HTTPException(status_code=422, detail={"message": "配置校验失败", "issues": issues})
        packages = canonical_package_names(items, body.packages)
        source = _source_snapshot(storage, source_service, canonical)
        if source is None:
            raise HTTPException(status_code=503, detail="没有可绑定的源码快照")
        expected_source_id = _device_source_id(rt, canonical)
        actual_source_id = source.get("source_id")
        if expected_source_id and actual_source_id and str(expected_source_id) != str(actual_source_id):
            raise HTTPException(status_code=409, detail="所选设备与源码快照不匹配，请刷新源码目录")
        try:
            # Validate the persisted id against the same SourceManager used by
            # the worker before creating a queue row.  This prevents a
            # contaminated snapshot from becoming a guaranteed instant
            # failure after the worker starts.
            _validated_snapshot(rt, source, canonical)
        except SourceSnapshotUnavailable as exc:
            refresh_requested = False
            if source_service is not None:
                refresh_requested = source_service.request(True, f"queue-{exc.kind}-snapshot")
            raise HTTPException(
                status_code=503,
                detail={
                    "code": f"source_snapshot_{exc.kind}",
                    "message": str(exc),
                    "refresh_requested": refresh_requested,
                },
            ) from exc
        job_id = uuid.uuid4().hex
        artifact_root = settings.artifact_dir.resolve()
        device_output = artifact_root / canonical
        if device_output.exists() and device_output.is_symlink():
            raise HTTPException(status_code=503, detail="设备产物目录无效")
        output = artifact_root / canonical / job_id
        config_name = _device_config(rt, canonical)
        if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,120}", config_name) or "/" in config_name or "\\" in config_name:
            raise HTTPException(status_code=422, detail="配置名称无效")
        job = {
            "id": job_id,
            "device": canonical,
            "config": config_name,
            "packages": packages,
            "options": body.options,
            "parallel_jobs": parallel_jobs,
            "reuse_cache": reuse_cache,
            "source_snapshot": source,
            "status": "queued",
            "created_at": utc_now(),
            "output_dir": str(output),
            "owner_user_id": int(request.state.user_id),
        }
        storage.create_job(job)
        storage.append_log(job_id, f"任务已排队：{canonical}")
        return {"ok": True, "job": _public_job(storage.get_job(job_id) or job), "issues": issues}

    @app.get("/api/jobs")
    async def list_jobs(limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), _session: dict[str, Any] = Depends(get_auth)):
        return {"items": [_public_job(job) for job in storage.list_jobs(limit, offset)], "limit": limit, "offset": offset}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, _session: dict[str, Any] = Depends(get_auth)):
        job = storage.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        job["artifacts"] = storage.list_artifacts(job_id)
        return {"job": _public_job(job)}

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, _session: dict[str, Any] = Depends(get_mutation_auth)):
        if worker is None:
            raise HTTPException(status_code=503, detail="构建 worker 未启动")
        result = worker.request_cancel(job_id)
        if result is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"ok": True, "status": result}

    @app.post("/api/jobs/{job_id}/save-default")
    async def save_job_default(job_id: str, request: Request, _session: dict[str, Any] = Depends(get_mutation_auth)):
        job = storage.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if job["status"] != "succeeded":
            raise HTTPException(status_code=409, detail="只有成功任务可以保存为设备默认")
        storage.save_defaults(job["device"], job["packages"], job["options"], int(request.state.user_id))
        return {"ok": True, "device": job["device"], "packages": job["packages"], "options": job["options"]}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request, after: int = Query(default=0, ge=0), _session: dict[str, Any] = Depends(get_auth)):
        if storage.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        last = max(after, _last_event_id(request))

        async def stream():
            nonlocal last
            idle = 0
            while not await request.is_disconnected():
                rows = storage.get_logs(job_id, last, 500)
                if rows:
                    for row in rows:
                        last = int(row["seq"])
                        payload = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                        yield f"id: {last}\nevent: log\ndata: {payload}\n\n"
                    idle = 0
                    continue
                job = storage.get_job(job_id)
                if job and job["status"] in {"succeeded", "failed", "canceled", "interrupted"}:
                    yield f"event: status\ndata: {json.dumps({'status': job['status'], 'job_id': job_id}, ensure_ascii=False)}\n\n"
                    return
                idle += 1
                if idle % 10 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/jobs/{job_id}/artifacts")
    async def artifacts(job_id: str, _session: dict[str, Any] = Depends(get_auth)):
        if storage.get_job(job_id) is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"items": storage.list_artifacts(job_id)}

    @app.get("/api/jobs/{job_id}/artifacts/{artifact_id}/download")
    async def download_artifact(job_id: str, artifact_id: str, _session: dict[str, Any] = Depends(get_auth)):
        job = storage.get_job(job_id)
        artifact = storage.get_artifact(artifact_id)
        if job is None or artifact is None or artifact["job_id"] != job_id:
            raise HTTPException(status_code=404, detail="产物不存在")
        artifact_root = settings.artifact_dir.resolve()
        root = Path(job["output_dir"]).resolve()
        if root != artifact_root and artifact_root not in root.parents:
            raise HTTPException(status_code=404, detail="产物目录无效")
        relative = Path(artifact["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise HTTPException(status_code=404, detail="产物路径无效")
        candidate = (root / relative)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise HTTPException(status_code=404, detail="产物文件不存在") from exc
        if resolved != root and root not in resolved.parents:
            raise HTTPException(status_code=404, detail="产物路径无效")
        if any(part.is_symlink() for part in _path_parts_between(root, resolved)):
            raise HTTPException(status_code=404, detail="产物路径无效")
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="产物文件不存在")
        return FileResponse(resolved, filename=artifact["name"], media_type="application/octet-stream")

    return app


def _last_event_id(request: Request) -> int:
    try:
        return max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        return 0


def _inspect_build_runtime(runtime: Runtime, job_id: str) -> str:
    """Return a container state before recovering a persisted running job.

    BuildEngine versions that expose the fixed ``inspect(job_id)`` hook are
    preferred.  The current Docker runner uses a deterministic container name,
    so the CLI inspection fallback still checks Docker's real state and never
    starts a second container.  A missing/unknown container becomes
    ``unknown`` is marked interrupted by the caller unless the persisted job
    already carries an explicit cancellation request.
    """
    try:
        state = str(runtime.build.inspect(job_id)).strip().lower()
    except Exception as exc:
        LOGGER.warning("unable to inspect job %s through BuildEngine: %s", job_id, _safe_error(exc))
        return "unknown"
    return {"created": "created", "running": "running", "restarting": "restarting", "exited": "failed", "success": "succeeded", "succeeded": "succeeded", "failed": "failed", "cancelled": "canceled", "canceled": "canceled"}.get(state, "unknown")


def _source_snapshot(storage: Storage, source_service: SourceService | None, device: str | None = None) -> dict[str, Any] | None:
    state = source_service.status() if source_service else storage.get_state("source")
    if not state or not state.get("ready"):
        return None
    snapshots = state.get("snapshots") or {}
    if device and isinstance(snapshots, Mapping) and snapshots.get(device):
        return snapshots[device]
    snapshot = state.get("snapshot")
    return snapshot if isinstance(snapshot, Mapping) else None


def _validated_snapshot(runtime: Runtime, source: Mapping[str, Any], device: str) -> PreparedSource:
    """Resolve and validate the exact snapshot a worker would consume.

    The Web state is persisted separately from the source cache.  Looking up
    the id again here closes that gap: a stale, missing, or contaminated
    snapshot is rejected before a job can be written to the queue.
    """

    snapshot_id = str(source.get("snapshot_id") or source.get("id") or "")
    if not snapshot_id:
        raise SourceSnapshotUnavailable("missing", "当前源码快照记录缺少 snapshot_id，请先更新源码/feeds")
    try:
        prepared = runtime.sources.get_snapshot(snapshot_id)
    except KeyError as exc:
        raise SourceSnapshotUnavailable(
            "missing",
            f"源码快照 {snapshot_id!r} 不存在，请先更新源码/feeds",
        ) from exc
    except SourceError as exc:
        raise SourceSnapshotUnavailable(
            "invalid",
            f"源码快照 {snapshot_id!r} 不可用：{exc}；请先更新源码/feeds",
        ) from exc
    except OSError as exc:
        raise SourceSnapshotUnavailable(
            "invalid",
            f"读取源码快照 {snapshot_id!r} 失败：{exc}；请先更新源码/feeds",
        ) from exc
    except AttributeError as exc:
        raise SourceSnapshotUnavailable(
            "invalid",
            "当前 Web 运行时无法验证源码快照，请先更新源码/feeds",
        ) from exc
    expected_source_id = _device_source_id(runtime, device)
    if expected_source_id and prepared.source_id != expected_source_id:
        raise SourceSnapshotUnavailable(
            "invalid",
            f"源码快照 {snapshot_id!r} 属于 {prepared.source_id!r}，而设备需要 {expected_source_id!r}；请更新源码目录",
        )
    return prepared


def _catalog_for(storage: Storage, source_service: SourceService | None, device: str | None) -> list[dict[str, Any]] | None:
    state = source_service.status() if source_service else storage.get_state("source")
    if not state or not state.get("ready"):
        return None
    snapshot_id = state.get("snapshot_id")
    snapshots = state.get("snapshots") or {}
    if device and isinstance(snapshots, Mapping) and isinstance(snapshots.get(device), Mapping):
        snapshot_id = snapshots[device].get("snapshot_id") or snapshots[device].get("id")
    row = storage.latest_catalog(str(snapshot_id) if snapshot_id else None, device)
    return list(row["items"]) if row else None


def _device_config(runtime: Runtime, device: str) -> str:
    spec = runtime.devices.resolve(device)
    return Path(spec.config).stem


def _device_source_id(runtime: Runtime, device: str) -> str | None:
    return runtime.devices.resolve(device).source_id


app = create_app()
