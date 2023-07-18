# 此文件用于配置编译时使用的openwrt源码版本以及插件版本，若文件或配置不存在，则默认使用最新的代码编译
# 问：为什么要有这个版本号机制而不是默认就使用最新版本编译
# 答：由于OP和插件源码更新非常频繁，部分更新可能会导致编译失败，所以需要有一个Release版本机制，记录百分百能够编出来的版本
# 目前本文件由Workflow定时自动更新
OPENWRT_VER=R23.07.19
OPENWRT_COMMIT_ID=dff0d3553d314269de9a5f840abcca6232407f49
OPENWRT_PACKAGES_COMMIT_ID=e7466adcc6b0de5055ebbd45cab747022fe9485e
PASSWALL_PACKAGE_COMMIT_ID=46d3d46794d38d6f2bb20d14c3c1528eaebc1b1c
SMALL_PACKAGE_COMMIT_ID=8e7dc5543342854d500cd6eb9b5be4c23be26177
