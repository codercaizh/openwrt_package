#!/bin/bash
set -e
usage() {
  echo "Complier Usage: ${0} [-c|--configName] [-d|--device] [-r|--rebuild] [-p|--only_package] [-n|--name]" 1>&2
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
        -r|--rebuild)
        REBUILD=1
        shift
        ;;
        -p|--only_package)
        ONLY_PACKAGE=1
        shift
        ;;
        -n|--name)
        NAME=${2}
        shift 2
        ;;
        *)
        usage
        shift
        ;;
    esac
    done
fi
IMAGE_NAME=${NAME:=openwrt_build}
BUILD_DIR=$PWD/openwrt_build_tmp
mkdir -p $BUILD_DIR
[ ! -f "./configs/$CONFIG.config" ] && echo '错误：configs目录中未找到'$CONFIG'.config配置文件' && exit -1
[ `docker ps -a | grep $IMAGE_NAME | wc -l` -eq 0 ] || docker rm -f $IMAGE_NAME
if test -z "$REBUILD";then
   [ `docker image ls $IMAGE_NAME | wc -l` -eq 2 ] || docker build . --tag=$IMAGE_NAME
else
   docker build . --tag=$IMAGE_NAME
   [ -d "$BUILD_DIR" ] && rm -rf $BUILD_DIR
fi
if test -z "$IS_MAKE_MENUCONFIG";then
    echo '当前选择编译的设备：'$DEVICE
    echo '当前选择编译的配置：'$CONFIG
    docker run -d \
    -v $BUILD_DIR/openwrt:/opt/openwrt \
    -v $BUILD_DIR/packit:/opt/openwrt_packit \
    -v $BUILD_DIR/kernel:/opt/kernel \
    -v $PWD/configs:/opt/configs \
    -v $PWD/scripts:/opt/scripts \
    -v $BUILD_DIR/artifact:/opt/artifact \
    --privileged \
    --name $IMAGE_NAME $IMAGE_NAME $DEVICE $CONFIG $ONLY_PACKAGE
    WAIT_COUNT=0
    MAX_WAIT_COUNT=3
    docker logs -f $IMAGE_NAME | while read line
    do
        echo $line
        [[ $line == "编译固件成功"* ]] && break
        [[ $line == "wait for /dev/"* ]] && WAIT_COUNT=$((WAIT_COUNT+1))
        if [ $WAIT_COUNT -gt $MAX_WAIT_COUNT ];then
            echo 'wait for dev timeout,now retry'
            [ `docker ps -a | grep $IMAGE_NAME | wc -l` -eq 0 ] || docker rm -f $IMAGE_NAME
            docker run -d \
            -v $BUILD_DIR/openwrt:/opt/openwrt \
            -v $BUILD_DIR/packit:/opt/openwrt_packit \
            -v $BUILD_DIR/kernel:/opt/kernel \
            -v $PWD/configs:/opt/configs \
            -v $PWD/scripts:/opt/scripts \
            -v $BUILD_DIR/artifact:/opt/artifact \
            --privileged \
            --name $IMAGE_NAME $IMAGE_NAME $DEVICE $CONFIG 1
            docker logs -f $IMAGE_NAME  | while read sub_line
            do
                echo $sub_line
                [[ $sub_line == "编译固件成功"* ]] && break
            done
            break
        fi
    done
    if [ "$(ls -A $BUILD_DIR/artifact)" ]; then
        echo '编译成功'
    else
        echo '编译失败'
        exit -1
    fi
else
    docker run -it \
    -v $BUILD_DIR/openwrt:/opt/openwrt \
    -v $BUILD_DIR/packit:/opt/openwrt_packit \
    -v $BUILD_DIR/kernel:/opt/kernel \
    -v $PWD/configs:/opt/configs \
    -v $PWD/scripts:/opt/scripts \
    -v $BUILD_DIR/artifact:/opt/artifact \
    --privileged \
    --name $IMAGE_NAME $IMAGE_NAME 0 $CONFIG 
fi


