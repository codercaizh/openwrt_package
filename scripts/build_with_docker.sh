#!/bin/bash
#使用docker编译时使用脚本
set -e
SCRIPT_DIR=/opt/scripts
CONFIG_DIR=/opt/configs
OPENWRT_DIR=/opt/openwrt
PACKIT_DIR=/opt/openwrt_packit
ARTIFACT_DIR=/opt/artifact
KERNEL_DIR=/opt/kernel
#从外部传入的参数
OP=$1
DEVICE=$2
CONFIG=$3
OUTPUT_DIR=$ARTIFACT_DIR/$DEVICE
# 设置编译版本
[ -f "$OPENWRT_VERSION_FILE" ] && source $OPENWRT_VERSION_FILE
export FORCE_UNSAFE_CONFIGURE=1
export OPENWRT_VER=${OPENWRT_VER:-"R$(TZ=':Asia/Shanghai' date '+%y.%m.%d')"}
export BUILD_DEVICE=$DEVICE
export BUILD_CONFIG=$CONFIG
echo '当前选择编译版本为：'$OPENWRT_VER
source $SCRIPT_DIR/package_firmware.sh
if [ $OP != "package" ];then
    # 切换源码
    if [ ! -d "$OPENWRT_DIR/.git" ]; then
        echo '未找到openwrt源码，正在检出源码'
        # git clone https://github.com/hanwckf/immortalwrt-mt798x /opt/openwrt_tmp
        # git clone -b openwrt-24.10-6.6 --single-branch --filter=blob:none https://github.com/padavanonly/immortalwrt-mt798x-24.10 /opt/openwrt_tmp
        git clone -b openwrt-24.10 --single-branch --filter=blob:none https://github.com/immortalwrt/immortalwrt.git /opt/openwrt_tmp
        echo 'openwrt源码更新完毕'
        cp -r /opt/openwrt_tmp/. $OPENWRT_DIR/
        cd $OPENWRT_DIR
       #  git checkout "$OPENWRT_COMMIT_ID" # 切换到指定 commitId
    else
        cd $OPENWRT_DIR
       # git reset --hard;git fetch --all;git checkout "$OPENWRT_COMMIT_ID"
        # 切换到指定 commitId
       # [ `echo "$OPENWRT_COMMIT_ID"|awk '{print length($0)}'` != '40' ] && git pull # 当前为主干则更新一下代码
        rm -rf *.feeds.sh
    fi
