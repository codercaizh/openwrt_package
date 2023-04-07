#!/bin/bash
# 此文件用于配置编译时使用的openwrt源码版本以及插件版本，若没有配置或文件不存着，在默认使用最新的代码编译
# 问：为什么要有这个版本号机制而不是默认就使用最新版本编译
# 答：由于OP和插件源码更新非常频繁，部分更新可能会导致编译失败，所以需要有一个Release的版本机制，记录百分百能够编出来的版本
OPENWRT_VER=
OPENWRT_COMMIT_ID=
OPENWRT_PACKAGES_COMMIT_ID=
SMALL_PACKAGE_COMMIT_ID=