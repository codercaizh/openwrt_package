#!/bin/bash
# 执行脚本的目录在openwrt
RUST_DIR=package/feeds/packages/rust
if grep -qE '^[^#]*PKG_VERSION:=1.90.0' "$RUST_DIR/Makefile"; then
    echo "rust版本有问题，使用fix版本替代"
    cp $SCRIPT_DIR/fix_bugs/rust_Makefile $RUST_DIR/Makefile
fi
# 移除 openwrt feeds 自带的核心库
rm -rf feeds/packages/net/{xray-core,v2ray-geodata,sing-box,chinadns-ng,dns2socks,hysteria,ipt2socks,microsocks,naiveproxy,shadowsocks-libev,shadowsocks-rust,shadowsocksr-libev,simple-obfs,tcping,trojan-plus,tuic-client,v2ray-plugin,xray-plugin,geoview,shadow-tls}
git clone https://github.com/Openwrt-Passwall/openwrt-passwall-packages package/passwall-packages

# 移除 openwrt feeds 过时的luci版本
rm -rf feeds/luci/applications/luci-app-passwall
git clone https://github.com/Openwrt-Passwall/openwrt-passwall package/passwall-luci

# 升级Go版本
rm -rf feeds/packages/lang/golang
git clone https://github.com/sbwml/packages_lang_golang -b 26.x feeds/packages/lang/golang
