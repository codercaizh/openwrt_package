#!/bin/bash
set -e

# ── 镜像定义 ──────────────────────────────────────────────
BUILD_IMAGE_IMMORTAL="codercai/immortalwrt_package:2.1"
BUILD_IMAGE_LEDE="codercai/openwrt_package:2.0"

# ── 用法说明 ──────────────────────────────────────────────
function usage() {
    echo "用法:"
    echo "  ${0} compile -c <config> -d <device> [-t openwrt|immortalwrt] [-p]"
    echo "  ${0} package -c <config> -d <device> [-t openwrt|immortalwrt]"
    echo "  ${0} download -c <config> [-t openwrt|immortalwrt]"
    echo "  ${0} menuconfig -c <config> [-t openwrt|immortalwrt]"
    echo ""
    echo "参数:"
    echo "  -c, --config    配置文件名（不含 .config 后缀）"
    echo "  -d, --device    设备名，如: r66s, x86, 360t7"
    echo "  -t, --target    编译目标: openwrt(LEDE) | immortalwrt（默认）"
    echo "  -p, --only      仅打包（compile 时生效）"
    echo "  -w, --workdir   工作目录（menuconfig/download 可选）"
    echo ""
    echo "特殊命令:"
    echo "  go [workdir]   - 仅启动容器"
    exit 1
}

# ── 特殊命令：go ──────────────────────────────────────────
if [ "$1" = "go" ]; then
    MOUNT_PATH=${2:-$PWD}
    docker run -it --rm --net=host \
        -v "$MOUNT_PATH:/mount" -w /mount --privileged \
        $BUILD_IMAGE_IMMORTAL \
        /bin/bash -c "echo 'now in container'; /bin/bash"
    exit 0
fi

# ── 子命令解析 ────────────────────────────────────────────
OP="${1}"
shift || usage

CONFIG=""
DEVICE=""
TARGET="immortalwrt"
WORKDIR=""

while [[ $# -gt 0 ]]; do
    case "${1}" in
        -c|--config)    CONFIG="${2}"; shift 2 ;;
        -d|--device)    DEVICE="${2}"; shift 2 ;;
        -t|--target)    TARGET="${2}"; shift 2 ;;
        -w|--workdir)   WORKDIR="${2}"; shift 2 ;;
        -p|--only)       ONLY_PACKAGE=1; shift ;;
        *)              usage ;;
    esac
done

# ── 校验 ──────────────────────────────────────────────────
[ -z "$CONFIG" ] && echo "错误: 请指定 -c/--config" && exit 1

if [ "$OP" = "compile" ] || [ "$OP" = "package" ]; then
    [ -z "$DEVICE" ] && echo "错误: compile/package 需要指定 -d/--device" && exit 1
fi

[ ! -f "./configs/$CONFIG.config" ] && echo "错误: configs/ 中未找到 $CONFIG.config" && exit 1

# ── 选择镜像 ──────────────────────────────────────────────
if [ "$TARGET" = "openwrt" ]; then
    BUILD_IMAGE=$BUILD_IMAGE_LEDE
else
    BUILD_IMAGE=$BUILD_IMAGE_IMMORTAL
fi

BUILD_DIR="${WORKDIR:-$PWD/openwrt_build_tmp}"
mkdir -p "$BUILD_DIR"/{openwrt,packit,kernel,artifact}

echo "=================================================="
echo "  操作: $OP"
echo "  平台: $TARGET"
[ -n "$DEVICE" ] && echo "  设备: $DEVICE"
echo "  配置: $CONFIG"
echo "  镜像: $BUILD_IMAGE"
echo "=================================================="

# ── 组装 Docker 参数 ──────────────────────────────────────
DOCKER_ARGS=(
    -v "$BUILD_DIR/openwrt:/opt/openwrt"
    -v "$BUILD_DIR/packit:/opt/openwrt_packit"
    -v "$BUILD_DIR/kernel:/opt/kernel"
    -v "$PWD/configs:/opt/configs"
    -v "$PWD/scripts:/opt/scripts"
    -v "$BUILD_DIR/artifact:/opt/artifact"
    --net=host
    --privileged
)

# ── 执行 ──────────────────────────────────────────────────
if [ "$OP" = "menuconfig" ]; then
    docker rm -f openwrt_menuconfig 2>/dev/null || true
    docker run -it --rm "${DOCKER_ARGS[@]}" --name openwrt_menuconfig \
        $BUILD_IMAGE \
        /opt/scripts/build.py menuconfig -c "$CONFIG" -t "$TARGET"
    exit 0
fi

if [ "$OP" = "download" ]; then
    docker rm -f openwrt_download 2>/dev/null || true
    docker run -d "${DOCKER_ARGS[@]}" --name openwrt_download \
        $BUILD_IMAGE \
        /opt/scripts/build.py download -c "$CONFIG" -t "$TARGET"
    docker logs -f openwrt_download
    exit 0
fi

CONTAINER_NAME="openwrt_build"
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

if [ "$OP" = "package" ]; then
    docker run -d "${DOCKER_ARGS[@]}" --name "$CONTAINER_NAME" \
        $BUILD_IMAGE \
        /opt/scripts/build.py package -d "$DEVICE" -c "$CONFIG" -t "$TARGET"
else
    docker run -d "${DOCKER_ARGS[@]}" --name "$CONTAINER_NAME" \
        $BUILD_IMAGE \
        /opt/scripts/build.py compile -d "$DEVICE" -c "$CONFIG" -t "$TARGET"
fi

docker logs -f "$CONTAINER_NAME"

# 检查产物
if ls "$BUILD_DIR/artifact/$DEVICE"/*.7z &>/dev/null; then
    echo "=================================================="
    echo "  编译成功！产物在: $BUILD_DIR/artifact/$DEVICE/"
    echo "=================================================="
    exit 0
else
    echo "=================================================="
    echo "  编译失败，请检查日志"
    echo "=================================================="
    exit 1
fi
