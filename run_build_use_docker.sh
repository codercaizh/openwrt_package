#!/bin/bash
#使用docker进行编译
IMAGE_NAME=openwrt_build
BUILD_DIR=$PWD/openwrt_build_tmp
DEVICE=${1:-vplus}
CONFIG=${2:-common}
echo '当前选择编译的设备：'$DEVICE
echo '当前选择编译的配置：'$CONFIG
mkdir -p $BUILD_DIR
docker rm -f $IMAGE_NAME
[ `docker image ls $IMAGE_NAME | wc -l` -eq 2 ] || docker build . --tag=$IMAGE_NAME
[ -d "$BUILD_DIR" ] || mkdir -p $BUILD_DIR
cp $CONFIG.config $BUILD_DIR/openwrt/.config
docker run -d \
-v $BUILD_DIR/openwrt:/opt/openwrt \
-v $BUILD_DIR/packit:/opt/openwrt_packit \
-v $PWD/scripts:/opt/scripts \
-v $BUILD_DIR/artifact:/opt/artifact \
--privileged \
--name $IMAGE_NAME $IMAGE_NAME $DEVICE
docker logs -f $IMAGE_NAME
