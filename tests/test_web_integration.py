from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx

from owrt_builder import web as web_module
from owrt_builder.auth import hash_password
from owrt_builder.catalog import Catalog, PackageMetadata
from owrt_builder.devices import load_catalog
from owrt_builder.sources import PreparedSource, SourceError
from owrt_builder.storage import Storage
from owrt_builder.web import QueueWorker, Runtime, Settings, SourceService, create_app, system_logical_cpus


def test_real_app_initialises_reviewed_devices(tmp_path: Path) -> None:
    """The default app uses the real core catalog and exposes all four devices."""

    app = create_app(settings=Settings(data_dir=tmp_path))
    storage = app.state.storage
    storage.create_admin("admin", hash_password("a-strong-test-password"))

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "a-strong-test-password"},
                headers={"Origin": "http://testserver"},
            )
            assert login.status_code == 200
            response = await client.get("/api/devices")
            assert response.status_code == 200
            items = response.json()["items"]
            assert [item["key"] for item in items] == ["360t7", "netcore_n60-pro", "s905d", "vplus"]
            assert all("s905" not in item["aliases"] for item in items)
            return login

    asyncio.run(request())


def test_source_refresh_keeps_catalogs_distinct_for_shared_source_devices(tmp_path: Path) -> None:
    """One armv8 snapshot serves both devices without a catalog PK collision."""

    source_root = tmp_path / "snapshot" / "source"
    source_root.mkdir(parents=True)
    catalog_path = tmp_path / "snapshot" / "catalog.json"
    Catalog(
        root=source_root,
        packages=[PackageMetadata(name="luci-app-demo", title="Demo")],
        authoritative=True,
    ).write(catalog_path)
    prepared = PreparedSource(
        source_id="armv8",
        snapshot_id="snapshot-armv8-v5",
        path=source_root,
        catalog_path=catalog_path,
        source_commit="source-sha",
    )

    devices = load_catalog()
    runtime = Runtime(
        devices=devices,
        sources=SimpleNamespace(
            prepare_source=lambda source_id, update=False, status=None: prepared,
        ),
        build=SimpleNamespace(),
    )
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")

    SourceService(runtime, storage)._run(False, "test")

    state = storage.get_state("source")
    assert state["ready"] is True
    first = storage.latest_catalog(prepared.snapshot_id, "s905d")
    second = storage.latest_catalog(prepared.snapshot_id, "vplus")
    assert first is not None and second is not None
    assert first["device"] == "s905d"
    assert second["device"] == "vplus"
    assert first["id"] != second["id"]


def test_source_refresh_does_not_expose_stale_schema_snapshot_after_restart(tmp_path: Path) -> None:
    """A persisted ready flag is cleared when v6 rejects its snapshot."""

    class SourceStub:
        def get_snapshot(self, _snapshot_id: str):
            raise SourceError("old preparation version")

        def prepare_source(self, *_args, **_kwargs):
            raise SourceError("feed unavailable")

    runtime = Runtime(
        devices=SimpleNamespace(
            sources={"armv8": object()},
            devices={"s905d": SimpleNamespace(key="s905d", source_id="armv8")},
        ),
        sources=SourceStub(),
        build=SimpleNamespace(),
    )
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    storage.set_state(
        "source",
        {
            "status": "ready",
            "ready": True,
            "snapshot_id": "old-snapshot",
            "snapshot": {"source_id": "armv8", "snapshot_id": "old-snapshot"},
            "snapshots": {"s905d": {"source_id": "armv8", "snapshot_id": "old-snapshot"}},
        },
    )

    SourceService(runtime, storage)._run(False, "startup")

    state = storage.get_state("source")
    assert state["status"] == "failed"
    assert state["ready"] is False
    assert "snapshot" not in state


