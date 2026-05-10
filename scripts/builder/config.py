"""
builder/config.py
常量、路径、环境变量统一配置。
"""
from pathlib import Path
import os
import re

# ── 基础路径 ──────────────────────────────────────────────────────
SCRIPT_DIR = Path("/opt/scripts")
CONFIG_DIR = Path("/opt/configs")
OPENWRT_DIR = Path("/opt/openwrt")
PACKIT_DIR = Path("/opt/openwrt_packit")
ARTIFACT_DIR = Path("/opt/artifact")
KERNEL_DIR = Path("/opt/kernel")
FIX_BUGS_DIR = SCRIPT_DIR / "fix_bugs"
WHOAMI_FILE = SCRIPT_DIR / "whoami"

# ── Docker 镜像标签 ──────────────────────────────────────────────
BUILD_IMAGE_LEDE = "codercai/openwrt_package:2.0"
BUILD_IMAGE_IMMORTAL = "codercai/immortalwrt_package:2.1"

# ── 源码仓库配置 ──────────────────────────────────────────────────
SOURCE_REPOS = {
    "openwrt": {
        "url": "https://github.com/coolsnowwolf/lede.git",
        "branch": None,          # None = 默认主干/master
        "depth": None,            # None = 全量克隆（LEDE 历史不大）
    },
    "immortalwrt": {
        "url": "https://github.com/immortalwrt/immortalwrt.git",
        "branch": "openwrt-25.12",
        "depth": 1,
    },
    "immortalwrt-mt798x": {
        "url": "https://github.com/padavanonly/immortalwrt-mt798x-24.10",
        "branch": "openwrt-24.10-6.6",
        "depth": 1,
    },
}

# ── Packit 仓库 ───────────────────────────────────────────────────
PACKIT_REPO = "https://github.com/unifreq/openwrt_packit"

# ── 内核 Releases ─────────────────────────────────────────────────
KERNEL_RELEASE_REPO = "codercaizh/openwrt_package"
KERNEL_TAG_STABLE = "kernel_stable"
KERNEL_TAG_RK3588 = "kernel_rk3588"

# ── 打包脚本映射（设备 → packit 脚本名）────────────────────────
PACKIT_SCRIPTS = {
    "vplus":     "mk_h6_vplus.sh",
    "beikeyun":  "mk_rk3328_beikeyun.sh",
    "l1pro":     "mk_rk3328_l1pro.sh",
    "r66s":      "mk_rk3568_r66s.sh",
    "r68s":      "mk_rk3568_r68s.sh",
    "h66k":      "mk_rk3568_h66k.sh",
    "h68k":      "mk_rk3568_h68k.sh",
    "e25":       "mk_rk3568_e25.sh",
    "rock5b":    "mk_rk3588_rock5b.sh",
    "h88k":      "mk_rk3588_h88k.sh",
    "s905":      "mk_s905_mxqpro+.sh",
    "s905d":     "mk_s905d_n1.sh",
    "s905x2":    "mk_s905x2_x96max.sh",
    "s905x3":    "mk_s905x3_multi.sh",
    "s912":      "mk_s912_zyxq.sh",
    "s922x":     "mk_s922x_gtking.sh",
    "s922x-n2":  "mk_s922x_odroid-n2.sh",
    "qemu":      "mk_qemu-aarch64_img.sh",
    "diy":       "mk_diy.sh",
}

# ── Feeds 配置 ────────────────────────────────────────────────────
FEEDS_STORES = [
    ("https://github.com/kenzok8/openwrt-packages", "kenzo"),
    ("https://github.com/stackia/rtp2httpd",          "rtp2httpd"),
    ("https://github.com/stevenjoezhang/luci-app-cloudflarespeedtest", "luci-app-cloudflarespeedtest"),
]

# Passwall 相关
PASSWALL_PACKAGES_REPO = "https://github.com/Openwrt-Passwall/openwrt-passwall-packages"
PASSWALL_LUCI_REPO    = "https://github.com/Openwrt-Passwall/openwrt-passwall"
PASSWALL_LUCI_COMMIT  = "ffea67c"

# Go 版本替换
GOLANG_FEED_REPO = "https://github.com/sbwml/packages_lang_golang"
GOLANG_FEED_BRANCH = "26.x"

# ── 7z 压缩参数 ──────────────────────────────────────────────────
COMPRESS_ARGS = "-mx=9"

# ── 环境变量辅助 ──────────────────────────────────────────────────
def load_version_file() -> dict:
    """读取 /opt/version.sh（若存在），返回变量字典。"""
    vf = Path("/opt/version.sh")
    if not vf.exists():
        return {}
    vars_ = {}
    for line in vf.read_text().splitlines():
        m = re.match(r'^export\s+(\w+)=(.*)', line)
        if m:
            vars_[m.group(1)] = m.group(2).strip('"\'')
    return vars_

def get_env_version(default_openwrt="R25.12", default_lede=None):
    """根据 target 返回对应版本号。"""
    vf_vars = load_version_file()
    # LEDE 默认用日期版本
    if default_lede is None:
        from datetime import datetime
        now = datetime.now()
        default_lede = f"R{now.strftime('%y.%m.%d')}"
    return vf_vars.get("OPENWRT_VER") or (default_lede if os.environ.get("BUILD_TARGET") == "openwrt" else default_openwrt)
