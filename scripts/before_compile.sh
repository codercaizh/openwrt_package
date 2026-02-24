#!/bin/bash
# 执行脚本的目录在openwrt
RUST_DIR=package/feeds/packages/lang/rust
sed -i 's/PKG_VERSION:=1.90.0/PKG_VERSION:=1.93.1/g' $RUST_DIR/Makefile
sed -i 's/^PKG_HASH:.*/PKG_HASH:=skip/g' $RUST_DIR/Makefile
