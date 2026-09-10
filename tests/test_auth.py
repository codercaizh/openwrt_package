from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from owrt_builder.auth import (
    AuthManager,
    hash_password,
    validate_password,
    validate_username,
    verify_password,
)
from owrt_builder.storage import Storage
from owrt_builder.web import LoginBody, SettingsBody


def _login_request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/auth/login",
            "raw_path": b"/api/auth/login",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"origin", b"http://testserver"), (b"host", b"testserver")],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }
    )


def test_credential_validation_requires_presence_but_no_complexity_or_whitespace_rule() -> None:
    for username in ("x", " ", " user name "):
        assert validate_username(username) == username
    for password in ("x", " ", "\n", "password"):
        assert validate_password(password) == password
        encoded = hash_password(password)
        assert verify_password(password, encoded)

    with pytest.raises(ValueError):
        validate_username("")
    with pytest.raises(ValueError):
        validate_password("")
    assert not verify_password("", encoded)


def test_credential_models_allow_non_empty_whitespace_and_reject_empty() -> None:
    assert LoginBody(username=" ", password=" ").model_dump() == {"username": " ", "password": " "}
    settings = SettingsBody(username=" user name ", current_password=" ", new_password="\n")
    assert settings.username == " user name "
    assert settings.current_password == " "
    assert settings.new_password == "\n"

    with pytest.raises(ValueError):
        LoginBody(username="", password="x")
    with pytest.raises(ValueError):
        LoginBody(username="x", password="")
    with pytest.raises(ValueError):
        SettingsBody(username="")
    with pytest.raises(ValueError):
        SettingsBody(current_password="")
    with pytest.raises(ValueError):
        SettingsBody(new_password="")
    long_username = "u" * 121
    long_password = "p" * 513
    assert LoginBody(username=long_username, password=long_password).username == long_username
    long_settings = SettingsBody(
        username=long_username,
        current_password=long_password,
        new_password=long_password,
    )
    assert long_settings.username == long_username
    assert long_settings.current_password == long_password
    assert long_settings.new_password == long_password
    assert verify_password(long_password, hash_password(long_password))


def test_auth_manager_preserves_whitespace_credentials_and_rejects_empty_direct_inputs(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    username = " user name "
    password = " "
    storage.create_admin(username, hash_password(password))
    long_username = "u" * 121
    long_password = "p" * 513
    storage.create_admin(long_username, hash_password(long_password))
    auth = AuthManager(storage)

    result = auth.login(_login_request(), Response(), username, password)
    assert result["username"] == username
    long_result = auth.login(_login_request(), Response(), long_username, long_password)
    assert long_result["username"] == long_username

    with pytest.raises(HTTPException) as error:
        auth.login(_login_request(), Response(), "", password)
    assert error.value.status_code == 401
