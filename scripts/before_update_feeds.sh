#!/bin/bash
# 执行脚本的目录在openwrt
cd package;PACKAGE_DIR=$PWD

function clondOrUpdateStore() {
    GIT_URL=$1
    STORE_NAME=$2
    COMMIT_ID=$3
    if [ -d $PACKAGE_DIR/$STORE_NAME/.git ];then
        echo "$STORE_NAME 已存在，即将进行更新"
        cd $PACKAGE_DIR/$STORE_NAME
        git reset --hard
        git fetch --all
    else
        git clone $GIT_URL $PACKAGE_DIR/$STORE_NAME
    fi
    cd $PACKAGE_DIR/$STORE_NAME
    git checkout $COMMIT_ID
    [ `echo "$COMMIT_ID"|awk '{print length($0)}'` != '40' ] && git pull # 如果是master分支则拉一下最新代码
}

# 有新的feeds按照下面格式添加即可
clondOrUpdateStore "https://github.com/kenzok8/openwrt-packages" "kenzo" $OPENWRT_PACKAGES_COMMIT_ID
clondOrUpdateStore "https://github.com/xiaorouji/openwrt-passwall" "passwall" $PASSWALL_PACKAGE_COMMIT_ID
clondOrUpdateStore "https://github.com/kenzok8/small-package" "small-package" $SMALL_PACKAGE_COMMIT_ID
clondOrUpdateStore "https://github.com/kenzok8/small" "small" $SMALL_COMMIT_ID

# 添加自定义的部分源
SMALL_PACKAGE_DIR=$PACKAGE_DIR/small-package;
SMALL_PACKAGE_TMP=/tmp/small-package
mv $SMALL_PACKAGE_DIR $SMALL_PACKAGE_TMP && mkdir $SMALL_PACKAGE_DIR
mv $SMALL_PACKAGE_TMP/luci-app-tencentddns $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/luci-app-netspeedtest $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/luci-app-wolplus $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/homebox $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/luci-app-bandwidthd  $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/luci-app-netdata  $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/luci-app-nginx-manager $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/luci-app-passwall $SMALL_PACKAGE_DIR/
mv $SMALL_PACKAGE_TMP/.git $SMALL_PACKAGE_DIR/ && rm -rf $SMALL_PACKAGE_TMP
