#!/bin/bash
#使用docker进行编译
IMAGE_NAME=openwrt_build
BUILD_DIR=$PWD/openwrt_build_tmp
docker rm -f $IMAGE_NAME
[ `docker image ls $IMAGE_NAME | wc -l` -eq 2 ] || docker build . --tag=$IMAGE_NAME
[ -d "$BUILD_DIR" ] || mkdir -p $BUILD_DIR
cp common.config $BUILD_DIR/.config
docker run -d -v $BUILD_DIR:/opt/openwrt -v $PWD/scripts:/opt/scripts --cpus=$(nproc)  -m=2048m --name $IMAGE_NAME $IMAGE_NAME
docker logs -f $IMAGE_NAME