def test_source_refresh_failure_preserves_a_readable_current_snapshot(tmp_path: Path) -> None:
    prepared = PreparedSource(
        source_id="armv8",
        snapshot_id="current-v5",
        path=tmp_path / "source",
        catalog_path=tmp_path / "catalog.json",
        source_commit="source-sha",
    )

    class SourceStub:
        def get_snapshot(self, snapshot_id: str) -> PreparedSource:
            assert snapshot_id == prepared.snapshot_id
            return prepared

        def prepare_source(self, *_args, **_kwargs):
            raise SourceError("feed unavailable")

    runtime = Runtime(
        devices=SimpleNamespace(
            sources={"armv8": object()},
            devices={"s905d": SimpleNamespace(key="s905d", source_id="armv8")},
        ),
        sources=SourceStub(),
        build=SimpleNamespace(),
    )
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    previous = {
        "status": "ready",
        "ready": True,
        "snapshot_id": prepared.snapshot_id,
        "snapshot": prepared.to_dict(),
        "snapshots": {"s905d": prepared.to_dict()},
    }
    storage.set_state("source", previous)

    SourceService(runtime, storage)._run(False, "manual")

    state = storage.get_state("source")
    assert state["status"] == "failed"
    assert state["ready"] is True
    assert state["snapshot_id"] == prepared.snapshot_id


def test_login_csrf_and_session_protection(tmp_path: Path) -> None:
    app = create_app(settings=Settings(data_dir=tmp_path))
    app.state.storage.create_admin("admin", hash_password("a-strong-test-password"))

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            unauthenticated = await client.get("/api/auth/me")
            assert unauthenticated.status_code == 401
            missing_origin = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "a-strong-test-password"},
            )
            assert missing_origin.status_code == 403
            login = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "a-strong-test-password"},
                headers={"Origin": "http://testserver"},
            )
            csrf = login.json()["csrf"]
            forbidden = await client.post("/api/auth/logout", json={}, headers={"Origin": "http://testserver"})
            assert forbidden.status_code == 403
            logout = await client.post(
                "/api/auth/logout",
                json={},
                headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
            )
            assert logout.status_code == 200

    asyncio.run(request())


def test_wildcard_origin_accepts_public_ip_origins_but_keeps_csrf(tmp_path: Path, monkeypatch) -> None:
    """Changing public IPs must not require rewriting Origin configuration."""

    monkeypatch.setenv("OWRT_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("OWRT_PUBLIC_ORIGIN", "http://fixed.example.test:8000")
    app = create_app(settings=Settings(data_dir=tmp_path))
    app.state.storage.create_admin("admin", hash_password("a-strong-test-password"))

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://internal") as client:
            unauthenticated = await client.get("/api/auth/me")
            assert unauthenticated.status_code == 401

            for origin in ("http://203.0.113.10:8000", "https://[2001:db8::10]:8443"):
                login = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "a-strong-test-password"},
                    headers={"Origin": origin},
                )
                assert login.status_code == 200

            csrf = login.json()["csrf"]
            missing_csrf = await client.post(
                "/api/auth/logout",
                json={},
                headers={"Origin": "https://[2001:db8::10]:8443"},
            )
            assert missing_csrf.status_code == 403
            logout = await client.post(
                "/api/auth/logout",
                json={},
                headers={"Origin": "https://[2001:db8::10]:8443", "X-CSRF-Token": csrf},
            )
            assert logout.status_code == 200

            malformed = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "a-strong-test-password"},
                headers={"Origin": "http://[2001:db8::10"},
            )
            assert malformed.status_code == 403
            bad_scheme = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "a-strong-test-password"},
                headers={"Origin": "javascript:alert(1)"},
            )
            assert bad_scheme.status_code == 403
            wrong_password = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong-password-123"},
                headers={"Origin": "http://198.51.100.20:8000"},
            )
            assert wrong_password.status_code == 401

    asyncio.run(request())


