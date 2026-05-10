#!/usr/bin/env python3
"""
scripts/build.py
OpenWrt / ImmortalWrt 编译 + 打包统一入口（Python 重写版）

用法（容器内）:
  python3 build.py compile -d <device> -c <config> [-t openwrt|immortalwrt]
  python3 build.py package -d <device> -c <config> [-t openwrt|immortalwrt]
  python3 build.py download -c <config> [-t openwrt|immortalwrt]
  python3 build.py menuconfig -c <config> [-t openwrt|immortalwrt]

参数:
  op               compile | package | download | menuconfig
  -d, --device   设备名，如: r66s, x86, 360t7
  -c, --config    配置文件名（不含 .config 后缀），如: armv8, 360t7
  -t, --target    openwrt(LEDE) | immortalwrt（默认）
  -w, --workdir   工作目录（menuconfig/download 可选）

环境变量:
  OPENWRT_VER       编译版本号（可选）
  FORCE_UNSAFE_CONFIGURE=1（自动设置）
"""
import sys
import os
import shutil
import argparse
from pathlib import Path
from datetime import datetime

# ── 添加 scripts/ 到 sys.path ────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from builder import (
    OPENWRT_DIR, CONFIG_DIR,
    load_version_file,
)
from builder.source import setup_source
from builder.feeds import update_feeds, before_compile
from builder.build import configure_config, run_download, run_compile
from builder.package import run_package


def parse_args():
    parser = argparse.ArgumentParser(
        description="OpenWrt / ImmortalWrt 编译打包工具"
    )
    subparsers = parser.add_subparsers(dest="op", required=True)

    # compile
    compile_parser = subparsers.add_parser("compile", help="完整编译流程")
    compile_parser.add_argument("-d", "--device", required=True, help="设备名")
    compile_parser.add_argument("-c", "--config", required=True, help="配置文件名（不含 .config）")
    compile_parser.add_argument("-t", "--target", default="immortalwrt",
                               choices=["openwrt", "immortalwrt"], help="编译目标平台")
    compile_parser.add_argument("-w", "--workdir", default=None, help="工作目录（可选）")

    # package
    package_parser = subparsers.add_parser("package", help="仅打包（跳过编译）")
    package_parser.add_argument("-d", "--device", required=True, help="设备名")
    package_parser.add_argument("-c", "--config", required=True, help="配置文件名（不含 .config）")
    package_parser.add_argument("-t", "--target", default="immortalwrt",
                               choices=["openwrt", "immortalwrt"], help="编译目标平台")

    # download
    download_parser = subparsers.add_parser("download", help="仅下载依赖")
    download_parser.add_argument("-c", "--config", required=True, help="配置文件名（不含 .config）")
    download_parser.add_argument("-t", "--target", default="immortalwrt",
                                choices=["openwrt", "immortalwrt"], help="编译目标平台")
    download_parser.add_argument("-w", "--workdir", default=None, help="工作目录（可选）")

    # menuconfig
    menuconfig_parser = subparsers.add_parser("menuconfig", help="运行 make menuconfig")
    menuconfig_parser.add_argument("-c", "--config", required=True, help="配置文件名（不含 .config）")
    menuconfig_parser.add_argument("-t", "--target", default="immortalwrt",
                                  choices=["openwrt", "immortalwrt"], help="编译目标平台")
    menuconfig_parser.add_argument("-w", "--workdir", default=None, help="工作目录（可选）")

    return parser.parse_args()


def setup_environment(target: str, config: str):
    """设置环境变量"""
    os.environ["FORCE_UNSAFE_CONFIGURE"] = "1"
    os.environ["BUILD_CONFIG"] = config
    os.environ["BUILD_TARGET"] = target

    # 版本号
    vf_vars = load_version_file()
    if "OPENWRT_VER" in vf_vars:
        os.environ["OPENWRT_VER"] = vf_vars["OPENWRT_VER"]
    elif target == "openwrt":
        os.environ["OPENWRT_VER"] = f"R{datetime.now().strftime('%y.%m.%d')}"
    else:
        os.environ["OPENWRT_VER"] = "R25.12"

    print(f"\n{'='*50}")
    print(f"  编译目标平台: {target}")
    if hasattr(args, 'device') and args.device:
        os.environ["BUILD_DEVICE"] = args.device
        print(f"  设备: {args.device}")
    print(f"  配置文件: {config}")
    print(f"  版本号: {os.environ['OPENWRT_VER']}")
    print(f"{'='*50}\n")


def op_menuconfig(args):
    """make menuconfig 并保存配置"""
    ret = os.system(f"cd {OPENWRT_DIR} && make menuconfig")
    if ret != 0:
        print("[main][ERROR] menuconfig 失败")
        sys.exit(1)
    config_path = CONFIG_DIR / f"{args.config}.config"
    shutil.copy(OPENWRT_DIR / ".config", config_path)
    print(f"[main] 配置已保存到: {config_path}")
    sys.exit(0)


def op_download(args):
    """仅下载依赖，不编译"""
    setup_source(args.target, args.config)
    update_feeds()
    before_compile()
    configure_config(args.config, args.target)
    run_download()
    print("\n[main] 依赖下载完毕。")
    print("[main] 进入容器继续操作: docker exec -it openwrt_download /bin/bash -c 'cd /opt/openwrt; /bin/bash'")
    # 阻塞，保持容器运行
    os.system("tail -f /dev/null")


def op_compile(args):
    """完整编译流程"""
    # 1. 源码
    setup_source(args.target, args.config)

    # 2. feeds + 配置
    update_feeds()
    before_compile()
    configure_config(args.config, args.target)

    # 3. 编译
    if not run_compile():
        print("[main][ERROR] 编译失败")
        sys.exit(1)

    # 4. 打包
    if not run_package(args.target, args.config, args.device):
        print("[main][ERROR] 打包失败")
        sys.exit(1)

    print(f"\n[main] 编译固件成功：{args.device}")
    sys.exit(0)


def op_package(args):
    """仅打包（跳过编译）"""
    # 打包也需要 feeds 配置（为了 defconfig 拼接）
    update_feeds()
    before_compile()
    configure_config(args.config, args.target)

    print("[main] 仅打包，跳过编译...")
    if not run_package(args.target, args.config, args.device):
        print("[main][ERROR] 打包失败")
        sys.exit(1)
    print(f"\n[main] 打包完成：{args.device}")
    sys.exit(0)


if __name__ == "__main__":
    args = parse_args()
    setup_environment(args.target, args.config)

    if args.op == "menuconfig":
        op_menuconfig(args)
    elif args.op == "download":
        op_download(args)
    elif args.op == "compile":
        op_compile(args)
    elif args.op == "package":
        op_package(args)
