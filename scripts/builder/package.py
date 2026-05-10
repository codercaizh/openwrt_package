"""
builder/package.py
打包逻辑：armv8（调用 packit）、x86、mt798x 路由固件。
"""
import os
import shutil
import subprocess
from pathlib import Path
from .config import (
    OPENWRT_DIR, PACKIT_DIR, ARTIFACT_DIR, KERNEL_DIR,
    SCRIPT_DIR, WHOAMI_FILE, PACKIT_REPO, PACKIT_SCRIPTS,
    COMPRESS_ARGS,
)
from .kernel import download_kernel, extract_kernel_version


def _run(cmd, cwd=None, check=True, shell=False):
    if isinstance(cmd, str) and not shell:
        cmd = cmd.split()
    tag = "[package]"
    print(f"{tag} {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    return subprocess.run(cmd, cwd=cwd, check=check, shell=shell)


def _invoke_script(script_path: Path) -> bool:
    """执行打包脚本，失败重试两次。"""
    for i in range(3):
        result = subprocess.run(["bash", str(script_path)], cwd=str(PACKIT_DIR))
        if result.returncode == 0:
            return True
        print(f"[package][WARN] {script_path.name} 第 {i+1} 次失败，重试...")
    return False


def _ensure_packit() -> None:
    """确保 packit 源码存在。"""
    if (PACKIT_DIR / ".git").exists():
        print("[package] packit 已存在，更新中...")
        _run("git fetch --all", cwd=str(PACKIT_DIR))
        _run("git pull", cwd=str(PACKIT_DIR))
    else:
        print("[package] 克隆 packit 源码...")
        if PACKIT_DIR.exists():
            shutil.rmtree(PACKIT_DIR)
        _run(["git", "clone", "--depth=1", PACKIT_REPO, str(PACKIT_DIR)])


def run_package(target: str, config: str, device: str) -> bool:
    """
    主打包入口，根据 config 和 device 分路由：
    - armv8* config → armv8 盒子固件（需要 packit + 内核）
    - x86       device → x86 固件
    - 其他      → mt798x / 路由固件
    """
    output_dir = ARTIFACT_DIR / device
    build_ver = os.environ.get("OPENWRT_VER", "R25.12")

    if "armv8" in config:
        return _package_armv8(device, output_dir, build_ver)
    elif device == "x86":
        return _package_x86(output_dir, build_ver)
    else:
        return _package_router(device, output_dir, build_ver)


def _package_armv8(device: str, output_dir: Path, build_ver: str) -> bool:
    """armv8 盒子固件打包（需要 packit + 内核）"""
    # 1. 找到 rootfs
    if os.environ.get("BUILD_TARGET") == "openwrt":
        rootfs_name = "openwrt-armvirt-64-generic-rootfs.tar.gz"
        rootfs_path = OPENWRT_DIR / "bin/targets/armvirt/64" / rootfs_name
    else:
        rootfs_name = "immortalwrt-armsr-armv8-generic-rootfs.tar.gz"
        rootfs_path = OPENWRT_DIR / "bin/targets/armsr/armv8" / rootfs_name

    if not rootfs_path.exists():
        print(f"[package][ERROR] 编译产物不存在: {rootfs_path}")
        return False
    print(f"[package] 找到 rootfs: {rootfs_name}")

    # 2. 下载内核
    kernel_tag = "kernel_rk3588" if device == "rk3588" else "kernel_stable"
    download_kernel(kernel_tag)
    kernel_ver = extract_kernel_version(KERNEL_DIR)
    os.environ["KERNEL_VERSION"] = kernel_ver
    print(f"[package] 当前内核版本：{kernel_ver}")

    # 3. 准备 packit
    _ensure_packit()
    # 清理旧产物
    for p in PACKIT_DIR.glob("*rootfs.tar.gz"):
        p.unlink(missing_ok=True)
    for p in (PACKIT_DIR / "output").glob("*"):
        if p.is_file():
            p.unlink()
    for p in (PACKIT_DIR / "tmp").glob("*"):
        if p.is_file():
            p.unlink()

    # 复制 rootfs + whoami 到 packit 目录
    shutil.copy(rootfs_path, PACKIT_DIR / rootfs_name)
    os.environ["OP_ROOT_TGZ"] = rootfs_name
    if WHOAMI_FILE.exists():
        shutil.copy(WHOAMI_FILE, PACKIT_DIR / "whoami")

    # 4. 找到并执行打包脚本
    script_name = PACKIT_SCRIPTS.get(device)
    if not script_name:
        print(f"[package][ERROR] 未找到设备 {device} 的打包脚本")
        return False
    script_path = PACKIT_DIR / script_name
    if not script_path.exists():
        print(f"[package][ERROR] 打包脚本不存在: {script_path}")
        return False

    # 修改 ROOTFS_MB=1024
    content = script_path.read_text()
    content = content.replace("ROOTFS_MB=2048", "ROOTFS_MB=1024")
    script_path.write_text(content)

    print(f"[package] 开始打包，设备: {device}，脚本: {script_name}")
    if not _invoke_script(script_path):
        print("[package][ERROR] 盒子固件打包失败")
        return False

    # 5. 压缩产物
    output_dir.mkdir(parents=True, exist_ok=True)
    img_files = list((PACKIT_DIR / "output").glob("*.img"))
    if not img_files:
        print("[package][ERROR] 打包后未找到 .img 文件")
        return False

    for img in img_files:
        out_7z = output_dir / f"{img.name}.7z"
        _run(f"7z a {COMPRESS_ARGS} {out_7z} {img}", shell=True)
        print(f"[package] 已压缩: {out_7z}")

    print(f"[package] 盒子固件打包成功：{device}")
    return True


def _package_x86(output_dir: Path, build_ver: str) -> bool:
    """x86 固件打包"""
    pattern = "bin/targets/x86/*/*squashfs-combined*.img.gz"
    matches = list(OPENWRT_DIR.glob(pattern))
    if not matches:
        print("[package][ERROR] 未找到 x86 固件产物")
        return False

    output_dir.mkdir(parents=True, exist_ok=True)
    out_7z = output_dir / f"openwrt_x86_{build_ver}.7z"
    file_list = " ".join(str(m) for m in matches)
    _run(f"7z a {COMPRESS_ARGS} {out_7z} {file_list}", shell=True)
    print(f"[package] x86 固件打包成功: {out_7z}")
    return True


def _package_router(device: str, output_dir: Path, build_ver: str) -> bool:
    """mt798x / 普通路由固件打包"""
    pattern = f"bin/targets/*/*/*{device}*.bin"
    matches = list(OPENWRT_DIR.glob(pattern))
    if not matches:
        print(f"[package][ERROR] 未找到设备 {device} 的固件产物")
        return False

    output_dir.mkdir(parents=True, exist_ok=True)
    out_7z = output_dir / f"openwrt_{device}_{build_ver}.bin.7z"
    file_list = " ".join(str(m) for m in matches)
    _run(f"7z a {COMPRESS_ARGS} {out_7z} {file_list}", shell=True)
    print(f"[package] 路由固件打包成功: {device}")
    return True
