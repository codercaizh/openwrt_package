# 执行脚本的目录在openwrt
## 更换n2n版本
rm -rf package/lean/n2n/patches
sed -i 's/PKG_VERSION:=3.0/PKG_VERSION:=3.1.1/g' package/lean/n2n/Makefile
sed -i 's/^PKG_HASH:.*/PKG_HASH:=skip/g' package/lean/n2n/Makefile