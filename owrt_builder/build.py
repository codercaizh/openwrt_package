"""Build orchestration shared by the CLI, web actions and worker container.

The host process is intentionally small: it validates a catalog entry, holds a
workspace lock, starts the reviewed builder image, streams logs and checks the
worker result.  The worker uses the same class in direct mode, so there is one
implementation of source preparation, configuration, compilation and
packaging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import time
import traceback
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

from .catalog import Catalog
from .configuration import (
    ConfigDocument,
    ConfigEntry,
    compose_fragment_parts,
    parse_config,
    validate_fragment,
)
from .devices import CatalogError, DeviceCatalog, DeviceSpec, SourceSpec, load_catalog
from .arm_packager import ArmPackagerError, package_arm
from .cache import BuildCacheError, BuildCacheManager
from .sources import PreparedSource, SourceError, SourceManager, stage_download_seeds


LogCallback = Callable[[str], None]
BUILDER_CONTEXT_LABEL = "org.openwrt.builder.context"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _deduplicate_config_assignments(text: str) -> str:
    """Keep the last assignment for each Kconfig symbol in a fragment.

    Device append files and the main fragment can both mention a package or
    option.  The later fragment is the deliberate override, but emitting both
    assignments makes the generated config ambiguous to humans and to tools
    that inspect it before ``make defconfig``.  Preserve comments and blank
    lines while dropping only superseded config assignment lines.
    """

    lines = text.splitlines(keepends=True)
    last_line: dict[str, int] = {}
    parsed: list[tuple[str, str | None]] = []
    for index, line in enumerate(lines):
        entries = parse_config(line).entries
        symbol = entries[0].symbol if len(entries) == 1 else None
        parsed.append((line, symbol))
        if symbol is not None:
            last_line[symbol] = index
    return "".join(
        line
        for index, (line, symbol) in enumerate(parsed)
        if symbol is None or last_line[symbol] == index
    )


def _safe_task_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise ValueError("task_id must contain only letters, digits, '.', '_' or '-'")
    return value


def _cgroup_cpu_quota() -> int | None:
    """Return a conservative integer CPU quota when cgroups expose one."""

    candidates: list[Path] = [
        Path("/sys/fs/cgroup/cpu.max"),
        Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"),
    ]
    # The process is commonly below a systemd cgroup rather than the cgroup
    # mount root.  Walk that relative path and its parents so a root-level
    # ``cpu.max`` lookup does not silently ignore the actual container quota.
    try:
        groups = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except OSError:
        groups = []
    for line in groups:
        try:
            _hierarchy, controllers, relative = line.split(":", 2)
        except ValueError:
            continue
        if not relative:
            continue
        if not controllers:
            current = Path("/sys/fs/cgroup") / relative.lstrip("/")
            while current != Path("/sys/fs/cgroup").parent:
                candidates.append(current / "cpu.max")
                if current == Path("/sys/fs/cgroup"):
                    break
                current = current.parent
        elif "cpu" in controllers.split(","):
            current = Path("/sys/fs/cgroup/cpu") / relative.lstrip("/")
            while current != Path("/sys/fs/cgroup/cpu").parent:
                candidates.append(current / "cpu.cfs_quota_us")
                if current == Path("/sys/fs/cgroup/cpu"):
                    break
                current = current.parent
    for quota_path in candidates:
        try:
            text = quota_path.read_text(encoding="ascii").strip()
            if quota_path.name == "cpu.max":
                quota_text, period_text = text.split()[:2]
                if quota_text == "max":
                    continue
            else:
                quota_text = text
                period_text = quota_path.with_name("cpu.cfs_period_us").read_text(encoding="ascii").strip()
            quota = int(quota_text)
            period = int(period_text)
        except (OSError, ValueError, IndexError):
            continue
        if quota > 0 and period > 0:
            return max(1, quota // period)
    return None


def logical_cpu_count() -> int:
    """Return effective CPUs, respecting affinity and cgroup CPU quotas."""

    effective = max(1, int(os.cpu_count() or 1))
    try:
        affinity = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        affinity = None
    if affinity:
        effective = min(effective, len(affinity))
    quota = _cgroup_cpu_quota()
    if quota is not None:
        effective = min(effective, quota)
    return max(1, effective)


class BuildError(RuntimeError):
    """A build could not be completed."""


class BuildCancelled(BuildError):
    """A caller requested cancellation and the worker stopped."""


class WorkspaceBusy(BuildError):
    """Another build currently owns the workspace lock."""


class CommandFailed(BuildError):
    """A subprocess exited with a non-zero status."""

    def __init__(self, command: Sequence[str], returncode: int):
        self.command = tuple(command)
        self.returncode = returncode
        display = " ".join(_quote_arg(item) for item in command)
        super().__init__(f"command exited {returncode}: {display}")


def _quote_arg(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=+@%-]+", value):
        return value
    return repr(value)


@dataclass(frozen=True)
class BuildRequest:
    """A build request accepted by all front ends.

    ``snapshot_id`` is populated by a prepared source and can only pin the
    exact snapshot selected by the worker.  It is not exposed as a CLI option;
    callers use the reviewed branch in ``devices.toml`` and the resulting
    manifest records the resolved commit.
    """

    device: str
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    snapshot_id: str | None = None
    # ``None`` keeps the reviewed device defaults.  An empty tuple/list is a
    # deliberate request to build with no optional packages.
    packages: tuple[str, ...] | list[str] | None = None
    # Compatibility spelling used by older HTTP adapters.  It is normalised
    # into ``packages`` and is never sent to the worker as a second selection.
    package_selections: tuple[str, ...] | list[str] | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    jobs: int | None = None
    reuse_cache: bool = True
    lock_timeout: float = 0.0

    def __post_init__(self) -> None:
        _safe_task_id(self.task_id)
        if not str(self.device).strip():
            raise ValueError("device is required")
        if self.snapshot_id is not None and (
            not self.snapshot_id
            or Path(self.snapshot_id).name != self.snapshot_id
            or len(self.snapshot_id) > 128
        ):
            raise ValueError("snapshot_id must be an opaque path-safe snapshot id")
        selected_packages = self.packages
        if selected_packages is None and self.package_selections is not None:
            selected_packages = tuple(self.package_selections)
        elif selected_packages is not None and self.package_selections is not None:
            if tuple(selected_packages) != tuple(self.package_selections):
                raise ValueError("packages and package_selections disagree")
        for package in selected_packages or ():
            if not re.fullmatch(r"[A-Za-z0-9_.+@-]+", package):
                raise ValueError(f"invalid package selection {package!r}")
        if selected_packages is not None:
            normalised = tuple(selected_packages)
            object.__setattr__(self, "packages", normalised)
            object.__setattr__(self, "package_selections", normalised)
        jobs = self.jobs
        if jobs is None:
            jobs = logical_cpu_count()
            object.__setattr__(self, "jobs", jobs)
        if isinstance(jobs, bool) or not isinstance(jobs, int):
            raise ValueError("jobs must be an integer")
        maximum = logical_cpu_count()
        if jobs < 1 or jobs > maximum:
            raise ValueError(f"jobs must be between 1 and {maximum}")
        if not isinstance(self.reuse_cache, bool):
            raise ValueError("reuse_cache must be a boolean")
        if self.lock_timeout < 0:
            raise ValueError("lock_timeout must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "task_id": self.task_id,
            "snapshot_id": self.snapshot_id,
            "packages": (
                list(self.packages)
                if self.packages is not None
                else None
            ),
            "options": dict(self.options),
            "jobs": self.jobs,
            "reuse_cache": self.reuse_cache,
            "lock_timeout": self.lock_timeout,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BuildRequest":
        jobs_value = value.get("jobs")
        if jobs_value is None:
            jobs_value = value.get("parallel_jobs")
        return cls(
            device=str(value["device"]),
            task_id=str(value.get("task_id") or uuid.uuid4().hex),
            snapshot_id=value.get("snapshot_id"),
            packages=(
                tuple(value["packages"])
                if value.get("packages") is not None
                else None
            ),
            package_selections=(
                tuple(value["package_selections"])
                if value.get("package_selections") is not None
                else None
            ),
            options=dict(value.get("options") or {}),
            jobs=jobs_value,
            reuse_cache=value.get("reuse_cache", True),
            lock_timeout=float(value.get("lock_timeout", 0.0)),
        )


@dataclass
class BuildResult:
    """Stable result shape consumed by API actions and the CLI."""

    task_id: str
    device: str
    status: str
    source_id: str = ""
    snapshot_id: str = ""
    artifacts: list[str] = field(default_factory=list)
    manifest_path: str = ""
    config_path: str = ""
    config_sha256: str = ""
    log_path: str = ""
    workspace: str = ""
    exit_code: int = 0
    error: str | None = None
    started_at: str = ""
    finished_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    jobs: int | None = None
    reuse_cache: bool | None = None
    cache_reused: bool | None = None

    @property
    def artifact_paths(self) -> list[str]:
        """Compatibility alias for callers that use the longer name."""

        return self.artifacts

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "device": self.device,
            "status": self.status,
            "success": self.ok,
            "ok": self.ok,
            "source_id": self.source_id,
            "snapshot_id": self.snapshot_id,
            "artifacts": list(self.artifacts),
            "artifact_paths": list(self.artifacts),
            "manifest_path": self.manifest_path,
            "config_path": self.config_path,
            "config_sha256": self.config_sha256,
            "log_path": self.log_path,
            "workspace": self.workspace,
            "exit_code": self.exit_code,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metadata": self.metadata,
            "jobs": self.jobs,
            "reuse_cache": self.reuse_cache,
            "cache_reused": self.cache_reused,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BuildResult":
        return cls(
            task_id=str(value.get("task_id", "")),
            device=str(value.get("device", "")),
            status=str(value.get("status", "failed")),
            source_id=str(value.get("source_id", "")),
            snapshot_id=str(value.get("snapshot_id", "")),
            artifacts=[
                str(path)
                for path in (value.get("artifacts") or value.get("artifact_paths") or ())
            ],
            manifest_path=str(value.get("manifest_path", "")),
            config_path=str(value.get("config_path", "")),
            config_sha256=str(value.get("config_sha256", "")),
            log_path=str(value.get("log_path", "")),
            workspace=str(value.get("workspace", "")),
            exit_code=int(value.get("exit_code", 0)),
            error=value.get("error"),
            started_at=str(value.get("started_at", "")),
            finished_at=str(value.get("finished_at", "")),
            metadata=dict(value.get("metadata") or {}),
            jobs=(
                int(value.get("jobs", value.get("parallel_jobs")))
                if value.get("jobs", value.get("parallel_jobs")) is not None
                else None
            ),
            reuse_cache=(bool(value["reuse_cache"]) if value.get("reuse_cache") is not None else None),
            cache_reused=(bool(value["cache_reused"]) if value.get("cache_reused") is not None else None),
        )


class WorkspaceLock:
    """Process-safe exclusive lock with a small diagnostic payload."""

    def __init__(self, workspace: Path, task_id: str, timeout: float = 0.0):
        self.workspace = workspace
        self.task_id = task_id
        self.timeout = timeout
        self.path = workspace / ".build.lock"
        self._file: Any = None

    def __enter__(self) -> "WorkspaceLock":
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    raise WorkspaceBusy(
                        f"workspace is busy (lock: {self.path}); try again after the active task finishes"
                    )
                time.sleep(min(0.25, max(0.01, deadline - time.monotonic())))
        self._file.seek(0)
        self._file.truncate()
        self._file.write(
            json.dumps(
                {"task_id": self.task_id, "pid": os.getpid(), "started_at": _utc_now()},
                ensure_ascii=False,
            )
        )
        self._file.flush()
        os.fsync(self._file.fileno())
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            self._file.truncate()
            self._file.flush()
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


class BuildEngine:
    """Build OpenWrt from the reviewed device and source catalog."""

    def __init__(
        self,
        repo_root: str | Path | None = None,
        workspace: str | Path | None = None,
        *,
        catalog: DeviceCatalog | None = None,
        use_docker: bool | None = None,
        image: str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve()
        self.workspace = Path(
            workspace
            or os.environ.get("OWRT_WORKSPACE")
            or (self.repo_root / ".owrt")
        ).expanduser().resolve()
        self.catalog = catalog or load_catalog(self.repo_root / "configs" / "devices.toml")
        self.image = image or os.environ.get("OWRT_BUILDER_IMAGE", "owrt-builder:local")
        if use_docker is None:
            # ``OWRT_BUILDER_WORKER`` is reserved for a request-file worker
            # launched by the host orchestrator.  The dependency-free
            # ``./owrt`` fallback runs the CLI inside the complete builder
            # image; it is a direct build there, but must not masquerade as a
            # worker and accidentally derive paths from a missing request.
            in_worker = os.environ.get("OWRT_BUILDER_WORKER") == "1"
            in_cli_container = os.environ.get("OWRT_CLI_CONTAINER") == "1"
            use_docker = not in_worker and not in_cli_container
        self.use_docker = use_docker

    def build(
        self,
        request: BuildRequest,
        *,
        on_log: LogCallback | None = None,
        cancel_event: Any = None,
    ) -> BuildResult:
        """Run one complete compile and package operation."""

        spec = self.catalog.resolve(request.device)
        source = self.catalog.source_for(spec)
        callback = on_log or (lambda line: print(line, flush=True))
        if self.use_docker and os.environ.get("OWRT_BUILDER_WORKER") != "1":
            return self._build_docker(request, spec, source, callback, cancel_event)
        return self._build_direct(
            request,
            spec,
            source,
            callback,
            cancel_event,
            lock_held=os.environ.get("OWRT_WORKER_LOCK_HELD") == "1",
        )

    def _build_docker(
        self,
        request: BuildRequest,
        spec: DeviceSpec,
        source: SourceSpec,
        callback: LogCallback,
        cancel_event: Any,
    ) -> BuildResult:
        self._check_docker()
        task_dir = self.workspace / "tasks" / request.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        log_path = task_dir / "docker.log"
        request_path = task_dir / "request.json"
        worker_request = request.to_dict()
        # Paths and Docker mode belong to the engine/container boundary, not
        # to the public request DTO.  The worker infers its mounted paths from
        # its cwd and request-file location.
        worker_request["worker_lock_held"] = True
        _write_json(request_path, worker_request)
        container_request_path = f"/workspace/work/tasks/{request.task_id}/request.json"
        container_name = f"owrt-build-{request.task_id[:48]}"
        host_uid = str(os.getuid()) if hasattr(os, "getuid") else "0"
        host_gid = str(os.getgid()) if hasattr(os, "getgid") else "0"
        arm_privileged = spec.packager == "arm"
        # A retried task id must never be able to reuse a stale result from a
        # previous invocation.
        result_path = task_dir / "result.json"
        result_path.unlink(missing_ok=True)

        with WorkspaceLock(self.workspace, request.task_id, request.lock_timeout):
            try:
                context_fingerprint = self._context_fingerprint()
                if not self._image_exists(self.image):
                    self._emit(callback, f"构建编译镜像 {self.image}")
                    self._run_checked(
                        [
                            "docker",
                            "build",
                            "--file",
                            str(self.repo_root / "docker" / "Dockerfile"),
                            "--label",
                            f"{BUILDER_CONTEXT_LABEL}={context_fingerprint}",
                            "--tag",
                            self.image,
                            str(self.repo_root),
                        ],
                        cwd=self.repo_root,
                        callback=callback,
                        cancel_event=cancel_event,
                    )
                command = ["docker", "run", "--rm", "--init"]
                # MediaTek compilation only needs the normal container
                # sandbox.  ARM's private image packager can use loop/mount
                # operations, so retain the elevated flag only for that
                # reviewed packager path.
                if arm_privileged:
                    command.append("--privileged")
                else:
                    # The worker writes the large build tree as the invoking
                    # user.  This keeps the ordinary compiler isolated and
                    # prevents a later non-root build from inheriting root
                    # owned files.
                    command.extend(["--user", f"{host_uid}:{host_gid}"])
                command.extend(
                    [
                        "--name",
                        container_name,
                        "--env",
                        "OWRT_BUILDER_WORKER=1",
                        # The worker's stdout is piped through Docker rather
                        # than attached to a TTY.  Disable Python's block
                        # buffering so each streamed build line reaches the
                        # Web SSE log immediately.
                        "--env",
                        "PYTHONUNBUFFERED=1",
                        "--env",
                        "OWRT_WORKER_LOCK_HELD=1",
                        "--env",
                        "HOME=/tmp/owrt-home",
                        "--env",
                        "GIT_CONFIG_NOSYSTEM=1",
                        "--env",
                        f"DOWNLOAD_MIRROR={self._download_mirror()}",
                        "--volume",
                        f"{self.repo_root}:/workspace/repo:ro",
                        "--volume",
                        f"{self.workspace}:/workspace/work:rw",
                        "--workdir",
                        "/workspace/repo",
                        self.image,
                        "python3",
                        "-m",
                        "owrt_builder.cli",
                        "worker",
                        "--request-file",
                        container_request_path,
                    ]
                )
                self._emit(callback, f"启动构建容器 {container_name}")
                ownership_ok = True
                try:
                    returncode = self._run_stream(
                        command,
                        cwd=self.repo_root,
                        callback=callback,
                        cancel_event=cancel_event,
                        log_path=log_path,
                        container_name=container_name,
                    )
                finally:
                    if arm_privileged:
                        ownership_ok = self._restore_arm_ownership(
                            request,
                            spec,
                            host_uid,
                            host_gid,
                            callback,
                        )
            except BuildCancelled:
                self._remove_container(container_name)
                return self._failed_result(
                    request,
                    spec,
                    source,
                    "cancelled",
                    "build cancelled",
                    exit_code=130,
                    log_path=log_path,
                )

        if result_path.is_file():
            try:
                result = BuildResult.from_dict(json.loads(result_path.read_text(encoding="utf-8")))
                result.log_path = str(log_path)
                result = self._translate_worker_paths(result)
                if not ownership_ok and result.ok:
                    result.status = "failed"
                    result.exit_code = 1
                    result.error = "ARM build completed but workspace ownership could not be restored"
                if returncode != 0 or not result.ok:
                    # The worker's persisted result is authoritative for the
                    # detailed error, while the container exit code remains
                    # the process-level failure signal.
                    if result.ok:
                        result.status = "failed"
                    if result.exit_code == 0:
                        result.exit_code = returncode or 1
                    if not result.error:
                        result.error = f"worker exited {returncode}"
                return result
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                return self._failed_result(
                    request,
                    spec,
                    source,
                    "failed",
                    f"invalid worker result: {exc}",
                    exit_code=returncode or 1,
                    log_path=log_path,
                )
        # A zero exit without a result file is still a failed build.  A
        # successful build must publish a manifest and artifact list.
        status = "failed"
        return self._failed_result(
            request,
            spec,
            source,
            status,
            f"worker exited {returncode}; worker result.json is missing",
            exit_code=returncode or 1,
            log_path=log_path,
        )

    def _restore_arm_ownership(
        self,
        request: BuildRequest,
        spec: DeviceSpec,
        host_uid: str,
        host_gid: str,
        callback: LogCallback,
    ) -> bool:
        """Return ARM worker files to the invoking user.

        The ARM packit scripts call ``mount`` while constructing the image,
        which requires the worker to run as root inside its privileged
        container.  The worker still writes into the host-mounted workspace;
        use a short, unprivileged-on-the-host Docker helper to repair only
        that workspace's mutable paths before the next task runs.  The helper
        deliberately has neither host networking nor ``--privileged``.
        """

        if host_uid == "0":
            return True
        relative_paths = (
            # The ARM worker remains root for the compiler/cache and those
            # trees can be tens of GiB.  They are intentionally left owned by
            # root and are readable by the host; only task evidence and the
            # copied firmware need to be writable by the invoking user.
            Path("tasks") / request.task_id,
            Path("artifacts") / spec.key / request.task_id,
        )
        existing = [
            path
            for path in relative_paths
            if (self.workspace / path).exists() or (self.workspace / path).is_symlink()
        ]
        if not existing:
            return True
        command = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--user",
            "0:0",
            "--volume",
            f"{self.workspace}:/workspace/work:rw",
            self.image,
            "sh",
            "-ec",
            (
                'uid="$1"; gid="$2"; shift 2; '
                'for path in "$@"; do '
                'if [ -d "$path" ] && [ ! -L "$path" ]; then '
                'chown -R "$uid:$gid" "$path"; '
                'else chown "$uid:$gid" "$path"; fi; '
                'done'
            ),
            "sh",
            host_uid,
            host_gid,
            *[f"/workspace/work/{path}" for path in existing],
        ]
        self._emit(callback, "恢复 ARM 工作区文件权限")
        try:
            returncode = self._run_stream(
                command,
                cwd=self.repo_root,
                callback=callback,
                cancel_event=None,
            )
        except (OSError, BuildError) as exc:
            self._emit(callback, f"恢复 ARM 工作区文件权限失败: {exc}")
            return False
        if returncode != 0:
            self._emit(callback, f"恢复 ARM 工作区文件权限失败，退出码 {returncode}")
            return False
        return True

    def _translate_worker_paths(self, result: BuildResult) -> BuildResult:
        """Translate worker ``/workspace/work`` paths back to host paths."""

        work_prefix = "/workspace/work"
        host_prefix = str(self.workspace)

        def translate(value: str) -> str:
            if value == work_prefix:
                return host_prefix
            if value.startswith(work_prefix + "/"):
                return host_prefix + value[len(work_prefix) :]
            return value

        result.artifacts = [translate(path) for path in result.artifacts]
        result.manifest_path = translate(result.manifest_path)
        result.config_path = translate(result.config_path)
        result.log_path = translate(result.log_path)
        result.workspace = translate(result.workspace)
        return result

    def _build_direct(
        self,
        request: BuildRequest,
        spec: DeviceSpec,
        source: SourceSpec,
        callback: LogCallback,
        cancel_event: Any,
        *,
        lock_held: bool,
    ) -> BuildResult:
        # Workspace selection is an engine concern.  The public request only
        # carries a device, an optional immutable snapshot, and selections.
        workspace = self.workspace
        task_dir = workspace / "tasks" / request.task_id
        build_dir = workspace / "builds" / request.task_id
        artifact_dir = workspace / "artifacts" / spec.key / request.task_id
        log_path = task_dir / "build.log"
        task_dir.mkdir(parents=True, exist_ok=True)
        for scratch in (build_dir, artifact_dir):
            if scratch.is_symlink() or scratch.is_file():
                scratch.unlink(missing_ok=True)
            elif scratch.is_dir():
                shutil.rmtree(scratch)
        build_dir.mkdir(parents=True, exist_ok=True)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")

        parent_callback = callback

        def callback(line: str) -> None:  # type: ignore[no-redef]
            parent_callback(line)
            log_handle.write(line + "\n")
            log_handle.flush()

        started_at = _utc_now()
        source_result: PreparedSource | None = None
        config_path = build_dir / "generated.config"
        result: BuildResult | None = None
        cache_reused: bool | None = None
        cache_manager = BuildCacheManager(
            workspace,
            device_resolver=lambda value: self.catalog.resolve(value).key,
        )

        def execute() -> BuildResult:
            nonlocal source_result, config_path, cache_reused
            try:
                self._emit(callback, f"设备: {spec.key} ({spec.description})")
                self._emit(callback, f"配置: {spec.config}")
                source_result = self._prepare_snapshot(
                    spec,
                    request.snapshot_id,
                    workspace,
                    callback,
                    cancel_event,
                )
                cache_lease = None
                try:
                    cache_lease = cache_manager.acquire(
                        spec.key,
                        request.task_id,
                        source_id=source_result.source_id,
                        snapshot_id=source_result.snapshot_id,
                        reuse=request.reuse_cache,
                        legacy_aliases=spec.all_aliases,
                        callback=callback,
                        lock_timeout=request.lock_timeout,
                    )
                    cache_reused = cache_lease.reused
                    if cache_lease.reused:
                        previous = cache_lease.previous
                        if previous and previous.snapshot_id != source_result.snapshot_id:
                            cache_manager.refresh_source(
                                cache_lease,
                                source_result.path,
                                source_id=source_result.source_id,
                                snapshot_id=source_result.snapshot_id,
                                source_commit=source_result.source_commit,
                                callback=callback,
                            )
                        openwrt_dir = cache_lease.source_root
                    else:
                        openwrt_dir = self._copy_snapshot(
                            source_result.path,
                            cache_lease.root,
                            callback,
                            workspace=workspace,
                        )
                        cache_manager.touch(
                            spec.key,
                            task_id=request.task_id,
                            source_id=source_result.source_id,
                            snapshot_id=source_result.snapshot_id,
                            source_commit=source_result.source_commit,
                            config_sha256="",
                            ready=False,
                        )
                    try:
                        prepared_catalog = Catalog.read(source_result.catalog_path)
                    except Exception as exc:
                        raise BuildError(f"unable to load prepared package catalog: {exc}") from exc
                    if not prepared_catalog.authoritative:
                        raise BuildError("prepared package catalog is not authoritative")
                    config_path = self._write_config(
                        spec,
                        source,
                        openwrt_dir,
                        build_dir,
                        request.packages,
                        request.options,
                        prepared_catalog,
                    )
                    config_sha256 = _sha256(config_path)
                    if (
                        cache_lease.reused
                        and cache_lease.previous
                        and cache_lease.previous.config_sha256
                        and cache_lease.previous.config_sha256 != config_sha256
                    ):
                        self._emit(callback, "检测到配置变化，复用设备编译缓存并重新运行 defconfig")
                    cache_manager.update_source(
                        cache_lease,
                        source_id=source_result.source_id,
                        snapshot_id=source_result.snapshot_id,
                        source_commit=source_result.source_commit,
                        config_sha256=config_sha256,
                        ready=False,
                    )
                    self._run_pipeline(
                        spec,
                        source_result,
                        openwrt_dir,
                        config_path,
                        callback,
                        cancel_event,
                        jobs=request.jobs,
                    )
                    artifacts = self._package(
                        spec,
                        source_result,
                        openwrt_dir,
                        artifact_dir,
                        callback,
                        cancel_event,
                        started_at,
                    )
                    cache_manager.update_source(
                        cache_lease,
                        source_id=source_result.source_id,
                        snapshot_id=source_result.snapshot_id,
                        source_commit=source_result.source_commit,
                        config_sha256=config_sha256,
                        ready=True,
                    )
                finally:
                    if cache_lease is not None:
                        cache_lease.release()
                finished_at = _utc_now()
                metadata = self._artifact_metadata(artifacts)
                manifest = self._manifest(
                    request,
                    spec,
                    source_result,
                    config_path,
                    artifacts,
                    metadata,
                    status="success",
                    started_at=started_at,
                    finished_at=finished_at,
                    error=None,
                    cache_reused=cache_reused,
                )
                manifest_path = artifact_dir / "manifest.json"
                _write_json(manifest_path, manifest)
                result = BuildResult(
                    task_id=request.task_id,
                    device=spec.key,
                    status="success",
                    source_id=source_result.source_id,
                    snapshot_id=source_result.snapshot_id,
                    artifacts=[str(path) for path in artifacts],
                    manifest_path=str(manifest_path),
                    config_path=str(config_path),
                    config_sha256=_sha256(config_path),
                    log_path=str(log_path),
                    workspace=str(workspace),
                    exit_code=0,
                    started_at=started_at,
                    finished_at=finished_at,
                    metadata=metadata,
                    jobs=request.jobs,
                    reuse_cache=request.reuse_cache,
                    cache_reused=cache_reused,
                )
                return result
            except BuildCancelled:
                finished_at = _utc_now()
                error = "build cancelled"
                return self._write_failure(
                    request,
                    spec,
                    source_result,
                    config_path,
                    artifact_dir,
                    log_path,
                    workspace,
                    started_at,
                    finished_at,
                    error,
                    status="cancelled",
                    exit_code=130,
                    cache_reused=cache_reused,
                )
            except Exception as exc:  # noqa: BLE001 - preserve failure in manifest
                finished_at = _utc_now()
                self._emit(callback, f"构建失败: {exc}")
                return self._write_failure(
                    request,
                    spec,
                    source_result,
                    config_path,
                    artifact_dir,
                    log_path,
                    workspace,
                    started_at,
                    finished_at,
                    str(exc),
                    status="failed",
                    exit_code=getattr(exc, "returncode", 1) or 1,
                    cache_reused=cache_reused,
                )

        if lock_held:
            result = execute()
        else:
            with WorkspaceLock(workspace, request.task_id, request.lock_timeout):
                result = execute()
        assert result is not None
        _write_json(task_dir / "result.json", result.to_dict())
        log_handle.close()
        return result

    def run(
        self,
        request: BuildRequest,
        on_log: LogCallback | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> BuildResult:
        """Web-worker compatibility wrapper around :meth:`build`.

        The HTTP worker supplies a predicate because cancellation can come
        from SQLite as well as a local event.  The core still polls it at the
        subprocess boundary and therefore terminates the actual Docker
        container rather than merely changing the database status.
        """

        class _PredicateEvent:
            def is_set(self) -> bool:
                return bool(is_cancelled and is_cancelled())

        return self.build(
            request,
            on_log=on_log,
            cancel_event=_PredicateEvent() if is_cancelled else None,
        )

    def compose_config(
        self,
        request: BuildRequest,
        prepared: PreparedSource,
        build_dir: str | Path,
    ) -> Path:
        """Compose the exact generated config used by :meth:`build`.

        Web validation calls this public seam so its ``make defconfig`` input
        includes the reviewed device target/config fragment and optional
        ``CONFIG_APPEND`` file.  Keeping composition here prevents the API
        layer from quietly validating a different configuration than the
        compiler receives.
        """

        spec = self.catalog.resolve(request.device)
        source = self.catalog.source_for(spec)
        destination = Path(build_dir).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        return self._write_config(
            spec,
            source,
            Path(prepared.path).expanduser().resolve(),
            destination,
            request.packages,
            request.options,
            Catalog.read(prepared.catalog_path),
        )

    def cancel(self, task_id: str) -> None:
        """Terminate the real task container, if one exists."""

        _safe_task_id(task_id)
        self._remove_container(f"owrt-build-{task_id[:48]}")

    def inspect(self, task_id: str) -> str:
        """Return Docker state for reconciliation after a web restart."""

        _safe_task_id(task_id)
        name = f"owrt-build-{task_id[:48]}"
        completed = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
        result_path = self.workspace / "tasks" / task_id / "result.json"
        if result_path.is_file():
            try:
                status = str(json.loads(result_path.read_text(encoding="utf-8")).get("status", ""))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                status = ""
            return {"success": "succeeded", "failed": "failed", "cancelled": "canceled"}.get(status, status or "unknown")
        return "unknown"

    def _prepare_snapshot(
        self,
        spec: DeviceSpec,
        requested_snapshot: str | None,
        workspace: Path,
        callback: LogCallback,
        cancel_event: Any,
    ) -> PreparedSource:
        """Obtain a source snapshot through the shared SourceManager."""

        manager = SourceManager(workspace / "sources", source_specs=self.catalog.sources)

        def source_status(event: Mapping[str, Any]) -> None:
            phase = event.get("phase", "source")
            message = event.get("message", "")
            self._emit(callback, f"[{phase}] {message}")

        if requested_snapshot:
            try:
                prepared = manager.get_snapshot(requested_snapshot)
            except KeyError as exc:
                raise BuildError(
                    f"source snapshot missing: {requested_snapshot!r}; refresh sources and retry"
                ) from exc
            except SourceError as exc:
                raise BuildError(
                    f"source snapshot invalid: {requested_snapshot!r}: {exc}"
                ) from exc
            if prepared.source_id != spec.source_id:
                raise BuildError(
                    f"snapshot {requested_snapshot!r} belongs to {prepared.source_id!r}, "
                    f"not {spec.source_id!r}"
                )
            return prepared

        prepared = manager.current(spec)
        if prepared is None:
            try:
                prepared = manager.prepare_source(spec, update=True, status=source_status)
            except SourceError as exc:
                raise BuildError(str(exc)) from exc
        self._emit(callback, f"源码快照: {prepared.snapshot_id} ({prepared.source_commit})")
        return prepared

    @staticmethod
    def _copy_snapshot(
        snapshot_path: Path,
        build_dir: Path,
        callback: LogCallback,
        *,
        workspace: Path | None = None,
    ) -> Path:
        if not snapshot_path.is_dir():
            raise BuildError(f"prepared source path missing: {snapshot_path}")
        destination = build_dir / "openwrt"
        BuildEngine._emit(callback, f"复制不可变源码快照到 {destination}")
        workspace = workspace or build_dir.parent.parent
        download_cache = workspace / "cache" / "dl"
        try:
            stage_download_seeds(snapshot_path, download_cache)
        except SourceError as exc:
            raise BuildError(f"invalid tracked download seed: {exc}") from exc
        shutil.copytree(snapshot_path, destination, symlinks=True)
        # A published source snapshot is deliberately independent of the
        # preparation container, but older snapshots may still contain
        # host-generated state.  Remove it again at the build boundary so an
        # absolute interpreter symlink or stale host tool cannot leak into a
        # new worker image.  The package catalog lives beside the source and
        # is rebuilt from the clean tree before configuration is composed.
        for name in ("build_dir", "staging_dir", "tmp", "dl", "logs", "bin"):
            path = destination / name
            if not (path.exists() or path.is_symlink()):
                continue
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
        download_cache.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.relpath(download_cache, destination), destination / "dl")
        return destination

    @staticmethod
    def _download_mirror() -> str:
        """Return the configured OpenWrt download mirror.

        Savannah and a few other upstream hosts are intermittently
        unreachable from home networks.  OpenWrt publishes a matching cache
        for the standard source archives; administrators can override it with
        ``OWRT_DOWNLOAD_MIRROR`` (semicolon separated values are accepted by
        the upstream ``download.pl`` script).
        """

        return (
            os.environ.get("OWRT_DOWNLOAD_MIRROR")
            or os.environ.get("DOWNLOAD_MIRROR")
            or "https://sources.cdn.openwrt.org"
        )

    def _write_config(
        self,
        spec: DeviceSpec,
        source: SourceSpec,
        openwrt_dir: Path,
        build_dir: Path,
        package_selections: Iterable[str] | None,
        options: Mapping[str, Any] | None = None,
        catalog: Catalog | None = None,
    ) -> Path:
        config_root = self.repo_root / "configs"
        device_config = config_root / spec.config
        if not device_config.is_file():
            raise BuildError(f"device config not found: {device_config}")
        try:
            append_text, fragment_text = compose_fragment_parts(
                device_config,
                defconfig_dir=openwrt_dir / "defconfig",
            )
            device_text = (
                append_text + "\n" + fragment_text
                if append_text
                else fragment_text
            )
        except Exception as exc:
            raise BuildError(f"unable to compose device config: {exc}") from exc
        if catalog is None:
            raise BuildError(
                "an authoritative prepared package catalog is required; "
                "do not scan a build tree after generated metadata cleanup"
            )
        if not catalog.authoritative:
            raise BuildError("prepared package catalog is not authoritative")

        # Validate package names and determine the base package selected by the
        # reviewed device config.  A package option is never a package by
        # itself; it must belong to one of these base packages.
        known_packages = {item.name for item in catalog.packages}
        requested_packages = tuple(package_selections or ())
        unknown_packages = [name for name in requested_packages if name not in known_packages]
        if unknown_packages:
            raise BuildError("unknown package(s) in request: " + ", ".join(unknown_packages))
        reviewed_doc = parse_config(device_text)
        reviewed_packages = {
            item.name
            for item in catalog.packages
            if reviewed_doc.get_entry(item.symbol) is not None
            and reviewed_doc.get_entry(item.symbol).value in {"y", "m"}  # type: ignore[union-attr]
        }
        selected_packages = set(requested_packages) if package_selections is not None else reviewed_packages

        def option_symbol(raw: str) -> str:
            value = str(raw).strip()
            if value.startswith("CONFIG_"):
                return value
            if value.startswith("PACKAGE_"):
                return "CONFIG_" + value
            return "CONFIG_" + value

        def option_value(value: Any) -> str:
            if isinstance(value, bool):
                return "y" if value else "n"
            if value is None:
                return "n"
            if isinstance(value, int) and not isinstance(value, bool):
                return str(value)
            text = str(value)
            if text in {"y", "m", "n"} or re.fullmatch(r"[-+]?\d+", text):
                return text
            if re.fullmatch(r"0[xX][0-9a-fA-F]+", text):
                return text
            if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
                return text
            return json.dumps(text, ensure_ascii=False)

        option_entries: list[ConfigEntry] = []
        for raw_symbol, raw_value in (options or {}).items():
            symbol = option_symbol(str(raw_symbol))
            if not re.fullmatch(r"CONFIG_[A-Za-z0-9_.+@-]+", symbol):
                raise BuildError(f"invalid package option symbol: {raw_symbol!r}")
            owner_name, is_base, option = catalog.symbol_info(symbol)
            if owner_name is None or is_base or option is None:
                raise BuildError(f"unknown package option: {raw_symbol}")
            package = catalog.package(owner_name)
            if package is None:
                raise BuildError(f"unknown package option owner: {raw_symbol}")
            if package.name not in selected_packages:
                raise BuildError(
                    f"package option {symbol} belongs to unselected package {package.name}"
                )
            option_entries.append(ConfigEntry(symbol, option_value(raw_value)))

        requested_entries = [ConfigEntry(entry.symbol, entry.value, raw=entry.raw) for entry in option_entries]
        validation = validate_fragment(catalog, ConfigDocument(requested_entries, source="build-request"))
        if any(issue.error for issue in validation.issues):
            messages = "; ".join(issue.message for issue in validation.issues)
            raise BuildError(f"invalid package options: {messages}")

        output = build_dir / "generated.config"
        chunks: list[str] = [
            "# Generated by owrt_builder; source and device are recorded in manifest.json.\n",
        ]
        mediatek_target_lines = [
            "CONFIG_TARGET_mediatek_filogic=y\n",
            f"CONFIG_TARGET_mediatek_filogic_DEVICE_{spec.profile}=y\n",
            f"CONFIG_TARGET_DEVICE_mediatek_filogic_DEVICE_{spec.profile}=y\n",
            f'CONFIG_TARGET_PROFILE="DEVICE_{spec.profile}"\n',
        ] if spec.packager == "mediatek" else []
        target_symbols = {
            parse_config(line).entries[0].symbol
            for line in mediatek_target_lines
            if parse_config(line).entries
        }
        # The reviewed fragment may already contain target/profile entries.
        # Remove those exact symbols before adding the canonical target block
        # once, avoiding duplicate Kconfig assignments and choice overrides.
        def remove_target_symbols(text: str) -> str:
            return "".join(
                line
                for line in text.splitlines(keepends=True)
                if not any(entry.symbol in target_symbols for entry in parse_config(line).entries)
            )

        device_text_for_output = remove_target_symbols(device_text)
        append_text_for_output = remove_target_symbols(append_text)
        fragment_text_for_output = remove_target_symbols(fragment_text)
        base_config = config_root / "base.config"
        if base_config.is_file():
            chunks.append(base_config.read_text(encoding="utf-8"))
            chunks.append("\n")
        if package_selections is None:
            # None means the reviewed device fragment, including its default
            # plugin set, is authoritative.
            chunks.append(device_text_for_output)
        else:
            # An explicit package list is a deliberate replacement of every
            # package symbol and package-local option in the main device
            # fragment, including ``# CONFIG_PACKAGE_x is not set`` entries.
            # The CONFIG_APPEND file is a hardware baseline and must remain
            # intact even when the Web UI sends an explicit package list.
            # Parsing against the prepared ownership map avoids leaving an old
            # disabled option behind when a child Kconfig symbol has a
            # different prefix.
            owned_symbols = {
                item.symbol
                for item in catalog.packages
            }
            owned_symbols.update(
                option.symbol
                for item in catalog.packages
                for option in item.options
            )
            chunks.append(append_text_for_output)
            chunks.append(
                "".join(
                    line
                    for line in fragment_text_for_output.splitlines(keepends=True)
                    if not any(
                        entry.symbol in owned_symbols
                        for entry in parse_config(line).entries
                    )
                )
            )
            chunks.append("\n")
        chunks.append("\n")
        chunks.extend(mediatek_target_lines)
        for package in package_selections or ():
            chunks.append(f"CONFIG_PACKAGE_{package}=y\n")
        for entry in option_entries:
            chunks.append(f"{entry.symbol}={entry.value}\n")
        rendered = _deduplicate_config_assignments("".join(chunks))
        rendered_doc = parse_config(rendered)
        for symbol in target_symbols:
            if sum(entry.symbol == symbol for entry in rendered_doc.entries) != 1:
                raise BuildError(f"generated config contains duplicate target symbol: {symbol}")
        output.write_text(rendered, encoding="utf-8")
        return output

    def _run_pipeline(
        self,
        spec: DeviceSpec,
        prepared: PreparedSource,
        openwrt_dir: Path,
        config_path: Path,
        callback: LogCallback,
        cancel_event: Any,
        *,
        jobs: int,
    ) -> None:
        env = os.environ.copy()
        env.update(
            {
                "FORCE_UNSAFE_CONFIGURE": "1",
                "OWRT_SOURCE_SNAPSHOT": prepared.snapshot_id,
                "OWRT_DEVICE": spec.key,
                "OWRT_PROFILE": spec.profile,
                "DOWNLOAD_MIRROR": self._download_mirror(),
                # OpenWrt's default silent recipes make a long host-tool or
                # package compile indistinguishable from a hung worker.  The
                # detailed mode keeps Web/SSE and CLI logs useful; callers
                # can opt out with OWRT_MAKE_VERBOSE=0 when collecting only
                # high-level output.
                "V": os.environ.get("OWRT_MAKE_VERBOSE", "s"),
            }
        )
        # SourceManager has already staged native feeds, project feeds,
        # Passwall packages and the authoritative package catalog.  Running
        # the legacy hooks again here would follow moving branches and could
        # invalidate the snapshot hash, so the worker consumes the prepared
        # tree as-is.
        self._emit(callback, "使用已准备的源码与 feed 快照")
        shutil.copyfile(config_path, openwrt_dir / ".config")
        self._run_checked(
            ["make", "defconfig"],
            cwd=openwrt_dir,
            env=env,
            callback=callback,
            cancel_event=cancel_event,
        )
        self._run_checked(
            ["make", "download"],
            cwd=openwrt_dir,
            env=env,
            callback=callback,
            cancel_event=cancel_event,
            retry_once=True,
        )
        self._run_compile_with_diagnostics(
            openwrt_dir,
            env,
            callback,
            cancel_event,
            jobs=jobs,
        )

    def _run_compile_with_diagnostics(
        self,
        openwrt_dir: Path,
        env: Mapping[str, str],
        callback: LogCallback,
        cancel_event: Any,
        *,
        jobs: int,
    ) -> None:
        """Run the formal compile and retain a verbose failure diagnostic.

        OpenWrt's parallel make output often ends with only the failing target.
        If that formal compile fails, immediately rerun the same tree in
        serial verbose mode (``make V=s -j1``).  A successful diagnostic
        confirms that the parallel failure was transient and lets the build
        continue to packaging; a second failure is raised with the detailed
        command's error while the original failure remains in the log.
        """

        command = ["make", f"-j{jobs}"]
        formal_error: CommandFailed | None = None
        try:
            self._run_checked(
                command,
                cwd=openwrt_dir,
                env=env,
                callback=callback,
                cancel_event=cancel_event,
            )
            return
        except BuildCancelled:
            raise
        except CommandFailed as exc:
            formal_error = exc
            self._emit(callback, "")
            self._emit(callback, "========== 正式编译失败，开始串行详细诊断：make V=s -j1 ==========")
            self._emit(callback, f"首次正式编译错误：{formal_error}")

        # The event can be set after the formal command exits but before the
        # diagnostic command is spawned.  Do not launch a second make in that
        # case; _run_stream also checks the event while the command runs.
        if cancel_event is not None and cancel_event.is_set():
            self._emit(callback, "已收到取消请求，跳过详细诊断编译")
            raise BuildCancelled()

        diagnostic_command = ["make", "V=s", "-j1"]
        try:
            self._run_checked(
                diagnostic_command,
                cwd=openwrt_dir,
                env=env,
                callback=callback,
                cancel_event=cancel_event,
            )
            if cancel_event is not None and cancel_event.is_set():
                raise BuildCancelled()
            self._emit(callback, "串行详细诊断编译成功，继续后续打包")
        except BuildCancelled:
            self._emit(callback, "详细诊断编译期间收到取消请求，已终止诊断")
            raise
        except Exception as diagnostic_error:  # noqa: BLE001 - preserve both errors in the log
            self._emit(callback, f"串行详细诊断编译失败：{diagnostic_error}")
            raise
        finally:
            self._emit(callback, "========== 详细诊断结束；首次并行编译日志已保留 ==========")

    def _package(
        self,
        spec: DeviceSpec,
        prepared: PreparedSource,
        openwrt_dir: Path,
        artifact_dir: Path,
        callback: LogCallback,
        cancel_event: Any,
        started_at: str,
    ) -> list[Path]:
        if spec.packager == "arm":
            return self._package_arm(spec, prepared, openwrt_dir, artifact_dir, callback, cancel_event)
        return self._package_mediatek(spec, prepared, openwrt_dir, artifact_dir, callback, cancel_event)

    def _package_mediatek(
        self,
        spec: DeviceSpec,
        prepared: PreparedSource,
        openwrt_dir: Path,
        artifact_dir: Path,
        callback: LogCallback,
        cancel_event: Any,
    ) -> list[Path]:
        target_dir = openwrt_dir / "bin" / "targets" / "mediatek" / "filogic"
        if not target_dir.is_dir():
            raise BuildError(f"mediatek target directory missing: {target_dir}")
        candidates = sorted(
            path
            for path in target_dir.glob("*.bin")
            if spec.profile in path.name and path.stat().st_size > 0
        )
        if not candidates:
            raise BuildError(f"no {spec.profile} firmware found in {target_dir}")
        outputs: list[Path] = []
        for candidate in candidates:
            destination = artifact_dir / candidate.name
            shutil.copyfile(candidate, destination)
            os.utime(destination, None)
            outputs.append(destination)
        # OpenWrt's append-metadata step stores supported_devices in the
        # fwtool JSON trailer.  Check the N60 Pro value so a generic image
        # with a misleading filename cannot pass acceptance.
        if spec.profile == "netcore_n60-pro":
            for output in outputs:
                if "sysupgrade" not in output.name:
                    continue
                supported_devices = self._firmware_supported_devices(output, openwrt_dir)
                if "netcore,n60-pro" not in supported_devices and "netcore_n60-pro" not in supported_devices:
                    raise BuildError(
                        f"{output.name} metadata does not contain netcore,n60-pro"
                    )
        self._emit(callback, f"固件产物: {len(outputs)} 个 mediatek .bin")
        return outputs

    def _package_arm(
        self,
        spec: DeviceSpec,
        prepared: PreparedSource,
        openwrt_dir: Path,
        artifact_dir: Path,
        callback: LogCallback,
        cancel_event: Any,
    ) -> list[Path]:
        # ARM packaging is intentionally a private post-build operation.  The
        # old shell entry point also exposed menuconfig, download-only,
        # package-only and x86 branches; package_arm has exactly two reviewed
        # board scripts and propagates every failure.
        rootfs = (
            openwrt_dir
            / "bin"
            / "targets"
            / "armsr"
            / "armv8"
            / "immortalwrt-armsr-armv8-generic-rootfs.tar.gz"
        )
        try:
            packaged = package_arm(
                device=spec.key,
                rootfs_path=rootfs,
                workspace=self.workspace,
                callback=callback,
                cancel_event=cancel_event,
            )
        except ArmPackagerError as exc:
            raise BuildError(str(exc)) from exc
        outputs: list[Path] = []
        for candidate in packaged.artifacts:
            destination = artifact_dir / candidate.name
            shutil.copyfile(candidate, destination)
            os.utime(destination, None)
            outputs.append(destination)
        self._emit(callback, f"固件产物: {len(outputs)} 个 ARM 打包文件")
        return outputs

    @staticmethod
    def _firmware_supported_devices(path: Path, openwrt_dir: Path) -> list[str]:
        """Read OpenWrt's appended firmware metadata.

        MediaTek sysupgrade images are tar payloads with an additional
        fwtool JSON chunk appended after the tar stream.  The ``CONTROL``
        member contains board variables, not the authoritative
        ``supported_devices`` list, so prefer the target tree's fwtool.  A
        tar/text fallback keeps this check useful for older image formats.
        """

        candidates = [openwrt_dir / "staging_dir" / "host" / "bin" / "fwtool"]
        host_fwtool = shutil.which("fwtool")
        if host_fwtool:
            candidates.append(Path(host_fwtool))
        seen: set[Path] = set()
        for fwtool in candidates:
            try:
                resolved = fwtool.resolve()
            except OSError:
                resolved = fwtool
            if resolved in seen or not fwtool.is_file():
                continue
            seen.add(resolved)
            try:
                completed = subprocess.run(
                    [str(fwtool), "-i", "/dev/stdout", str(path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    check=False,
                )
            except OSError:
                continue
            if completed.returncode != 0 or not completed.stdout.strip():
                continue
            try:
                payload = json.loads(completed.stdout)
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, Mapping):
                values = payload.get("supported_devices")
                if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                    return [str(value) for value in values]

        fallback = BuildEngine._tar_metadata(path)
        try:
            payload = json.loads(fallback)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, Mapping):
            values = payload.get("supported_devices")
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                return [str(value) for value in values]
        # Some legacy metadata writers emit a simple key/value line instead
        # of JSON.  Only return values from that explicit field.
        match = re.search(r"(?im)^\s*supported_devices\s*[=:]\s*([^\r\n]+)", fallback)
        if match:
            return [
                item.strip().strip('"').strip("'")
                for item in match.group(1).split(",")
                if item.strip()
            ]
        return []

    @staticmethod
    def _tar_metadata(path: Path) -> str:
        # Sysupgrade images are tar archives whose top-level directory is
        # commonly ``sysupgrade-<profile>/``.  The metadata therefore is not
        # necessarily addressable as ``CONTROL/metadata`` at archive root;
        # discover the single CONTROL metadata member first and then extract
        # that exact member.  Keeping the member name supplied by tar avoids
        # guessing from the firmware filename.
        listing = subprocess.run(
            ["tar", "-tf", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if listing.returncode != 0:
            return ""
        members = []
        for raw in listing.stdout.splitlines():
            member = raw.strip().rstrip("/")
            normalized = member.lstrip("./")
            if normalized == "CONTROL/metadata" or normalized.endswith("/CONTROL/metadata"):
                members.append(member)
        for member in members:
            completed = subprocess.run(
                ["tar", "-xOf", str(path), member],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            if completed.returncode == 0:
                return completed.stdout
        return ""

    def _manifest(
        self,
        request: BuildRequest,
        spec: DeviceSpec,
        prepared: PreparedSource,
        config_path: Path,
        artifacts: Sequence[Path],
        metadata: Mapping[str, Any],
        *,
        status: str,
        started_at: str,
        finished_at: str,
        error: str | None,
        cache_reused: bool | None = None,
    ) -> dict[str, Any]:
        source = self.catalog.source_for(spec)
        source_snapshot = self._portable_source(prepared)
        return {
            "schema_version": 1,
            "task_id": request.task_id,
            "status": status,
            "requested_device": request.device,
            "device": spec.to_dict(source),
            "source": source_snapshot,
            "reviewed_source": source.to_dict(),
            "build_controls": {
                "jobs": request.jobs,
                "reuse_cache": request.reuse_cache,
                "cache_reused": cache_reused,
            },
            "config": {
                "path": self._portable_workspace_path(config_path),
                "sha256": _sha256(config_path) if config_path.is_file() else "",
                "package_selections": (
                    list(request.packages)
                    if request.packages is not None
                    else None
                ),
                "options": dict(request.options),
            },
            "artifacts": dict(metadata),
            "started_at": started_at,
            "finished_at": finished_at,
            "error": error,
        }

    @staticmethod
    def _portable_source(prepared: PreparedSource) -> dict[str, Any]:
        """Return source provenance without embedding a worker mount path."""

        value = prepared.to_dict()
        relative = f"sources/{prepared.source_id}/snapshots/{prepared.snapshot_id}"
        value["path"] = f"{relative}/source"
        value["catalog_path"] = f"{relative}/catalog.json"
        return value

    def _portable_workspace_path(self, path: Path) -> str:
        """Store a workspace-relative path in a manifest.

        Worker manifests are written inside Docker, where the workspace is
        mounted at ``/workspace/work``.  Relative paths remain meaningful to
        the host, Web API and downloaded manifest consumers.
        """

        try:
            return str(path.resolve().relative_to(self.workspace.resolve()))
        except ValueError:
            return str(path)

    @staticmethod
    def _artifact_metadata(artifacts: Sequence[Path]) -> dict[str, Any]:
        # Store stable names in the manifest.  Worker paths are mounted as
        # ``/workspace/work`` inside Docker and are translated on the result
        # DTO, while a manifest is already persisted by the worker itself.
        # Absolute container paths would therefore be unusable to a host or
        # Web client.  The artifact directory is the allowlisted parent.
        return {
            path.name: {
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "mtime_ns": path.stat().st_mtime_ns,
            }
            for path in artifacts
        }

    def _write_failure(
        self,
        request: BuildRequest,
        spec: DeviceSpec,
        prepared: PreparedSource | None,
        config_path: Path,
        artifact_dir: Path,
        log_path: Path,
        workspace: Path,
        started_at: str,
        finished_at: str,
        error: str,
        *,
        status: str,
        exit_code: int,
        cache_reused: bool | None = None,
    ) -> BuildResult:
        source_spec = self.catalog.source_for(spec)
        if prepared is not None:
            manifest_source = self._portable_source(prepared)
        else:
            manifest_source = {
                "source_id": source_spec.id,
                "snapshot_id": request.snapshot_id or "",
                "path": "",
                "branch": source_spec.branch,
                "url": source_spec.url,
            }
        manifest = {
            "schema_version": 1,
            "task_id": request.task_id,
            "status": status,
            "requested_device": request.device,
            "device": spec.to_dict(source_spec),
            "source": manifest_source,
            "reviewed_source": source_spec.to_dict(),
            "build_controls": {
                "jobs": request.jobs,
                "reuse_cache": request.reuse_cache,
                "cache_reused": cache_reused,
            },
            "config": {
                "path": self._portable_workspace_path(config_path),
                "sha256": _sha256(config_path) if config_path.is_file() else "",
                "package_selections": (
                    list(request.packages)
                    if request.packages is not None
                    else None
                ),
                "options": dict(request.options),
            },
            "artifacts": {},
            "started_at": started_at,
            "finished_at": finished_at,
            "error": error,
            "traceback": traceback.format_exc(limit=8),
        }
        artifact_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = artifact_dir / "manifest.json"
        _write_json(manifest_path, manifest)
        result = BuildResult(
            task_id=request.task_id,
            device=spec.key,
            status=status,
            source_id=manifest_source["source_id"],
            snapshot_id=manifest_source.get("snapshot_id", "") or "",
            artifacts=[],
            manifest_path=str(manifest_path),
            config_path=str(config_path),
            config_sha256=manifest["config"]["sha256"],
            log_path=str(log_path),
            workspace=str(workspace),
            exit_code=exit_code,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            jobs=request.jobs,
            reuse_cache=request.reuse_cache,
            cache_reused=cache_reused,
        )
        return result

    def _failed_result(
        self,
        request: BuildRequest,
        spec: DeviceSpec,
        source: SourceSpec,
        status: str,
        error: str | None,
        *,
        exit_code: int,
        log_path: Path,
    ) -> BuildResult:
        return BuildResult(
            task_id=request.task_id,
            device=spec.key,
            status=status,
            source_id=source.id,
            snapshot_id=request.snapshot_id or "",
            log_path=str(log_path),
            workspace=str(self.workspace),
            exit_code=exit_code,
            error=error,
            jobs=request.jobs,
            reuse_cache=request.reuse_cache,
        )

    @staticmethod
    def _emit(callback: LogCallback, line: str) -> None:
        try:
            callback(line)
        except TypeError:
            # A few API adapters accept structured event callbacks.  Keep the
            # core dependency-free and offer a graceful string fallback.
            callback(str(line))

    def _check_docker(self) -> None:
        try:
            completed = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise BuildError("Docker is required; install Docker Engine and retry") from exc
        if completed.returncode != 0 or not completed.stdout.strip():
            detail = completed.stderr.strip() or "Docker daemon is unavailable"
            raise BuildError(detail)

    def _context_fingerprint(self) -> str:
        """Hash files that affect the worker image or its runtime code.

        The repository is mounted into the worker, so a stale tag can execute
        a dependency layer built for an older checkout.  A content label makes
        that boundary explicit and lets both the host launcher and ``doctor``
        report whether the image is known to match this checkout.
        """

        roots = (
            Path("docker") / "Dockerfile",
            Path("requirements.txt"),
            Path("pyproject.toml"),
            Path("owrt_builder"),
            Path("configs"),
            Path("scripts"),
        )
        files: list[Path] = []
        ignored = {".git", ".owrt", ".owrt-web", ".venv", "__pycache__"}
        for relative in roots:
            path = self.repo_root / relative
            if path.is_file():
                files.append(path)
                continue
            if not path.is_dir():
                continue
            for candidate in path.rglob("*"):
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                if any(part in ignored for part in candidate.relative_to(self.repo_root).parts):
                    continue
                if candidate.suffix in {".pyc", ".pyo"}:
                    continue
                files.append(candidate)
        digest = hashlib.sha256()
        for path in sorted(set(files), key=lambda item: item.relative_to(self.repo_root).as_posix()):
            relative = path.relative_to(self.repo_root).as_posix()
            digest.update(f"{_sha256(path)}  {relative}\n".encode("utf-8"))
        return digest.hexdigest()

    def _image_exists(self, image: str) -> bool:
        expected = self._context_fingerprint()
        completed = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                f"{{{{ index .Config.Labels \"{BUILDER_CONTEXT_LABEL}\" }}}}",
                image,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        return completed.returncode == 0 and completed.stdout.strip() == expected

    @staticmethod
    def _remove_container(name: str) -> None:
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    @staticmethod
    def _capture(command: Sequence[str], *, cwd: Path | None = None) -> str:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise CommandFailed(command, completed.returncode)
        return completed.stdout

    def _run_checked(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        callback: LogCallback,
        cancel_event: Any,
        env: Mapping[str, str] | None = None,
        retry_once: bool = False,
    ) -> None:
        attempts = 2 if retry_once else 1
        last_code = 1
        for attempt in range(attempts):
            last_code = self._run_stream(
                command,
                cwd=cwd,
                callback=callback,
                cancel_event=cancel_event,
                env=env,
            )
            if last_code == 0:
                return
            if attempt + 1 < attempts:
                self._emit(callback, "命令失败，进行一次重试")
        raise CommandFailed(command, last_code)

    def _run_stream(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        callback: LogCallback,
        cancel_event: Any,
        env: Mapping[str, str] | None = None,
        log_path: Path | None = None,
        container_name: str | None = None,
    ) -> int:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        self._emit(callback, "$ " + " ".join(_quote_arg(str(item)) for item in command))
        process = subprocess.Popen(
            [str(item) for item in command],
            cwd=cwd,
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            start_new_session=(os.name == "posix"),
        )
        selector = selectors.DefaultSelector()
        assert process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        log_handle = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("ab")
        buffer = b""
        started_monotonic = time.monotonic()
        last_output_monotonic = started_monotonic
        try:
            heartbeat_seconds = float(os.environ.get("OWRT_LOG_HEARTBEAT_SECONDS", "30"))
        except ValueError:
            heartbeat_seconds = 30.0
        heartbeat_seconds = max(5.0, heartbeat_seconds)
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self._terminate_process(process, container_name)
                    raise BuildCancelled()
                events = selector.select(timeout=0.25)
                for key, _ in events:
                    # ``subprocess.PIPE`` is a BufferedReader.  Its regular
                    # ``read(n)`` may wait for the requested amount even
                    # after ``select`` reports a small amount available,
                    # which stalls low-volume worker logs until the process
                    # exits.  ``read1`` performs one underlying read and
                    # returns the bytes available now.
                    try:
                        chunk = key.fileobj.read1(64 * 1024)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if log_handle is not None:
                        log_handle.write(chunk)
                        log_handle.flush()
                    last_output_monotonic = time.monotonic()
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        self._emit(callback, raw.decode("utf-8", errors="replace"))
                now = time.monotonic()
                if (
                    process.poll() is None
                    and now - last_output_monotonic >= heartbeat_seconds
                ):
                    elapsed = int(now - started_monotonic)
                    self._emit(
                        callback,
                        f"[heartbeat] 命令仍在运行，已耗时 {elapsed // 60} 分钟 {elapsed % 60} 秒",
                    )
                    last_output_monotonic = now
                if process.poll() is not None and not selector.get_map():
                    break
            if buffer:
                self._emit(callback, buffer.decode("utf-8", errors="replace"))
            return process.wait()
        finally:
            selector.close()
            if log_handle is not None:
                log_handle.close()

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes], container_name: str | None) -> None:
        if process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                process.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
        if container_name:
            BuildEngine._remove_container(container_name)


def build(
    request: BuildRequest,
    *,
    repo_root: str | Path | None = None,
    workspace: str | Path | None = None,
    on_log: LogCallback | None = None,
    cancel_event: Any = None,
    use_docker: bool | None = None,
) -> BuildResult:
    """Convenience function for web actions and small scripts."""

    engine = BuildEngine(repo_root, workspace, use_docker=use_docker)
    return engine.build(request, on_log=on_log, cancel_event=cancel_event)


__all__ = [
    "BuildCancelled",
    "BuildEngine",
    "BuildError",
    "BuildRequest",
    "BuildResult",
    "CommandFailed",
    "logical_cpu_count",
    "PreparedSource",
    "WorkspaceBusy",
    "WorkspaceLock",
    "build",
]
