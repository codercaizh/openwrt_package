# 此文件用于配置编译时使用的openwrt源码版本以及插件版本，若文件或配置不存在，则默认使用最新的代码编译
# 问：为什么要有这个版本号机制而不是默认就使用最新版本编译
# 答：由于OP和插件源码更新非常频繁，部分更新可能会导致编译失败，所以需要有一个Release版本机制，记录百分百能够编出来的版本
# 目前本文件由Workflow定时自动更新
OPENWRT_VER=R23.05.10
OPENWRT_COMMIT_ID=0f270a2436c9fd79750c779a23baeaf6e3993872
OPENWRT_PACKAGES_COMMIT_ID=ae3f5980cb899be2405ed4eec8074b34144083e1
PASSWALL_PACKAGE_COMMIT_ID=56a799e3d4c5852a597799ae0dc0898d1c25459b
SMALL_PACKAGE_COMMIT_ID=d136b3404b0312d147b0144f29ab564784e0c3bd
