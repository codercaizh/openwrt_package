"""Local-only administrator bootstrap CLI.

Examples::

    python -m owrt_builder.admin create-admin admin
    OWRT_ADMIN_PASSWORD='...long secret...' python -m owrt_builder.admin create-admin admin

The environment form is useful for provisioning but the password is never
printed. Public registration is intentionally not implemented.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from .auth import hash_password
from .storage import Storage


def _storage_from_env() -> Storage:
    data_dir = Path(os.getenv("OWRT_DATA_DIR", "data"))
    return Storage(
        os.getenv("OWRT_DB_PATH", str(data_dir / "state.sqlite3")),
        os.getenv("OWRT_LOG_DIR", str(data_dir / "logs")),
        os.getenv("OWRT_ARTIFACT_DIR", str(data_dir / "artifacts")),
    )


def create_admin(username: str, password: str | None = None) -> int:
    username = username.strip()
    if not username or len(username) > 120 or any(ch.isspace() for ch in username):
        raise ValueError("用户名不能为空、不能含空白且长度不能超过 120")
    if password is None:
        password = os.getenv("OWRT_ADMIN_PASSWORD")
    if password is None:
        password = getpass.getpass("管理员密码（不会回显）：")
        confirm = getpass.getpass("再次输入管理员密码：")
        if password != confirm:
            raise ValueError("两次密码不一致")
    storage = _storage_from_env()
    if storage.get_admin_by_username(username):
        raise ValueError("管理员用户名已存在")
    user_id = storage.create_admin(username, hash_password(password))
    print(f"已创建本地管理员：{username}（id={user_id}）")
    return user_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenWrt builder 管理员本地初始化工具")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-admin", help="创建首个管理员；不提供密码参数，避免出现在 shell 历史")
    create.add_argument("username")
    args = parser.parse_args(argv)
    try:
        if args.command == "create-admin":
            create_admin(args.username)
            return 0
    except (ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised by provisioning
    raise SystemExit(main())
