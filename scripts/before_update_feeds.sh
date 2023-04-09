#!/bin/bash
# 执行脚本的目录在openwrt
cd package && PACKAGE_DIR=$PWD

# 有新的feeds按照下面格式添加即可
rm -rf $PACKAGE_DIR/kenzo && git clone https://github.com/kenzok8/openwrt-packages $PACKAGE_DIR/kenzo
cd $PACKAGE_DIR/kenzo && git checkout $OPENWRT_PACKAGES_COMMIT_ID

rm -rf $PACKAGE_DIR/passwall && git clone https://github.com/xiaorouji/openwrt-passwall.git $PACKAGE_DIR/passwall
cd $PACKAGE_DIR/passwall && git checkout $PASSWALL_PACKAGE_COMMIT_ID

# 添加自定义的部分源
rm -rf $PACKAGE_DIR/small-package && mkdir -p $PACKAGE_DIR/small-package && git clone https://github.com/kenzok8/small-package.git --depth=1 /tmp/small-package
cp -r /tmp/small-package/luci-app-tencentddns $PACKAGE_DIR/small-package/
cp -r /tmp/small-package/luci-app-netspeedtest $PACKAGE_DIR/small-package/
cp -r /tmp/small-package/luci-app-wolplus $PACKAGE_DIR/small-package/
cp -r /tmp/small-package/homebox $PACKAGE_DIR/small-package/
rm -rf /tmp/small-package