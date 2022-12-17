#!/bin/bash
# 此脚本为作者个人使用，用于在个人服务器上定时编译，若有类似需求可参考编写
# 编译定时任务命令：crontab –e（需要安装定时任务组件: apt-get install cron）
# 定时任务配置如下：每天凌晨3点30分执行编译
# 30 3 * * * bash /openwrt_package/scripts/schedule_package.sh

compile_firmware() {
    TARGET_DEVICE=$1
    TARGET_CONFIG=$2
    ./run_build_use_docker.sh -c $TARGET_CONFIG -d $TARGET_DEVICE -p -n $NAME
    mkdir -p $FIRMWARE_OUTPUT_DIR/$TARGET_DEVICE/
    mv $BASE_DIR/openwrt_build_tmp/artifact/*.7z $FIRMWARE_OUTPUT_DIR/$TARGET_DEVICE/
}
source /etc/profile
set -e
BASE_DIR=$(cd $(dirname $0);cd ..; pwd)
NOW_DATE=$(TZ=':Asia/Shanghai' date '+%Y%m%d')
# 固件输出根目录
FIRMWARE_DIR=/data/webroot/firmware
# 固件输出具体目录（按照日期建立目录）
FIRMWARE_OUTPUT_DIR=$FIRMWARE_DIR/$NOW_DATE
NAME_PREFIX=schedule_package
# 编译时使用的容器名
NAME=$NAME_PREFIX"_"$NOW_DATE
# 推送编译通知到手机上，可以自己到pushplus申请token配到环境中
START_CONTENT='http://www.pushplus.plus/send?token='${PUSH_TOKEN}'&title=%E5%BC%80%E5%A7%8B%E7%BC%96%E8%AF%91openwrt%E5%9B%BA%E4%BB%B6&content=%E6%9C%AC%E6%AC%A1%E7%BC%96%E8%AF%91%E5%AE%B9%E5%99%A8%E5%90%8D%EF%BC%9A'$NAME
curl $START_CONTENT
echo
START_TIME=`date +%Y-%m-%d_%H:%M:%S`
cd $BASE_DIR
[ -z "$ONLY_PACKAGE" ] && rm -rf $BASE_DIR/openwrt_build_tmp
# 根据当前日期重新建立固件目录
rm -rf $FIRMWARE_DIR && mkdir -p $FIRMWARE_OUTPUT_DIR
git pull

# 编译盒子固件，有新的盒子要定时编译往这里加
compile_firmware 'vplus' 'armv8'
compile_firmware 's912' 'armv8'
compile_firmware 's905d' 'armv8'
# 清理掉环境，开始编译路由固件
rm -rf $BASE_DIR/openwrt_build_tmp
compile_firmware 'r3g' 'r3g'

[ `docker ps -a | grep $NAME_PREFIX | wc -l` -eq 0 ] || docker rm -f $(docker ps -a |  grep "$NAME_PREFIX"  | awk '{print $1}')
echo '固件定时编译完毕：'$FIRMWARE_OUTPUT_DIR
END_TIME=`date +%Y-%m-%d_%H:%M:%S`
END_CONTENT='http://www.pushplus.plus/send?token='${PUSH_TOKEN}'&title=openwrt%E5%9B%BA%E4%BB%B6%E7%BC%96%E8%AF%91%E5%AE%8C%E6%88%90&content=openwrt%E5%9B%BA%E4%BB%B6%E6%89%80%E5%9C%A8%E7%9B%AE%E5%BD%95%EF%BC%9A'$NOW_DATE'%EF%BC%8C%E7%BC%96%E8%AF%91%E5%BC%80%E5%A7%8B%E6%97%B6%E9%97%B4%EF%BC%9A'$START_TIME'%EF%BC%8C%E7%BC%96%E8%AF%91%E5%AE%8C%E6%88%90%E6%97%B6%E9%97%B4%EF%BC%9A'$END_TIME
echo
curl $END_CONTENT