#!/bin/bash
# 执行脚本的目录在openwrt
RUST_DIR=package/feeds/packages/rust
if grep -qE '^[^#]*PKG_VERSION:=1.90.0' "$RUST_DIR/Makefile"; then
    echo "rust版本有问题，使用fix版本替代"
    cp $SCRIPT_DIR/fix_bugs/rust_Makefile $RUST_DIR/Makefile
fi

