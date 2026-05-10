"""
builder/source.py
源码克隆与更新。
"""
from pathlib import Path
import shutil
import subprocess
import sys
from .config import (
    OPENWRT_DIR, SOURCE_REPOS,
    SCRIPT_DIR, CONFIG_DIR, PACKIT_DIR,
    ARTIFACT_DIR, KERNEL_DIR,
)

TMP_DIR = Path("/opt/openwrt_tmp")


def _run(cmd, cwd=None, check=True):
    if isinstance(cmd, str):
        cmd = cmd.split()
    print(f"[source] {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, check=check)


def _strategy_from_config(target: str, config: str) -> str:
    """
    根据 target + config 决定实际使用的源码策略：
    - openwrt           → "openwrt"
    - immortalwrt + armv8* → "immortalwrt"
    - immortalwrt 其他    → "immortalwrt-mt798x"
    """
    if target == "openwrt":
        return "openwrt"
    if config and "armv8" in config:
        return "immortalwrt"
    return "immortalwrt-mt798x"


def clone_repo(url: str, branch: str | None, depth: int | None) -> None:
    """克隆到临时目录，再复制到 OPENWRT_DIR。"""
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)
    cmd = ["git", "clone"]
    if depth:
        cmd += ["--depth", str(depth)]
    if branch:
        cmd += ["-b", branch, "--single-branch"]
    cmd += [url, str(TMP_DIR)]
    _run(cmd)
    print(f"[source] openwrt源码检出完毕: {url}")

    if OPENWRT_DIR.exists():
        shutil.rmtree(OPENWRT_DIR, ignore_errors=True)
    OPENWRT_DIR.mkdir(parents=True, exist_ok=True)
    # 复制所有文件（含隐藏）到目标目录
    subprocess.run(f"cp -r {TMP_DIR}/. {OPENWRT_DIR}/", shell=True, check=True)
    shutil.rmtree(TMP_DIR, ignore_errors=True)


def setup_source(target: str, config: str) -> None:
    """
    根据 target + config 克隆或更新源码。
    去掉了 commit_id 逻辑，统一拉最新。
    """
    strategy = _strategy_from_config(target, config)
    info = SOURCE_REPOS[strategy]

    if not (OPENWRT_DIR / ".git").exists():
        print(f"[source] 未找到源码，使用策略: {strategy}")
        clone_repo(info["url"], info["branch"], info["depth"])
        return

    # 已存在：更新到最新
    print(f"[source] 源码已存在，更新到最新 (策略: {strategy})")
    cwd = str(OPENWRT_DIR)
    _run("git reset --hard", cwd=cwd)
    _run("git fetch --all", cwd=cwd)
    if info["branch"]:
        _run(f"git checkout {info['branch']}", cwd=cwd)
        _run("git pull", cwd=cwd)
    else:
        _run("git pull", cwd=cwd)
    # 清理遗留的 feeds 脚本
    for f in OPENWRT_DIR.glob("*.feeds.sh"):
        f.unlink(missing_ok=True)


def ensure_all_dirs() -> None:
    """确保运行所需的所有目录存在。"""
    for d in (OPENWRT_DIR, PACKIT_DIR, ARTIFACT_DIR, KERNEL_DIR, SCRIPT_DIR, CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)
