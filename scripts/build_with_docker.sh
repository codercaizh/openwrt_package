#!/bin/bash
#使用docker编译时使用脚本
SCRIPT_DIR=/opt/scripts
CONFIG_DIR=/opt/configs
OPENWRT_DIR=/opt/openwrt
PACKIT_DIR=/opt/openwrt_packit
ARTIFACT_DIR=/opt/artifact
KERNEL_DIR=/opt/kernel
#从外部传入的参数
DEVICE=$1
CONFIG=$2
ONLY_PACKAGE=$3
OUTPUT_DIR=$ARTIFACT_DIR/$DEVICE
if [ "$CONFIG" == "armv8" ];then
    BUILD_TARGET_DIR=$OPENWRT_DIR/bin/targets/armvirt/64
else
    BUILD_TARGET_DIR=$OPENWRT_DIR/bin/targets/ramips
fi
source $SCRIPT_DIR/package_firmware.sh
if [ ! -d "$OPENWRT_DIR/.git" ]; then
    echo '未找到openwrt源码，正在检出源码'
    git clone https://github.com/coolsnowwolf/lede.git --depth=1 /opt/openwrt_tmp
    echo 'openwrt源码更新完毕'
    mv /opt/openwrt_tmp/* $OPENWRT_DIR/ && mv /opt/openwrt_tmp/.* $OPENWRT_DIR/
    cd $OPENWRT_DIR
    chmod +x $SCRIPT_DIR/*.sh
    cp $SCRIPT_DIR/*feeds.sh ./
    ./before_update_feeds.sh
    ./scripts/feeds update -a
    ./scripts/feeds install -a
    ./after_update_feeds.sh
    echo 'feed更新完毕'
fi
cd $OPENWRT_DIR
cp $CONFIG_DIR/$CONFIG.config ./.config
if [ "$DEVICE" == "0" ];then
    make menuconfig
    cp .config $CONFIG_DIR/$CONFIG.config
    exit 0
fi

if test -z "$ONLY_PACKAGE";then
    echo '仅打包选项未开启，进入编译流程'
else
    # 当开启ONLY_PACKAGE选项，并且底包确实已存在，则跳过编译，直接进入打包
    echo '检测到开启仅打包选项'
    if [ -d "$BUILD_TARGET_DIR" ]; then
        SKIP_BUILD=1
        echo '当前编译产物目录已存在'
    else
        echo '当前编译产物目录不存在，仍然需要走编译流程'
    fi
fi

if test -z "$SKIP_BUILD";then
#    make defconfig
    echo '开始下载依赖'
    make download -j`nproc` || make download -j1
    echo '编译依赖下载完毕'
    rm -rf $OPENWRT_DIR/bin
    echo '开始编译底包'
    make -j`nproc`
else
    echo '跳过编译底包流程'
fi

# 检测编译是否成功

if [ ! -d "$BUILD_TARGET_DIR" ]; then
    echo '编译失败，即将使用单线程重试编译'
    make V=s -j1
else
    echo '编译完毕'
fi
if [ ! -d "$BUILD_TARGET_DIR" ]; then
    echo '编译失败，请根据日志排查原因'
    exit -1
fi

####打包部分####
if [ "$CONFIG" == "armv8" ];then
    # 拉取内核
    if [ ! -d "$KERNEL_DIR/opt/kernel" ]; then
        echo '未找到内核，正在下载最新内核'
        git clone https://github.com/breakings/OpenWrt --depth=1 $KERNEL_DIR 
        echo '内核下载完毕'
    fi
    LATEST_KERNEL_VERSION=`ls -l $KERNEL_DIR/opt/kernel | awk '{print $9}' | sort -k1.1r | head -1`
    echo '当前仓库最新内核版本：'$LATEST_KERNEL_VERSION
    cp -r $KERNEL_DIR/opt/kernel/$LATEST_KERNEL_VERSION/* $KERNEL_DIR/
    echo '开始进行打包'
    package_firmware $PACKIT_DIR $ROOTFS_TAR_PATH $DEVICE $SCRIPT_DIR/whoami
    cd $PACKIT_DIR/output/
    [ ! -d "$OUTPUT_DIR" ] && mkdir -p $OUTPUT_DIR
    rm -rf $OUTPUT_DIR/*
    echo '正在压缩镜像中'
    7z a $OUTPUT_DIR/`ls *.img | head -1`.7z ./*.img
else
    echo '打包固件中'
    7z a $OUTPUT_DIR/$DEVICE'.bin.7z' $BUILD_TARGET_DIR/*/*.bin
fi
echo '编译固件成功：'${DEVICE}
