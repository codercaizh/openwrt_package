"""
builder/build.py
编译流程：make download + make。
"""
import subprocess
from .config import OPENWRT_DIR


def _run(cmd, cwd=None, check=True, shell=False):
    if isinstance(cmd, str) and not shell:
        cmd = cmd.split()
    print(f"[build] {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    return subprocess.run(cmd, cwd=cwd, check=check, shell=shell)


def configure_config(config: str, target: str) -> None:
    """
    生成最终 .config：
    - openwrt 目标：直接 cp config
    - immortalwrt 目标：拼接 defconfig（若有）+ config
    """
    owrt = str(OPENWRT_DIR)
    config_file = OPENWRT_DIR / "configs" / f"{config}.config"
    # 检查 configs 目录是否挂载正确
    if not config_file.exists():
        # 可能挂载在 /opt/configs
        config_file = Path("/opt/configs") / f"{config}.config"
    if not config_file.exists():
        raise FileNotFoundError(f"未找到配置文件: {config}.config")

    # 清空老配置
    (OPENWRT_DIR / ".config").write_text("")

    if target == "openwrt":
        # LEDE：直接复制
        import shutil
        shutil.copy(config_file, OPENWRT_DIR / ".config")
    else:
        # ImmortalWrt：先拼接 defconfig
        content = ""
        defconfig_line = ""
        for line in config_file.read_text().splitlines():
            if line.startswith("#CONFIG_APPEND="):
                defconfig_line = line.split("=", 1)[1].strip()
            else:
                content += line + "\n"
        if defconfig_line:
            defconfig_path = OPENWRT_DIR / "defconfig" / defconfig_line
            if defconfig_path.exists():
                content = defconfig_path.read_text() + "\n" + content
                print(f"[build] 拼接 defconfig: {defconfig_line}")
            else:
                print(f"[build][WARN] defconfig 未找到: {defconfig_path}")
        (OPENWRT_DIR / ".config").write_text(content)

    # make defconfig
    _run("make defconfig", cwd=owrt, shell=True)


def run_download() -> None:
    """make download，失败自动重试一次。"""
    owrt = str(OPENWRT_DIR)
    print("[build] 开始下载依赖...")
    _run(f"make download -j$(nproc)", cwd=owrt, check=False, shell=True)
    # 如果第一次失败，再试一次
    result = subprocess.run(
        "make download -j$(nproc)",
        cwd=owrt, shell=True, capture_output=True, text=True
    )
    if result.returncode != 0:
        print("[build][WARN] download 有失败，但继续编译...")


def run_compile() -> bool:
    """执行 make 编译，返回是否成功。"""
    owrt = str(OPENWRT_DIR)
    print("[build] 开始编译...")
    import shutil
    # 清理旧产物
    shutil.rmtree(OPENWRT_DIR / "bin", ignore_errors=True)

    result = subprocess.run(
        f"make -j$(nproc)",
        cwd=owrt, shell=True, capture_output=True, text=True
    )
    if result.returncode == 0:
        print("[build] 编译成功")
        return True

    print("[build][WARN] 并行编译失败，尝试单线程...")
    result2 = subprocess.run(
        "make V=s -j1",
        cwd=owrt, shell=True, capture_output=True, text=True
    )
    if result2.returncode == 0:
        print("[build] 单线程编译成功")
        return True

    print("[build][ERROR] 最终编译失败，请根据日志排查原因")
    return False
