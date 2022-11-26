# 执行脚本的目录在openwrt/package

## 替换n2n为开发版
git clone --depth=1 https://github.com/ntop/n2n.git /tmp/n2n
mv /tmp/n2n/packages/openwrt ./
rm -rf /tmp/n2n
rm -rf /root/lede/package/lean/n2n
sed -i 's/=+n2n/=+n2n-edge/g' feeds/luci/luci-app-n2n/Makefile