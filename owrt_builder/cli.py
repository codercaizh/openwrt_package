"""Dependency-free command line entry point for ``./owrt`` and the worker."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from typing import Any

from .build import BuildEngine, BuildError, BuildRequest, BuildResult, WorkspaceBusy, logical_cpu_count
from .devices import CatalogError, load_catalog


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="owrt",
        description="Build the reviewed OpenWrt device set using Docker.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="compile and package one supported device")
    build.add_argument("device", help="360t7, n60pro, netcore_n60-pro, s905d or vplus")
    build.add_argument("--workspace", type=Path, help=argparse.SUPPRESS)
    build.add_argument(
        "--jobs",
        type=int,
        help=f"parallel make jobs (default: {logical_cpu_count()}, max: {logical_cpu_count()})",
    )
    cache = build.add_mutually_exclusive_group()
    cache.add_argument(
        "--reuse-cache",
        dest="reuse_cache",
        action="store_true",
        help="reuse the device compiler cache (default)",
    )
    cache.add_argument(
        "--no-reuse-cache",
        dest="reuse_cache",
        action="store_false",
        help="discard and recreate this device compiler cache",
    )
    build.set_defaults(reuse_cache=True)
    build.add_argument("--package", dest="packages", action="append", default=[], help=argparse.SUPPRESS)
    build.add_argument("--json", action="store_true", help="print the result as JSON")

    devices = sub.add_parser("devices", help="list supported devices")
    devices.add_argument("--json", action="store_true", help="print catalog JSON")

    doctor = sub.add_parser("doctor", help="check Docker, resources and the reviewed catalog")
    doctor.add_argument("--json", action="store_true", help="print the report as JSON")

    worker = sub.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("--request-file", required=True, type=Path)

    web = sub.add_parser("web", help="start the web service with Docker Compose")
    web.add_argument("compose_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return parser


def _install_cancel_handler(cancel_event: threading.Event) -> tuple[Any, Any]:
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)

    def handler(_signum: int, _frame: Any) -> None:
        cancel_event.set()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    return old_term, old_int


def _restore_cancel_handler(previous: tuple[Any, Any]) -> None:
    signal.signal(signal.SIGTERM, previous[0])
    signal.signal(signal.SIGINT, previous[1])


def _result_exit(result: BuildResult, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        if result.ok:
            print(f"编译成功: {result.device}")
            for path in result.artifacts:
                print(path)
            print(f"manifest: {result.manifest_path}")
        elif result.status == "cancelled":
            print("编译已取消", file=sys.stderr)
        else:
            print(f"编译失败: {result.error or 'unknown error'}", file=sys.stderr)
            if result.manifest_path:
                print(f"manifest: {result.manifest_path}", file=sys.stderr)
    return 0 if result.ok else (130 if result.status == "cancelled" else 1)


def _run_build(args: argparse.Namespace) -> int:
    root = _repo_root()
    workspace = args.workspace or os.environ.get("OWRT_WORKSPACE") or root / ".owrt"
    try:
        request = BuildRequest(
            device=args.device,
            packages=tuple(args.packages) if args.packages else None,
            jobs=args.jobs,
            reuse_cache=args.reuse_cache,
        )
    except (ValueError, TypeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    cancel_event = threading.Event()
    previous = _install_cancel_handler(cancel_event)
    try:
        engine = BuildEngine(root, workspace)
        result = engine.build(request, cancel_event=cancel_event)
    except (BuildError, CatalogError, WorkspaceBusy, ValueError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    finally:
        _restore_cancel_handler(previous)
    return _result_exit(result, args.json)


def _run_worker(args: argparse.Namespace) -> int:
    request_path = args.request_file.expanduser().resolve()
    try:
        request_data = json.loads(request_path.read_text(encoding="utf-8"))
        request = BuildRequest.from_dict(request_data)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"worker request invalid: {exc}", file=sys.stderr)
        return 2
    os.environ["OWRT_BUILDER_WORKER"] = "1"
    if request_data.get("worker_lock_held"):
        os.environ["OWRT_WORKER_LOCK_HELD"] = "1"
    root = _repo_root()
    # The worker request file is mounted under <workspace>/tasks/<id>/; this
    # keeps the workspace path out of the public BuildRequest DTO.
    workspace = request_path.parents[2].resolve()
    request = BuildRequest(
        device=request.device,
        task_id=request.task_id,
        snapshot_id=request.snapshot_id,
        packages=request.packages,
        package_selections=request.package_selections,
        options=request.options,
        jobs=request.jobs,
        reuse_cache=request.reuse_cache,
        lock_timeout=0,
    )
    cancel_event = threading.Event()
    previous = _install_cancel_handler(cancel_event)
    try:
        result = BuildEngine(root, workspace, use_docker=False).build(
            request,
            cancel_event=cancel_event,
        )
    finally:
        _restore_cancel_handler(previous)
    # _build_direct already writes this file.  Keep a marker on stdout for
    # users invoking the worker manually and for old adapters that only stream.
    print("OWRT_RESULT_JSON=" + json.dumps(result.to_dict(), ensure_ascii=False), flush=True)
    return _result_exit(result, as_json=False)


def _run_devices(args: argparse.Namespace) -> int:
    catalog = load_catalog(_repo_root() / "configs" / "devices.toml")
    if args.json:
        print(json.dumps(catalog.to_dict(), ensure_ascii=False, indent=2))
        return 0
    for spec in catalog.devices.values():
        aliases = ", ".join(spec.aliases) if spec.aliases else "-"
        print(f"{spec.key}\t{spec.description}\taliases: {aliases}")
    return 0


def _run_doctor(args: argparse.Namespace) -> int:
    root = _repo_root()
    catalog = load_catalog(root / "configs" / "devices.toml")
    report: dict[str, Any] = {
        "catalog": str(catalog.path),
        "devices": sorted(catalog.devices),
        "workspace": str(Path(os.environ.get("OWRT_WORKSPACE", root / ".owrt")).resolve()),
        "docker": {"available": False},
    }
    engine = BuildEngine(root, report["workspace"], catalog=catalog)
    expected_context = engine._context_fingerprint()
    report["builder_image"] = {
        "name": engine.image,
        "expected_context": expected_context,
        "actual_context": None,
        "current": False,
    }
    try:
        forwarded_available = os.environ.get("OWRT_DOCKER_SERVER_AVAILABLE")
        if forwarded_available is not None:
            available = forwarded_available == "1"
            server_version = os.environ.get("OWRT_DOCKER_SERVER_VERSION", "").strip()
            report["docker"] = {
                "available": available,
                "server_version": server_version,
                "error": None if available else "host Docker daemon is unavailable",
            }
            actual_context = os.environ.get("OWRT_DOCKER_IMAGE_CONTEXT", "").strip() or None
            report["builder_image"]["actual_context"] = actual_context
            report["builder_image"]["current"] = actual_context == expected_context
        else:
            completed = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            report["docker"] = {
                "available": completed.returncode == 0,
                "server_version": completed.stdout.strip(),
                "error": completed.stderr.strip() if completed.returncode else None,
            }
            if completed.returncode == 0:
                image = subprocess.run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        "--format",
                        "{{ index .Config.Labels \"org.openwrt.builder.context\" }}",
                        engine.image,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    check=False,
                )
                actual_context = image.stdout.strip() if image.returncode == 0 else None
                report["builder_image"]["actual_context"] = actual_context
                report["builder_image"]["current"] = actual_context == expected_context
    except OSError as exc:
        report["docker"] = {"available": False, "error": str(exc)}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"catalog: {report['catalog']}")
        print("devices: " + ", ".join(report["devices"]))
        docker = report["docker"]
        print("docker: " + (docker.get("server_version") or docker.get("error", "unavailable")))
    return 0 if report["docker"]["available"] else 1


def _run_web(args: argparse.Namespace) -> int:
    root = _repo_root()
    command = ["docker", "compose", "up", "--build"]
    command.extend(args.compose_args)
    # Compose owns the web and worker lifecycle; no Python dependency is
    # required on the host for this entry point.
    return subprocess.call(command, cwd=root)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            return _run_build(args)
        if args.command == "worker":
            return _run_worker(args)
        if args.command == "devices":
            return _run_devices(args)
        if args.command == "doctor":
            return _run_doctor(args)
        if args.command == "web":
            return _run_web(args)
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
