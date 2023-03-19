#!/bin/bash
# 执行脚本的目录在openwrt
cd package && PACKAGE_DIR=$PWD

# 有新的feeds按照下面格式添加即可
rm -rf $PACKAGE_DIR/kenzo && git clone https://github.com/kenzok8/openwrt-packages $PACKAGE_DIR/kenzo
cd $PACKAGE_DIR/kenzo && git checkout $OPENWRT_PACKAGES_COMMIT_ID

rm -rf $PACKAGE_DIR/small8 && git clone https://github.com/kenzok8/small-package $PACKAGE_DIR/small8
cd $PACKAGE_DIR/small8 && git checkout $SMALL_PACKAGE_COMMIT_ID