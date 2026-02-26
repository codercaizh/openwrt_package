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
clondOrUpdateStore "https://github.com/kenzok8/openwrt-packages" "kenzo"
# clondOrUpdateStore "https://github.com/xiaorouji/openwrt-passwall" "passwall" $PASSWALL_PACKAGE_COMMIT_ID
clondOrUpdateStore "https://github.com/kenzok8/small-package" "small-package" 
clondOrUpdateStore "https://github.com/sirpdboy/luci-app-netspeedtest" "netspeedtest" 
# 添加自定义的部分源
SMALL_PACKAGE_DIR=$PACKAGE_DIR/small-package;
SMALL_PACKAGE_TMP=/tmp/small-package
mv $SMALL_PACKAGE_DIR $SMALL_PACKAGE_TMP && mkdir $SMALL_PACKAGE_DIR

grep -E '^CONFIG_PACKAGE_luci-app-[^_]*=y$' "$CONFIG_DIR/$BUILD_CONFIG.config" \
 | sed -E 's/^CONFIG_PACKAGE_(luci-app-[^=]+)=y$/\1/' \
 | while IFS= read -r app; do
     # 从 luci-app-xxx 中提取 xxx 部分
     keyword=$(echo "$app" | sed -E 's/^luci-app-(.+)$/\1/')
     
     # 在临时目录中查找所有包含关键字的文件夹
     found_dirs=$(find "$SMALL_PACKAGE_TMP" -maxdepth 1 -type d -name "*${keyword}*")
     
     if [ -n "$found_dirs" ]; then
         moved_count=0
         # 直接遍历找到的目录，避免使用管道创建子shell
         for found_dir in $found_dirs; do
             if [ -n "$found_dir" ] && [ -d "$found_dir" ]; then
                 dir_name=$(basename "$found_dir")
                 mv "$found_dir" "$SMALL_PACKAGE_DIR/" 2>/dev/null
                 if [ $? -eq 0 ]; then
                     echo "${app} (对应目录: ${dir_name}) 已移动"
                     moved_count=$((moved_count + 1))
                 fi
             fi
         done
         # 现在 moved_count 的修改会在循环中保留
         if [ $moved_count -eq 0 ]; then
             echo "${app} (关键字: ${keyword}) 对应目录移动失败"
         fi
     else
         echo "${app} (关键字: ${keyword}) 对应目录不存在"
     fi
   done
