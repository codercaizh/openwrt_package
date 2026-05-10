"""
builder/kernel.py
内核查询、下载、解压、版本号提取。
"""
import json
import subprocess
import urllib.request
from pathlib import Path
from .config import (
    KERNEL_DIR, KERNEL_RELEASE_REPO,
    KERNEL_TAG_STABLE, KERNEL_TAG_RK3588,
)


def _run(cmd, check=True):
    if isinstance(cmd, str):
        cmd = cmd.split()
    print(f"[kernel] {' '.join(cmd)}")
    return subprocess.run(cmd, check=check)


def get_latest_kernel_filename(tag: str) -> str:
    """
    查询 GitHub releases，返回最新内核文件名。
    tag 可为 kernel_stable 或 kernel_rk3588。
    """
    api_url = f"https://api.github.com/repos/{KERNEL_RELEASE_REPO}/releases/tags/{tag}"
    req = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        assets = data.get("assets", [])
        names = sorted([a["name"] for a in assets], reverse=True)
        return names[0] if names else ""
    except Exception as e:
        print(f"[kernel][WARN] 获取内核版本失败: {e}")
        return ""


def download_kernel(kernel_tag: str) -> Path:
    """
    下载最新内核包到 KERNEL_DIR，并解压。
    返回解压后的目录路径。
    """
    latest = get_latest_kernel_filename(kernel_tag)
    if not latest:
        raise RuntimeError("无法获取最新内核版本")

    print(f"[kernel] 当前远程最新版本内核包：{latest}")
    dest_file = KERNEL_DIR / latest

    if dest_file.exists():
        print("[kernel] 内核包已存在，跳过下载")
    else:
        # 清空旧内核文件
        for item in KERNEL_DIR.iterdir():
            if item != dest_file:
                if item.is_dir():
                    subprocess.run(["rm", "-rf", str(item)])
                else:
                    item.unlink()
        url = f"https://github.com/{KERNEL_RELEASE_REPO}/releases/download/{kernel_tag}/{latest}"
        print(f"[kernel] 正在下载: {url}")
        subprocess.run(["wget", "-q", "-P", str(KERNEL_DIR), url], check=True)

    # 解压（7z）
    _run(f"7z x {dest_file} -y -o{KERNEL_DIR}/", check=True)
    return KERNEL_DIR


def extract_kernel_version(kernel_dir: Path) -> str:
    """
    从 boot-xxx.tar.gz 文件名提取内核版本号。
    例: boot-5.10.160-flippy-81+.tar.gz → 5.10.160-flippy-81+
    """
    for f in kernel_dir.iterdir():
        if f.name.startswith("boot-") and f.name.endswith(".tar.gz"):
            ver = f.name[len("boot-"): -len(".tar.gz")]
            return ver
    raise RuntimeError("[kernel] 未找到 boot-xxx.tar.gz 文件")
