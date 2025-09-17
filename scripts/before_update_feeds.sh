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
# clondOrUpdateStore "https://github.com/kenzok8/openwrt-packages" "kenzo" $OPENWRT_PACKAGES_COMMIT_ID
clondOrUpdateStore "https://github.com/xiaorouji/openwrt-passwall" "passwall" $PASSWALL_PACKAGE_COMMIT_ID
clondOrUpdateStore "https://github.com/kenzok8/small-package" "small-package" $SMALL_PACKAGE_COMMIT_ID
# 添加自定义的部分源
SMALL_PACKAGE_DIR=$PACKAGE_DIR/small-package;
SMALL_PACKAGE_TMP=/tmp/small-package
mv $SMALL_PACKAGE_DIR $SMALL_PACKAGE_TMP && mkdir $SMALL_PACKAGE_DIR
grep -E '^CONFIG_PACKAGE_luci-app-[^_]*=y$' "$CONFIG_DIR/$CONFIG.config" \
  | sed -E 's/^CONFIG_PACKAGE_(luci-app-[^=]+)=y$/\1/' \
  | while IFS= read -r app; do
      source="$SMALL_PACKAGE_TMP/$app"
      [ -d "$source" ] && mv $source $SMALL_PACKAGE_DIR/ && echo "${app} 已安装" || echo "${app} 第三方库不存在"
    done
mv $SMALL_PACKAGE_TMP/.git $SMALL_PACKAGE_DIR/ && rm -rf $SMALL_PACKAGE_TMP
