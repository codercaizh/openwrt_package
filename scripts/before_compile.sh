#!/bin/bash
# 删除掉node依赖
sed -i '/CONFIG_NODEJS/d' .config
sed -i '/CONFIG_PACKAGE_smartdns/d' .config
sed -i '/ddns/d' .config
sed -i '/vlmcsd/d' .config
sed -i '/ssr-plus/d' .config
sed -i '/qbittorrent/d' .config