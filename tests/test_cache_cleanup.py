from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from owrt_builder.auth import hash_password
from owrt_builder.cleanup import clear_directory, workspace_cleanup_lock
from owrt_builder.web import Settings, create_app


def _login_headers(app) -> dict[str, str]:
    async def request() -> dict[str, str]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "cleanup-password"},
                headers={"Origin": "http://testserver"},
            )
            assert response.status_code == 200, response.text
            session = response.cookies.get("owrt_session")
            csrf_cookie = response.cookies.get("owrt_csrf")
            return {
                "Origin": "http://testserver",
                "X-CSRF-Token": response.json()["csrf"],
                "Cookie": f"owrt_session={session}; owrt_csrf={csrf_cookie}",
            }

    return asyncio.run(request())


def test_clear_directory_unlinks_abnormal_or_symlink_roots_without_following(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("must survive", encoding="utf-8")
    target = tmp_path / "cache"
    target.symlink_to(outside, target_is_directory=True)

    released = clear_directory(target)

    assert released > 0
    assert target.is_dir() and not target.is_symlink()
    assert keep.read_text(encoding="utf-8") == "must survive"

    target.rmdir()
    target.write_text("abnormal root", encoding="utf-8")
    clear_directory(target)
    assert target.is_dir() and not target.is_symlink()


def test_cache_cleanup_api_requires_csrf_preserves_evidence_and_reports_breakdown(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings=settings)
    storage = app.state.storage
    admin_id = storage.create_admin("admin", hash_password("cleanup-password"))
    workspace = tmp_path / "workspace"
    (workspace / "cache" / "builds" / "device").mkdir(parents=True)
    (workspace / "cache" / "builds" / "device" / "object").write_bytes(b"compiler")
    (workspace / "cache" / "dl").mkdir(parents=True)
    (workspace / "cache" / "dl" / "source.tar.gz").write_bytes(b"download")
    (workspace / "sources" / "source" / "snapshots" / "snapshot").mkdir(parents=True)
    (workspace / "sources" / "source" / "snapshots" / "snapshot" / "source").write_bytes(b"snapshot")
    task = tmp_path / "tasks" / "keep"
    task.mkdir(parents=True)
    (task / "result.json").write_text("{}", encoding="utf-8")
    artifact = tmp_path / "artifacts" / "keep"
    artifact.mkdir(parents=True)
    (artifact / "firmware.bin").write_bytes(b"firmware")
    workspace_artifact = workspace / "artifacts" / "keep"
    workspace_artifact.mkdir(parents=True)
    (workspace_artifact / "firmware.bin").write_bytes(b"worker-firmware")
    storage.create_job(
        {
            "id": "kept-job",
            "device": "device",
            "config": "config",
            "packages": [],
            "options": {},
            "source_snapshot": {},
            "status": "succeeded",
            "output_dir": str(artifact),
        }
    )
    storage.append_log("kept-job", "retain this log")
    storage.add_artifact(
        {
            "id": "artifact-row",
            "job_id": "kept-job",
            "name": "firmware.bin",
            "relative_path": "firmware.bin",
            "size": (artifact / "firmware.bin").stat().st_size,
            "sha256": "fixture",
        }
    )
    storage.save_catalog(
        "catalog-row",
        "snapshot",
        "device",
        [{"name": "luci-app-demo", "title": "Demo"}],
        {"fixture": True},
    )
    storage.save_defaults("device", ["luci-app-demo"], {}, admin_id)
    storage.set_state("source", {"status": "ready", "ready": True, "snapshot_id": "snapshot"})

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "cleanup-password"},
                headers={"Origin": "http://testserver"},
            )
            csrf = login.json()["csrf"]
            missing = await client.post(
                "/api/cache/clear",
                json={},
                headers={"Origin": "http://testserver"},
            )
            assert missing.status_code == 403
            response = await client.post(
                "/api/cache/clear",
                json={},
                headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["total_bytes"] > 0
            assert payload["released_bytes"] == payload["total_bytes"]
            assert payload["categories"]["compile_cache"] > 0
            assert payload["categories"]["download_cache"] > 0
            assert payload["categories"]["source_cache"] > 0
            assert payload["source"]["ready"] is False
            assert payload["source"]["status"] == "not_ready"

    asyncio.run(request())
    assert (workspace / "cache").is_dir() and not any((workspace / "cache").iterdir())
    assert (workspace / "sources").is_dir() and not any((workspace / "sources").iterdir())
    assert (task / "result.json").is_file()
    # Firmware files are generated artifacts and are intentionally removed;
    # the task row/log evidence remains outside this cleanup target.
    assert settings.artifact_dir.is_dir()
    assert not artifact.exists()
    assert (workspace / "artifacts").is_dir()
    assert not workspace_artifact.exists()
    assert storage.get_artifact("artifact-row") is None
    assert storage.latest_catalog("snapshot", "device") is None
    assert storage.get_job("kept-job") is not None
    assert [row["line"] for row in storage.get_logs("kept-job")] == ["retain this log"]
    assert storage.get_defaults("device")["packages"] == ["luci-app-demo"]
    assert storage.admin_count() == 1
    state = storage.get_state("source")
    assert state["ready"] is False
    assert state["status"] == "not_ready"


def test_cache_cleanup_rejects_active_build_and_concurrent_cleanup(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings=settings)
    storage = app.state.storage
    storage.create_admin("admin", hash_password("cleanup-password"))
    storage.create_job(
        {
            "id": "queued-cleanup",
            "device": "device",
            "config": "config",
            "packages": [],
            "options": {},
            "source_snapshot": {},
            "status": "queued",
            "output_dir": str(tmp_path / "artifacts" / "queued-cleanup"),
        }
    )
    headers = _login_headers(app)

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            active = await client.post("/api/cache/clear", json={}, headers=headers)
            assert active.status_code == 409
            assert active.json()["detail"]["code"] == "builds_active"
            storage.request_cancel("queued-cleanup")
            workspace = tmp_path / "workspace"
            with workspace_cleanup_lock(workspace, timeout=0.0):
                busy = await client.post("/api/cache/clear", json={}, headers=headers)
            assert busy.status_code == 409
            assert busy.json()["detail"]["code"] == "cleanup_busy"

    asyncio.run(request())


def test_cache_cleanup_rejects_persisted_source_preparation(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings=settings)
    storage = app.state.storage
    storage.create_admin("admin", hash_password("cleanup-password"))
    storage.set_state("source", {"status": "preparing", "ready": False})
    headers = _login_headers(app)

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/cache/clear", json={}, headers=headers)
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "source_preparing"

    asyncio.run(request())


def test_cache_cleanup_static_contract() -> None:
    root = Path(__file__).resolve().parents[1] / "owrt_builder" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    script = (root / "app.js").read_text(encoding="utf-8")
    assert 'id="clear-cache"' in html
    assert 'id="cache-cleanup-result"' in html
    assert 'window.confirm("确定清理所有缓存吗？' in script
    assert "clearingCache: false" in script
    assert 'api("/api/cache/clear", { method: "POST"' in script
    assert "button.disabled = true" in script
    assert "总释放" in script


def test_history_cleanup_removes_terminal_evidence_and_preserves_project_state(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings=settings)
    storage = app.state.storage
    storage.create_admin("admin", hash_password("cleanup-password"))
    workspace = tmp_path / "workspace"

    preserved = {
        workspace / "cache" / "dl" / "source.tar.gz": b"download cache",
        workspace / "sources" / "main" / "README": b"source cache",
    }
    for path, content in preserved.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    for index, status in enumerate(("succeeded", "failed")):
        job_id = f"history-{index}"
        output = settings.artifact_dir / job_id
        output.mkdir(parents=True)
        (output / "firmware.bin").write_bytes(b"firmware")
        task = workspace / "tasks" / job_id
        task.mkdir(parents=True)
        (task / "result.json").write_text("{}", encoding="utf-8")
        storage.create_job(
            {
                "id": job_id,
                "device": "device",
                "config": "config",
                "packages": [],
                "options": {},
                "source_snapshot": {},
                "status": status,
                "output_dir": str(output),
            }
        )
        storage.append_log(job_id, "history log")
        storage.add_artifact(
            {
                "id": f"artifact-{index}",
                "job_id": job_id,
                "name": "firmware.bin",
                "relative_path": "firmware.bin",
                "size": 8,
                "sha256": "fixture",
            }
        )

    headers = _login_headers(app)

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            missing = await client.post(
                "/api/jobs/history/clear",
                json={},
                headers={"Origin": "http://testserver", "Cookie": headers["Cookie"]},
            )
            assert missing.status_code == 403
            response = await client.post("/api/jobs/history/clear", json={}, headers=headers)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["jobs"] == 2
            assert payload["logs"] == 2
            assert payload["task_data"] == 2
            assert payload["artifact_directories"] == 2
            assert payload["artifact_records"] == 2
            assert payload["released_bytes"] > 0

    asyncio.run(request())
    for index in range(2):
        job_id = f"history-{index}"
        assert storage.get_job(job_id) is None
        assert not storage.log_path(job_id).exists()
        assert not (workspace / "tasks" / job_id).exists()
        assert not (settings.artifact_dir / job_id).exists()
        assert storage.get_artifact(f"artifact-{index}") is None
    for path, content in preserved.items():
        assert path.read_bytes() == content
    assert storage.admin_count() == 1


def test_history_cleanup_rejects_active_build_without_partial_deletion(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings=settings)
    storage = app.state.storage
    storage.create_admin("admin", hash_password("cleanup-password"))
    for job_id, status in (("finished-history", "succeeded"), ("active-history", "queued")):
        output = settings.artifact_dir / job_id
        output.mkdir(parents=True)
        (output / "firmware.bin").write_bytes(b"keep")
        storage.create_job(
            {
                "id": job_id,
                "device": "device",
                "config": "config",
                "packages": [],
                "options": {},
                "source_snapshot": {},
                "status": status,
                "output_dir": str(output),
            }
        )

    headers = _login_headers(app)

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/api/jobs/history/clear", json={}, headers=headers)
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "builds_active"

    asyncio.run(request())
    assert storage.get_job("finished-history") is not None
    assert storage.get_job("active-history") is not None
    assert (settings.artifact_dir / "finished-history" / "firmware.bin").read_bytes() == b"keep"


def test_routed_mobile_console_static_contract() -> None:
    root = Path(__file__).resolve().parents[1] / "owrt_builder" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    script = (root / "app.js").read_text(encoding="utf-8")
    style = (root / "style.css").read_text(encoding="utf-8")

    for route in ("overview", "jobs", "processes", "settings"):
        assert f'data-route="{route}"' in html
        assert f'data-view="{route}"' in html
    overview = html[html.index('data-view="overview"'):html.index('data-view="jobs"')]
    settings_view = html[html.index('data-view="settings"'):html.index("</main>")]
    assert 'id="refresh-sources"' not in overview
    assert 'id="refresh-sources"' in settings_view
    assert 'id="clear-history"' in settings_view
    assert "status_counts" in script
    assert 'window.addEventListener("hashchange"' in script
    assert 'node.hidden = node.dataset.view !== selected;' in script
    assert ".spa-view[hidden] { display:none !important; }" in style
    assert "overflow-x:hidden; overflow-x:clip" in style
    assert "@media (max-width:430px)" in style
    assert ".top-nav { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); }" in style
