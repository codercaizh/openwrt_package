# 执行脚本的目录在openwrt
# sed -i '$a src-git kenzo https://github.com/kenzok8/openwrt-packages' feeds.conf.default
# sed -i '$a src-git small https://github.com/kenzok8/small' feeds.conf.default
sed -i '$a src-git small8 https://github.com/kenzok8/small-package' feeds.conf.default
# git clone https://github.com/sirpdboy/luci-app-ddns-go.git --depth=1 package/ddns-go