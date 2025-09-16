#!/bin/bash
# 执行脚本的目录在openwrt
cd package;PACKAGE_DIR=$PWD

function clondOrUpdateStore() {
    GIT_URL=$1
    STORE_NAME=$2
    if [ -d $PACKAGE_DIR/$STORE_NAME/.git ];then
        echo "$STORE_NAME 已存在，即将进行更新"
        cd $PACKAGE_DIR/$STORE_NAME
        git reset --hard
        git fetch --all
        git pull
    else
        git clone $GIT_URL $PACKAGE_DIR/$STORE_NAME
    fi
}

# 有新的feeds按照下面格式添加即可
clondOrUpdateStore "https://github.com/xiaorouji/openwrt-passwall" "passwall"
clondOrUpdateStore "https://github.com/kenzok8/small-package" "small-package"

