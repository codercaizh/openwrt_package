#!/bin/bash
# 此脚本为定时编译示例，用于在服务器orGithub Action上定时编译，若有类似需求可参考编写
# 调用方法 ./schedule_package xxx token，其中xxx为固件输出目录，token为推送token（可为空）
set -e
OUTPUT_DIR=${1:-"$PWD"}
PUSH_TOKEN=${2:-""}
compile_firmware() {
    TARGET_DEVICE=$1
    TARGET_CONFIG=$2
    ./run_build_use_docker.sh -c $TARGET_CONFIG -d $TARGET_DEVICE -p -n $NAME_PREFIX"_"$TARGET_DEVICE
    if [ ! -d "$FIRMWARE_OUTPUT_DIR" ]; then
        rm -rf $FIRMWARE_DIR && mkdir -p $FIRMWARE_OUTPUT_DIR
    fi
    mv $BASE_DIR/openwrt_build_tmp/artifact/* $FIRMWARE_OUTPUT_DIR/
}

BASE_DIR=$(cd $(dirname $0);cd ..; pwd)
NOW_DATE=$(TZ=':Asia/Shanghai' date '+%Y%m%d')
# 固件输出根目录
FIRMWARE_DIR=$OUTPUT_DIR
# 固件输出具体目录（按照日期建立目录）
FIRMWARE_OUTPUT_DIR=$FIRMWARE_DIR/$NOW_DATE
NAME_PREFIX=schedule_package
# 推送编译通知到手机上，可以自己到pushplus申请token配到环境中
START_CONTENT='http://www.pushplus.plus/send?token='${PUSH_TOKEN}'&title=%E5%BC%80%E5%A7%8B%E7%BC%96%E8%AF%91openwrt%E5%9B%BA%E4%BB%B6&content=%E6%9C%AC%E6%AC%A1%E7%BC%96%E8%AF%91%E5%9B%BA%E4%BB%B6%E8%BE%93%E5%87%BA%E7%9B%AE%E5%BD%95%EF%BC%9A'$FIRMWARE_OUTPUT_DIR
[ -n "$PUSH_TOKEN" ] && curl $START_CONTENT
echo
START_TIME=`date +%Y-%m-%d_%H:%M:%S`
cd $BASE_DIR
[ -z "$ONLY_PACKAGE" ] && rm -rf $BASE_DIR/openwrt_build_tmp
git pull
[ `docker ps -a | grep $NAME_PREFIX | wc -l` -eq 0 ] || docker rm -f $(docker ps -a |  grep "$NAME_PREFIX"  | awk '{print $1}')
# 编译盒子固件，有新的盒子要定时编译往这里加
compile_firmware 'vplus' 'armv8'
compile_firmware 's912' 'armv8'
compile_firmware 's905d' 'armv8'
rm -rf $BASE_DIR/openwrt_build_tmp
# 清理掉环境，开始编译路由固件
compile_firmware 'r3g' 'r3g'
compile_firmware 'r3p' 'r3p'
compile_firmware 'rm2100' 'rm2100'
echo '固件定时编译完毕：'$FIRMWARE_OUTPUT_DIR
END_TIME=`date +%Y-%m-%d_%H:%M:%S`
END_CONTENT='http://www.pushplus.plus/send?token='${PUSH_TOKEN}'&title=openwrt%E5%9B%BA%E4%BB%B6%E7%BC%96%E8%AF%91%E5%AE%8C%E6%88%90&content=openwrt%E5%9B%BA%E4%BB%B6%E6%89%80%E5%9C%A8%E7%9B%AE%E5%BD%95%EF%BC%9A'$NOW_DATE'%EF%BC%8C%E7%BC%96%E8%AF%91%E5%BC%80%E5%A7%8B%E6%97%B6%E9%97%B4%EF%BC%9A'$START_TIME'%EF%BC%8C%E7%BC%96%E8%AF%91%E5%AE%8C%E6%88%90%E6%97%B6%E9%97%B4%EF%BC%9A'$END_TIME
echo
[ -n "$PUSH_TOKEN" ] && curl $END_CONTENT