def test_client_cannot_supply_server_build_parameters(tmp_path: Path) -> None:
    app = create_app(settings=Settings(data_dir=tmp_path))
    app.state.storage.create_admin("admin", hash_password("a-strong-test-password"))

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "a-strong-test-password"},
                headers={"Origin": "http://testserver"},
            )
            csrf = login.json()["csrf"]
            response = await client.post(
                "/api/jobs",
                json={"device": "n60pro", "config": "/tmp/evil", "repo_root": "/tmp/evil"},
                headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
            )
            assert response.status_code == 422

    asyncio.run(request())


def test_artifact_download_requires_session_and_rejects_symlink_escape(tmp_path: Path) -> None:
    app = create_app(settings=Settings(data_dir=tmp_path))
    storage = app.state.storage
    storage.create_admin("admin", hash_password("a-strong-test-password"))
    output = tmp_path / "artifacts" / "netcore_n60-pro" / "job-download"
    output.mkdir(parents=True)
    artifact = output / "firmware.bin"
    artifact.write_bytes(b"firmware")
    storage.create_job(
        {
            "id": "job-download",
            "device": "netcore_n60-pro",
            "config": "n60pro",
            "packages": [],
            "options": {},
            "source_snapshot": {},
            "status": "succeeded",
            "output_dir": str(output),
        }
    )
    storage.add_artifact(
        {
            "id": "artifact-download",
            "job_id": "job-download",
            "name": "firmware.bin",
            "relative_path": "firmware.bin",
            "size": artifact.stat().st_size,
        }
    )

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            unauthenticated = await client.get("/api/jobs/job-download/artifacts/artifact-download/download")
            assert unauthenticated.status_code == 401
            login = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "a-strong-test-password"},
                headers={"Origin": "http://testserver"},
            )
            assert login.status_code == 200
            response = await client.get("/api/jobs/job-download/artifacts/artifact-download/download")
            assert response.status_code == 200
            assert response.content == b"firmware"

            outside = tmp_path / "outside.bin"
            outside.write_bytes(b"outside")
            artifact.unlink()
            os.symlink(outside, artifact)
            escaped = await client.get("/api/jobs/job-download/artifacts/artifact-download/download")
            assert escaped.status_code == 404

    asyncio.run(request())


def test_web_registers_manifest_alongside_firmware_artifact(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings=settings)
    output = tmp_path / "artifacts" / "netcore_n60-pro" / "job-manifest"
    output.mkdir(parents=True)
    firmware = tmp_path / "workspace-firmware.bin"
    firmware.write_bytes(b"firmware")
    manifest = tmp_path / "workspace-manifest.json"
    manifest.write_text('{"snapshot_id":"test"}\n', encoding="utf-8")
    job = {
        "id": "job-manifest",
        "device": "netcore_n60-pro",
        "config": "n60pro",
        "packages": [],
        "options": {},
        "source_snapshot": {},
        "status": "running",
        "created_at": "2026-01-01T00:00:00Z",
        "output_dir": str(output),
    }
    app.state.storage.create_job(job)
    worker = QueueWorker.__new__(QueueWorker)
    worker.storage = app.state.storage
    worker._register_artifacts(
        job,
        {"artifacts": [str(firmware)], "manifest_path": str(manifest)},
    )
    rows = app.state.storage.list_artifacts("job-manifest")
    assert {row["name"] for row in rows} == {"workspace-firmware.bin", "manifest.json"}


