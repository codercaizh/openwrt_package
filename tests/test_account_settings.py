from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from owrt_builder.admin import create_admin
from owrt_builder.auth import hash_password, verify_password
from owrt_builder.storage import Storage
from owrt_builder.web import Settings, create_app


def test_default_admin_bootstrap_is_atomic_and_does_not_overwrite_existing_users(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    bootstrap_hash = hash_password("admin")

    assert storage.ensure_default_admin("admin", bootstrap_hash) is True
    assert storage.ensure_default_admin("admin", hash_password("different-strong-password")) is False
    user = storage.get_admin_by_username("admin")
    assert user is not None
    assert verify_password("admin", user["password_hash"])
    assert storage.admin_count() == 1

    existing = Storage(tmp_path / "existing.sqlite3", tmp_path / "existing-logs", tmp_path / "existing-artifacts")
    existing.create_admin("operator", hash_password("operator-strong-password"))
    assert existing.ensure_default_admin("admin", bootstrap_hash) is False
    assert existing.get_admin_by_username("admin") is None
    assert existing.get_admin_by_username("operator") is not None


def test_cli_admin_initialization_preserves_non_empty_whitespace_credentials(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "cli-state.sqlite3"
    monkeypatch.setenv("OWRT_DB_PATH", str(db_path))
    monkeypatch.setenv("OWRT_LOG_DIR", str(tmp_path / "cli-logs"))
    monkeypatch.setenv("OWRT_ARTIFACT_DIR", str(tmp_path / "cli-artifacts"))

    user_id = create_admin(" operator ", " ")
    storage = Storage(db_path, tmp_path / "cli-logs", tmp_path / "cli-artifacts")
    user = storage.get_admin(user_id)
    assert user is not None
    assert user["username"] == " operator "
    assert verify_password(" ", user["password_hash"])


def test_settings_mask_pushplus_and_invalidate_sessions_after_account_change(tmp_path: Path) -> None:
    app = create_app(settings=Settings(data_dir=tmp_path))
    storage = app.state.storage
    storage.create_admin("admin", hash_password("current-strong-password"))

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "current-strong-password"},
                headers={"Origin": "http://testserver"},
            )
            assert login.status_code == 200, login.text
            csrf = login.json()["csrf"]
            headers = {"Origin": "http://testserver", "X-CSRF-Token": csrf}

            empty = await client.get("/api/settings")
            assert empty.status_code == 200
            assert empty.json()["settings"]["pushplus"] == {"configured": False, "masked": ""}

            saved_token = await client.put(
                "/api/settings",
                json={"pushplus_token": "push-secret-123"},
                headers=headers,
            )
            assert saved_token.status_code == 200, saved_token.text
            assert saved_token.json()["settings"]["pushplus"] == {"configured": True, "masked": "••••••••"}
            assert "push-secret-123" not in saved_token.text
            assert storage.get_pushplus_token() == "push-secret-123"

            wrong = await client.put(
                "/api/settings",
                json={"username": "operator", "current_password": "wrong-password", "new_password": "new-strong-password"},
                headers=headers,
            )
            assert wrong.status_code == 403

            changed = await client.put(
                "/api/settings",
                json={
                    "username": "operator",
                    "current_password": "current-strong-password",
                    "new_password": "new-strong-password",
                },
                headers=headers,
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["requires_login"] is True
            assert "current-strong-password" not in changed.text

            old_session = await client.get("/api/auth/me")
            assert old_session.status_code == 401
            new_login = await client.post(
                "/api/auth/login",
                json={"username": "operator", "password": "new-strong-password"},
                headers={"Origin": "http://testserver"},
            )
            assert new_login.status_code == 200, new_login.text

            cleared = await client.put(
                "/api/settings",
                json={"clear_pushplus": True},
                headers={"Origin": "http://testserver", "X-CSRF-Token": new_login.json()["csrf"]},
            )
            assert cleared.status_code == 200
            assert cleared.json()["settings"]["pushplus"] == {"configured": False, "masked": ""}
            assert storage.get_pushplus_token() is None

    asyncio.run(request())


def test_settings_account_change_accepts_short_and_whitespace_credentials(tmp_path: Path) -> None:
    app = create_app(settings=Settings(data_dir=tmp_path))
    storage = app.state.storage
    storage.create_admin("admin", hash_password("p"))

    async def request() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "p"},
                headers={"Origin": "http://testserver"},
            )
            assert login.status_code == 200, login.text
            changed = await client.put(
                "/api/settings",
                json={
                    "username": " operator ",
                    "current_password": "p",
                    "new_password": " ",
                },
                headers={"Origin": "http://testserver", "X-CSRF-Token": login.json()["csrf"]},
            )
            assert changed.status_code == 200, changed.text

            relogin = await client.post(
                "/api/auth/login",
                json={"username": " operator ", "password": " "},
                headers={"Origin": "http://testserver"},
            )
            assert relogin.status_code == 200, relogin.text
            assert relogin.json()["user"]["username"] == " operator "

    asyncio.run(request())
