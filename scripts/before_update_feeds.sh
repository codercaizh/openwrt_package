#!/bin/bash
# 执行脚本的目录在openwrt
cd package && PACKAGE_DIR=$PWD

# 有新的feeds按照下面格式添加即可
rm -rf $PACKAGE_DIR/kenzo && git clone https://github.com/kenzok8/openwrt-packages $PACKAGE_DIR/kenzo
cd $PACKAGE_DIR/kenzo && git checkout $OPENWRT_PACKAGES_COMMIT_ID

rm -rf $PACKAGE_DIR/passwall && git clone https://github.com/xiaorouji/openwrt-passwall.git $PACKAGE_DIR/passwall
cd $PACKAGE_DIR/passwall && git checkout $PASSWALL_PACKAGE_COMMIT_ID

# 添加自定义的部分源
SMALL_PACKAGE_DIR=$PACKAGE_DIR/small-package
SMALL_PACKAGE_TMP=/tmp/small-package
rm -rf $SMALL_PACKAGE_DIR && git clone https://github.com/kenzok8/small-package.git $SMALL_PACKAGE_TMP
cd $SMALL_PACKAGE_TMP && git checkout $SMALL_PACKAGE_COMMIT_ID && mkdir -p $SMALL_PACKAGE_DIR
mv $SMALL_PACKAGE_TMP/luci-app-tencentddns $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/luci-app-netspeedtest $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/luci-app-wolplus $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/homebox $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/luci-app-beardropper  $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/luci-app-bandwidthd  $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/luci-app-netdata  $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/luci-app-nginx-manager $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/.git $SMALL_PACKAGE_DIR/ && rm -rf $SMALL_PACKAGE_TMP