def _ready_job_app(tmp_path: Path):
    """Build a deterministic app with one ready catalog for submit tests."""

    source_root = tmp_path / "source-snapshot"
    source_root.mkdir(parents=True)
    catalog_path = tmp_path / "source-catalog.json"
    Catalog(root=source_root, packages=[], authoritative=True).write(catalog_path)
    prepared = PreparedSource(
        source_id="immortalwrt-mt798x",
        snapshot_id="web-controls-snapshot",
        path=source_root,
        catalog_path=catalog_path,
        source_commit="test-source",
    )

    class SourceStub:
        def get_snapshot(self, snapshot_id: str) -> PreparedSource:
            assert snapshot_id == prepared.snapshot_id
            return prepared

    runtime = Runtime(
        devices=load_catalog(),
        sources=SourceStub(),
        build=SimpleNamespace(),
    )
    app = create_app(runtime=runtime, settings=Settings(data_dir=tmp_path))
    storage = app.state.storage
    storage.create_admin("admin", hash_password("a-strong-test-password"))
    snapshot = {
        "source_id": "immortalwrt-mt798x",
        "snapshot_id": "web-controls-snapshot",
        "source_commit": "test-source",
    }
    storage.set_state(
        "source",
        {
            "status": "ready",
            "ready": True,
            "snapshot": snapshot,
            "snapshot_id": snapshot["snapshot_id"],
            "snapshots": {"netcore_n60-pro": snapshot},
        },
    )
    storage.save_catalog("web-controls-catalog", snapshot["snapshot_id"], "netcore_n60-pro", [], {})
    return app


def _snapshot_tree(root: Path) -> tuple[str, ...]:
    """Capture names, file contents and link targets for isolation tests."""

    entries: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append(f"L:{relative}->{os.readlink(path)}")
        elif path.is_dir():
            entries.append(f"D:{relative}")
        else:
            entries.append(f"F:{relative}:{path.read_bytes()!r}")
    return tuple(entries)


def test_web_static_validation_is_read_only_and_skips_native_make(tmp_path: Path) -> None:
    """Catalog checks never copy the snapshot or invoke native make."""

    source_root = tmp_path / "immutable-source"
    (source_root / "feeds" / "packages").mkdir(parents=True)
    (source_root / "Makefile").write_text("include rules.mk\n", encoding="utf-8")
    (source_root / "feeds" / "packages" / "Makefile").write_text("all:\n", encoding="utf-8")
    catalog_path = tmp_path / "catalog.json"
    Catalog(
        root=source_root,
        packages=[PackageMetadata(name="luci-app-demo", title="Demo")],
        authoritative=True,
    ).write(catalog_path)
    prepared = PreparedSource(
        source_id="immortalwrt-mt798x",
        snapshot_id="isolated-validation-snapshot",
        path=source_root,
        catalog_path=catalog_path,
        source_commit="source-sha",
    )
    before = _snapshot_tree(source_root)
    source_reads: list[str] = []
    compose_calls: list[str] = []

    class SourceStub:
        def get_snapshot(self, snapshot_id: str) -> PreparedSource:
            assert snapshot_id == prepared.snapshot_id
            source_reads.append(snapshot_id)
            return prepared

    class BuildStub:
        def compose_config(self, *_args, **_kwargs):
            compose_calls.append("called")
            raise AssertionError("static Web validation must not compose a build config")

    runtime = Runtime(
        devices=load_catalog(),
        sources=SourceStub(),
        build=BuildStub(),
    )
    settings = web_module.Settings(data_dir=tmp_path / "web")
    app = create_app(runtime=runtime, settings=settings)
    app.state.storage.create_admin("admin", hash_password("a-strong-test-password"))
    snapshot = prepared.to_dict()
    app.state.storage.set_state(
        "source",
        {
            "status": "ready",
            "ready": True,
            "snapshot": snapshot,
            "snapshot_id": prepared.snapshot_id,
            "snapshots": {"netcore_n60-pro": snapshot},
        },
    )
    app.state.storage.save_catalog("isolated-catalog", prepared.snapshot_id, "netcore_n60-pro", [
        {"name": "luci-app-demo", "symbol": "CONFIG_PACKAGE_luci-app-demo", "title": "Demo", "options": []},
    ], {})

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "a-strong-test-password"},
                headers={"Origin": "http://testserver"},
            )
            headers = {"Origin": "http://testserver", "X-CSRF-Token": login.json()["csrf"]}
            first = await client.post(
                "/api/configuration/validate",
                json={"device": "n60pro", "packages": ["luci-app-missing"], "options": {}},
                headers=headers,
            )
            assert first.status_code == 200, first.text
            first_payload = first.json()
            assert first_payload["authoritative"] is False
            assert first_payload["validation_mode"] == "catalog_static"
            assert first_payload["catalog_authoritative"] is True
            assert first_payload["native_defconfig"] is False
            assert first_payload["issues"][0]["kind"] == "unknown_package"
            second = await client.post(
                "/api/configuration/validate",
                json={"device": "n60pro", "packages": [], "options": {}},
                headers=headers,
            )
            assert second.status_code == 200, second.text
            assert second.json()["authoritative"] is False
            assert second.json()["validation_mode"] == "catalog_static"

    asyncio.run(request())
    assert source_reads == [prepared.snapshot_id, prepared.snapshot_id]
    assert compose_calls == []
    assert _snapshot_tree(source_root) == before


