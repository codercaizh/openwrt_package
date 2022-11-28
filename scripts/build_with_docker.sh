#!/bin/bash
#使用docker编译时使用脚本
SCRIPT_DIR=/opt/scripts
OPENWRT_DIR=/opt/openwrt
echo '当前cpu核心数：'`nproc`
if [ ! -f "$OPENWRT_DIR/Makefile" ]; then
    echo '未找到openwrt源码，正在检出源码'
    git clone --depth=1 https://github.com/coolsnowwolf/lede.git /opt/openwrt_tmp
    mv /opt/openwrt_tmp/* $OPENWRT_DIR/
    mv /opt/openwrt_tmp/.git $OPENWRT_DIR/
fi
cd $OPENWRT_DIR
git reset --hard && git pull
chmod +x $SCRIPT_DIR/*.sh
cp $SCRIPT_DIR/*.sh ./
./before_update_feeds.sh
./scripts/feeds update -a
./scripts/feeds install -a
./after_update_feeds.sh
make defconfig
make download -j$((`nproc` + 1))
make V=s -j$((`nproc` + 1))