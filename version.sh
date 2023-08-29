# 此文件用于配置编译时使用的openwrt源码版本以及插件版本，若文件或配置不存在，则默认使用最新的代码编译
# 问：为什么要有这个版本号机制而不是默认就使用最新版本编译
# 答：由于OP和插件源码更新非常频繁，部分更新可能会导致编译失败，所以需要有一个Release版本机制，记录百分百能够编出来的版本
# 目前本文件由Workflow定时自动更新
OPENWRT_VER=R23.08.30
OPENWRT_COMMIT_ID=7460dcc802b1232ec6e834fae12f546784353ed8
OPENWRT_PACKAGES_COMMIT_ID=17b9d3d8ba47e77ad4c49fabfa2c2e4d77dbf489
PASSWALL_PACKAGE_COMMIT_ID=83edb2e50a0cf440b2dcfbdff9bab5fd799859e4
SMALL_PACKAGE_COMMIT_ID=b896db3300a2effde6a3a6ac27836539f8240f02