def test_job_submission_rejects_invalid_snapshot_before_enqueue(tmp_path: Path) -> None:
    """A contaminated snapshot returns a refreshable 503 and no queue row."""

    class InvalidSource:
        def get_snapshot(self, _snapshot_id: str) -> PreparedSource:
            raise SourceError("source snapshot contains preparation-runtime directories: staging_dir")

    runtime = Runtime(
        devices=load_catalog(),
        sources=InvalidSource(),
        build=SimpleNamespace(),
    )
    app = create_app(runtime=runtime, settings=web_module.Settings(data_dir=tmp_path))
    storage = app.state.storage
    storage.create_admin("admin", hash_password("a-strong-test-password"))
    snapshot = {
        "source_id": "immortalwrt-mt798x",
        "snapshot_id": "contaminated-snapshot",
        "source_commit": "source-sha",
    }
    storage.set_state(
        "source",
        {
            "status": "ready",
            "ready": True,
            "snapshot": snapshot,
            "snapshot_id": snapshot["snapshot_id"],
            "snapshots": {"netcore_n60-pro": snapshot},
        },
    )
    storage.save_catalog("contaminated-catalog", snapshot["snapshot_id"], "netcore_n60-pro", [], {})

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "a-strong-test-password"},
                headers={"Origin": "http://testserver"},
            )
            headers = {"Origin": "http://testserver", "X-CSRF-Token": login.json()["csrf"]}
            response = await client.post(
                "/api/jobs",
                json={"device": "n60pro", "packages": [], "options": {}},
                headers=headers,
            )
            assert response.status_code == 503, response.text
            detail = response.json()["detail"]
            assert detail["code"] == "source_snapshot_invalid"
            assert "staging_dir" in detail["message"]

    asyncio.run(request())
    assert storage.list_jobs(20, 0) == []


def test_web_build_controls_are_strict_bounded_and_persisted(tmp_path: Path) -> None:
    app = _ready_job_app(tmp_path)
    maximum = system_logical_cpus()

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "a-strong-test-password"},
                headers={"Origin": "http://testserver"},
            )
            assert login.status_code == 200
            csrf = login.json()["csrf"]
            headers = {"Origin": "http://testserver", "X-CSRF-Token": csrf}

            devices = await client.get("/api/devices")
            assert devices.status_code == 200
            runtime = devices.json()["runtime"]
            assert runtime["logical_cpus"] == maximum
            assert runtime["default_parallel_jobs"] == maximum
            assert runtime["max_parallel_jobs"] == maximum

            base = {"device": "n60pro", "packages": [], "options": {}}
            default_job = await client.post("/api/jobs", json=base, headers=headers)
            assert default_job.status_code == 202, default_job.text
            default_payload = default_job.json()["job"]
            assert default_payload["parallel_jobs"] == maximum
            assert default_payload["reuse_cache"] is True

            explicit = await client.post(
                "/api/jobs",
                json={**base, "parallel_jobs": 1, "reuse_cache": False},
                headers=headers,
            )
            assert explicit.status_code == 202, explicit.text
            explicit_payload = explicit.json()["job"]
            assert explicit_payload["parallel_jobs"] == 1
            assert explicit_payload["reuse_cache"] is False

            for invalid in (True, 1.5, "1", 0, maximum + 1):
                response = await client.post(
                    "/api/jobs",
                    json={**base, "parallel_jobs": invalid},
                    headers=headers,
                )
                assert response.status_code in {400, 422}, (invalid, response.text)

            history = app.state.storage.get_job(explicit_payload["id"])
            assert history is not None
            assert history["parallel_jobs"] == 1
            assert history["reuse_cache"] is False
            detail = await client.get(f"/api/jobs/{explicit_payload['id']}")
            assert detail.status_code == 200
            assert detail.json()["job"]["parallel_jobs"] == 1
            assert detail.json()["job"]["reuse_cache"] is False

    asyncio.run(request())


