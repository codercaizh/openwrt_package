#!/bin/bash
# 执行脚本的目录在openwrt
## 更换n2n版本为最新版本
N2N_DIR=package/feeds/packages/n2n
rm -rf $N2N_DIR/patches
sed -i 's/PKG_VERSION:=3.0/PKG_VERSION:=3.1.1/g' $N2N_DIR/Makefile
sed -i 's/^PKG_HASH:.*/PKG_HASH:=skip/g' $N2N_DIR/Makefile