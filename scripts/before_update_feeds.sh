#!/bin/bash
# 执行脚本的目录在openwrt
cd package && PACKAGE_DIR=$PWD

# 有新的feeds按照下面格式添加即可
rm -rf $PACKAGE_DIR/kenzo && git clone https://github.com/kenzok8/openwrt-packages $PACKAGE_DIR/kenzo
cd $PACKAGE_DIR/kenzo && git checkout $OPENWRT_PACKAGES_COMMIT_ID

rm -rf $PACKAGE_DIR/passwall && git clone https://github.com/xiaorouji/openwrt-passwall.git $PACKAGE_DIR/passwall
cd $PACKAGE_DIR/passwall && git checkout $PASSWALL_PACKAGE_COMMIT_ID

# 添加自定义的部分源
rm -rf $PACKAGE_DIR/small-package && git clone https://github.com/kenzok8/small-package.git $PACKAGE_DIR/small-package
cd $PACKAGE_DIR/small-package && git checkout $SMALL_PACKAGE_COMMIT_ID
SMALL_PACKAGE_TMP=/tmp/small-package
mkdir -p $SMALL_PACKAGE_TMP
cp -r luci-app-tencentddns $SMALL_PACKAGE_TMP/
cp -r luci-app-netspeedtest $SMALL_PACKAGE_TMP/
cp -r luci-app-wolplus $SMALL_PACKAGE_TMP/
cp -r homebox $SMALL_PACKAGE_TMP/
rm -rf ./* && mv $SMALL_PACKAGE_TMP/* ./
rm -rf $SMALL_PACKAGE_TMP