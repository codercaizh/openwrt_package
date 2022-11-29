#!/bin/bash
set -e
usage() {
  echo "Complier Usage: ${0} [-c|--configName] [-d|--device]" 1>&2
  echo "Make menuconfig Usage: menuconfig" 1>&2
  exit 1 
}

if [ $# -eq 0 ];then
    usage
fi

if [ "$1" = "menuconfig" ];then
    IS_MAKE_MENUCONFIG=1
    CONFIG=${2:-common}
else
    while [[ $# -gt 0 ]];do
    key=${1}
    case ${key} in
        -c|--configName)
        CONFIG=${2}
        shift 2
        ;;
        -d|--device)
        DEVICE=${2}
        shift 2
        ;;
        -f|--force)
        FORCE=1
        shift 2
        ;;
        *)
        usage
        shift
        ;;
    esac
    done
fi
IMAGE_NAME=openwrt_build
BUILD_DIR=$PWD/openwrt_build_tmp
mkdir -p $BUILD_DIR
[ `docker ps -a | grep $IMAGE_NAME | wc -l` -eq 0 ] || docker rm -f $IMAGE_NAME
if test -z "$FORCE";then
   [ `docker image ls $IMAGE_NAME | wc -l` -eq 2 ] || docker build . --tag=$IMAGE_NAME
else
   docker build . --tag=$IMAGE_NAME
fi
[ -d "$BUILD_DIR/openwrt" ] || mkdir -p $BUILD_DIR/openwrt
cp $CONFIG.config $BUILD_DIR/openwrt/.config
if test -z "$IS_MAKE_MENUCONFIG";then
    echo '当前选择编译的设备：'$DEVICE
    echo '当前选择编译的配置：'$CONFIG
    docker run -d \
    -v $BUILD_DIR/openwrt:/opt/openwrt \
    -v $BUILD_DIR/packit:/opt/openwrt_packit \
    -v $PWD/scripts:/opt/scripts \
    -v $BUILD_DIR/artifact:/opt/artifact \
    --privileged \
    --name $IMAGE_NAME $IMAGE_NAME $DEVICE
    docker logs -f $IMAGE_NAME
else
    docker run -it \
    -v $BUILD_DIR/openwrt:/opt/openwrt \
    -v $BUILD_DIR/packit:/opt/openwrt_packit \
    -v $PWD/scripts:/opt/scripts \
    -v $BUILD_DIR/artifact:/opt/artifact \
    --privileged \
    --name $IMAGE_NAME $IMAGE_NAME
    docker cp $IMAGE_NAME:/opt/openwrt/.config ./$CONFIG.config
fi


