from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from owrt_builder.auth import hash_password
from owrt_builder.web import Settings, create_app


def test_jobs_api_returns_sql_page_metadata_and_recent_order(tmp_path: Path) -> None:
    app = create_app(settings=Settings(data_dir=tmp_path))
    storage = app.state.storage
    storage.create_admin("admin", hash_password("pagination-password"))
    for index in range(12):
        storage.create_job(
            {
                "id": f"job-{index:02d}",
                "device": "netcore_n60-pro",
                "config": "n60pro",
                "packages": [],
                "options": {},
                "source_snapshot": {},
                "status": "succeeded",
                "created_at": f"2026-09-10T00:{index:02d}:00+00:00",
                "output_dir": str(tmp_path / "artifacts" / f"job-{index:02d}"),
            }
        )

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "pagination-password"},
                headers={"Origin": "http://testserver"},
            )
            assert login.status_code == 200
            result = await client.get("/api/jobs?page=2&per_page=5")
            assert result.status_code == 200
            payload = result.json()
            assert payload["total"] == 12
            assert payload["page"] == 2
            assert payload["per_page"] == 5
            assert payload["pages"] == 3
            assert payload["status_counts"] == {
                "queued": 0,
                "running": 0,
                "succeeded": 12,
                "failed": 0,
                "canceled": 0,
                "interrupted": 0,
            }
            assert [item["id"] for item in payload["items"]] == ["job-06", "job-05", "job-04", "job-03", "job-02"]

            boundary = await client.get("/api/jobs?page=99&per_page=5")
            assert boundary.status_code == 200
            assert boundary.json()["page"] == 3
            assert len(boundary.json()["items"]) == 2

    asyncio.run(request())