def test_queue_worker_passes_controls_to_request_and_result_history(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class Request:
        def __init__(self, *, task_id, device, snapshot_id, package_selections, options, parallel_jobs, reuse_cache):
            captured.update(
                {
                    "task_id": task_id,
                    "device": device,
                    "snapshot_id": snapshot_id,
                    "package_selections": package_selections,
                    "options": options,
                    "parallel_jobs": parallel_jobs,
                    "reuse_cache": reuse_cache,
                }
            )

    class Build:
        def build(self, request, **_kwargs):
            captured["request"] = request
            return {"status": "success", "success": True}

    class Notifier:
        def send_async(self, *_args, **_kwargs):
            return None

    runtime = SimpleNamespace(request_type=Request, build=Build())
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    job = {
        "id": "controls-worker",
        "device": "netcore_n60-pro",
        "config": "n60pro",
        "packages": [],
        "options": {},
        "parallel_jobs": 3,
        "reuse_cache": False,
        "source_snapshot": {"snapshot_id": "snapshot"},
        "status": "running",
        "output_dir": str(tmp_path / "artifacts" / "controls-worker"),
    }
    storage.create_job(job)
    worker = QueueWorker(runtime, storage, Settings(data_dir=tmp_path), Notifier())
    worker._run_job(job)

    assert captured["parallel_jobs"] == 3
    assert captured["reuse_cache"] is False
    result = storage.get_job("controls-worker")["result"]
    assert result["parallel_jobs"] == 3
    assert result["reuse_cache"] is False
    assert storage.get_job("controls-worker")["status"] == "succeeded"


def test_old_jobs_database_migrates_control_defaults(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """CREATE TABLE jobs (
                id TEXT PRIMARY KEY, device TEXT NOT NULL, config TEXT NOT NULL,
                packages_json TEXT NOT NULL, options_json TEXT NOT NULL,
                source_snapshot_json TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                claimed_at TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                error TEXT, result_json TEXT, output_dir TEXT NOT NULL,
                owner_user_id INTEGER
            )"""
        )
        db.execute(
            """INSERT INTO jobs(
                id,device,config,packages_json,options_json,source_snapshot_json,
                status,created_at,output_dir
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("legacy-job", "netcore_n60-pro", "n60pro", "[]", "{}", "{}", "succeeded", "2026-01-01T00:00:00Z", str(tmp_path / "out")),
        )
        db.commit()

    storage = Storage(db_path, tmp_path / "logs", tmp_path / "artifacts")
    job = storage.get_job("legacy-job")
    assert job["parallel_jobs"] == 1
    assert job["reuse_cache"] is True


def test_system_logical_cpus_prefers_process_affinity(monkeypatch) -> None:
    monkeypatch.setattr(web_module.os, "sched_getaffinity", lambda _pid: {2, 5, 9})
    monkeypatch.setattr(web_module.os, "cpu_count", lambda: 128)
    assert system_logical_cpus() == 3

    def unavailable(_pid):
        raise OSError("affinity unavailable")

    monkeypatch.setattr(web_module.os, "sched_getaffinity", unavailable)
    monkeypatch.setattr(web_module.os, "cpu_count", lambda: 7)
    assert system_logical_cpus() == 7