fi
function install() {
    # 更新源与配置
    cd $OPENWRT_DIR
    chmod +x $SCRIPT_DIR/*.sh
    cp $SCRIPT_DIR/*.sh ./
    ./before_update_feeds.sh
    ./scripts/feeds update -a
    ./scripts/feeds install -a -f
    echo 'feed更新完毕'
    echo '' > ./.config
    # defconfig文件存在则拼接defconfig的配置
    DEFCONFIG=$OPENWRT_DIR/defconfig/$CONFIG.config
    [ -f "$DEFCONFIG" ] && cat ./defconfig/$CONFIG.config >> ./.config && echo 'append defconfig succeed'
    cat $CONFIG_DIR/$CONFIG.config >> ./.config
    make defconfig
}
function compile() {
    cd $OPENWRT_DIR
    ./before_compile.sh
    set +e
    echo '开始下载依赖'
    make download -j`nproc` || make download -j`nproc`
    echo '开始编译'
    rm -rf $OPENWRT_DIR/bin
    make -j`nproc` || make V=s -j1 || echo '最终编译失败，请根据日志排查原因'
    set -e
}

[ $OP == "package" ] && echo '仅打包选项开启，跳过编译流程' || install

if [ $OP == "download" ];then
    echo '依赖安装完毕，请执行以下命令进入容器完成后续操作'
    echo 'docker exec -it openwrt_download /bin/bash -c "cd /opt/openwrt;/bin/bash"'
    tail -f /dev/null
    exit 0
fi

if [ $OP == "menuconfig" ];then
    cd $OPENWRT_DIR
    make menuconfig
    cp .config $CONFIG_DIR/$CONFIG.config
    exit 0
fi

if [ $OP == "compile" ];then
    compile
fi

####打包部分####
COMPRESS_ARGS='-mx=9' 
if [[ $CONFIG == *armv8* ]];then
    ARMV8_ROOTFS_FILE_NAME="immortalwrt-armsr-armv8-generic-rootfs.tar.gz"
    ls $OPENWRT_DIR/bin/targets/armsr/armv8/$ARMV8_ROOTFS_FILE_NAME &> /dev/null || (echo '编译产物不存在，请先完成一次编译，才能进行打包';exit -1)
    [[ "${DEVICE}" == "rk3588" ]] && KERNEL_TAG="rk3588" || KERNEL_TAG="stable"
    LATEST_KERNEL_VERSION="$(curl -s -H "Accept: application/vnd.github+json" https://api.github.com/repos/breakings/OpenWrt/releases/tags/kernel_$KERNEL_TAG | jq -r '.assets[].name' | sort -rV | head -n 1)"
    echo '当前远程最新版本内核包：'$LATEST_KERNEL_VERSION
    if [ ! -f "$KERNEL_DIR/$LATEST_KERNEL_VERSION" ]; then
        echo '内核包存在更新，正在下载中'
        rm -rf $KERNEL_DIR/*
        wget "https://github.com/breakings/OpenWrt/releases/download/kernel_$KERNEL_TAG/$LATEST_KERNEL_VERSION" -q -P $KERNEL_DIR/
    fi
    tar -vxf $KERNEL_DIR/$LATEST_KERNEL_VERSION -C $KERNEL_DIR/
    mv $KERNEL_DIR/$(basename `ls -l $KERNEL_DIR | grep ^d | awk '{print $9}'`)/* $KERNEL_DIR/
    KERNEL_VERSION=$(basename `ls -l $KERNEL_DIR/* | awk '{print $9}' | grep boot | head -1`)
    KERNEL_VERSION=${KERNEL_VERSION%%.tar.gz}
    KERNEL_VERSION=${KERNEL_VERSION##boot-}
    export KERNEL_VERSION
    echo '当前仓库最新内核版本：'$KERNEL_VERSION
    echo '开始进行打包'
    package_firmware $PACKIT_DIR $OPENWRT_DIR/bin/targets/armsr/armv8/$ARMV8_ROOTFS_FILE_NAME $DEVICE $SCRIPT_DIR/whoami
    cd $PACKIT_DIR/output/
    rm -rf $OUTPUT_DIR && mkdir -p $OUTPUT_DIR
    if ls *.img &> /dev/null; then
        echo '正在压缩镜像中'
        7z a $COMPRESS_ARGS $OUTPUT_DIR/`ls *.img | head -1`.7z ./*.img
    else
        echo '盒子固件打包失败'
        exit 1
    fi
elif [[ $DEVICE == 'x86' ]];then
    if ls $OPENWRT_DIR/bin/targets/x86/*/*.img.gz &> /dev/null; then
        echo '打包x86固件中'
        7z a $COMPRESS_ARGS $OUTPUT_DIR/'openwrt_'$DEVICE'_'$OPENWRT_VER'.7z' $OPENWRT_DIR/bin/targets/x86/*/*squashfs-combined*.img.gz
    else 
        echo 'x86固件打包失败'
        exit 1
    fi
else
    # 针对ramips
    if ls $OPENWRT_DIR/bin/targets/*/*/*$DEVICE*.bin &> /dev/null; then
        echo '打包路由固件中'
        7z a $COMPRESS_ARGS $OUTPUT_DIR/'openwrt_'$DEVICE'_'$OPENWRT_VER'.bin.7z' $OPENWRT_DIR/bin/targets/*/*/*$DEVICE*.bin
    else 
        echo '路由固件打包失败'
        exit 1
    fi
fi
echo '编译固件成功：'${DEVICE}
