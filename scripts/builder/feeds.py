"""
builder/feeds.py
Feeds 更新 + before_compile / before_update_feeds 逻辑。
"""
import subprocess
import shutil
from pathlib import Path
from .config import (
    OPENWRT_DIR, SCRIPT_DIR, FIX_BUGS_DIR,
    FEEDS_STORES, PASSWALL_PACKAGES_REPO, PASSWALL_LUCI_REPO,
    PASSWALL_LUCI_COMMIT, GOLANG_FEED_REPO, GOLANG_FEED_BRANCH,
)


def _run(cmd, cwd=None, check=True):
    if isinstance(cmd, str):
        cmd = cmd.split()
    print(f"[feeds] {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, check=check)


def _clone_or_update(url: str, dest: Path) -> None:
    """克隆或更新一个 feeds 仓库。"""
    if (dest / ".git").exists():
        print(f"[feeds] {dest.name} 已存在，正在更新...")
        _run("git reset --hard", cwd=str(dest))
        _run("git fetch --all", cwd=str(dest))
        _run("git pull", cwd=str(dest))
    else:
        print(f"[feeds] 克隆 {dest.name} ...")
        subprocess.run(["git", "clone", url, str(dest)], check=True)


def before_update_feeds() -> None:
    """
    对应原 before_update_feeds.sh：
    - 克隆/更新 kenzo、rtp2httpd、cloudflarespeedtest 等 feeds
    """
    cwd = str(OPENWRT_DIR / "package")
    for url, name in FEEDS_STORES:
        dest = Path(cwd) / name
        _clone_or_update(url, dest)


def before_compile() -> None:
    """
    对应原 before_compile.sh：
    1. 修复 rust 版本
    2. 替换 golang feeds 为 26.x 分支
    3. 移除官方 passwall 相关包，替换为 Openwrt-Passwall 版本
    """
    owrt = str(OPENWRT_DIR)

    # 1. 修复 rust
    rust_makefile = Path(owrt) / "package/feeds/packages/rust/Makefile"
    if rust_makefile.exists():
        content = rust_makefile.read_text()
        if "PKG_VERSION:=1.90.0" in content:
            print("[feeds] rust 版本有问题，使用修复版本替代")
            fix = FIX_BUGS_DIR / "rust_Makefile"
            if fix.exists():
                shutil.copy(fix, rust_makefile)
            else:
                print(f"[feeds][WARN] 未找到修复文件: {fix}")

    # 2. 替换 golang feeds
    golang_dir = Path(owrt) / "feeds/packages/lang/golang"
    if golang_dir.exists():
        shutil.rmtree(golang_dir)
    subprocess.run(
        ["git", "clone", "-b", GOLANG_FEED_BRANCH, GOLANG_FEED_REPO, str(golang_dir)],
        check=True,
    )
    print("[feeds] golang feeds 替换为 26.x 分支")

    # 3. 移除官方 passwall 相关包
    net_dir = Path(owrt) / "feeds/packages/net"
    to_remove = [
        "xray-core", "v2ray-geodata", "sing-box", "chinadns-ng",
        "dns2socks", "hysteria", "ipt2socks", "microsocks",
        "naiveproxy", "shadowsocks-libev", "shadowsocks-rust",
        "shadowsocksr-libev", "simple-obfs", "tcping",
        "trojan-plus", "tuic-client", "v2ray-plugin",
        "xray-plugin", "geoview", "shadow-tls",
    ]
    for name in to_remove:
        d = net_dir / name
        if d.exists():
            shutil.rmtree(d)

    # 4. 克隆 passwall 包和 luci
    pkg_dir = Path(owrt) / "package/passwall-packages"
    _clone_or_update(PASSWALL_PACKAGES_REPO, pkg_dir)

    luci_dir = Path(owrt) / "package/passwall-luci"
    if (Path(owrt) / "feeds/luci/applications/luci-app-passwall").exists():
        shutil.rmtree(Path(owrt) / "feeds/luci/applications/luci-app-passwall")
    _clone_or_update(PASSWALL_LUCI_REPO, luci_dir)
    subprocess.run(["git", "reset", "--hard", PASSWALL_LUCI_COMMIT], cwd=str(luci_dir))
    print("[feeds] passwall packages + luci 准备完毕")


def update_feeds() -> None:
    """完整 feeds 流程：before_update_feeds + feeds update/install。"""
    before_update_feeds()
    owrt = str(OPENWRT_DIR)
    _run("./scripts/feeds update -a", cwd=owrt, shell=True)
    _run("./scripts/feeds install -a -f", cwd=owrt, shell=True)
    print("[feeds] feeds 更新完毕")
