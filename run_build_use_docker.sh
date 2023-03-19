#!/bin/bash
set -e
usage() {
  echo "Complier Usage: ${0} [-c|--configName] [-d|--device] [-p|--only_package] [-n|--name]" 1>&2
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
CONTAINER_NAME=${NAME:=openwrt_build}
BUILD_DIR=$PWD/openwrt_build_tmp
BUILD_IMAGE=codercai/openwrt_package
[ ! -f "./configs/$CONFIG.config" ] && echo '错误：configs目录中未找到'$CONFIG'.config配置文件' && exit -1
[ `docker ps -a | grep $CONTAINER_NAME | wc -l` -eq 0 ] || docker rm -f $CONTAINER_NAME
mkdir -p $BUILD_DIR
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
    -v $PWD/version.sh:/opt/version.sh \
    --net=host \
    --privileged \
    --name $CONTAINER_NAME $BUILD_IMAGE $DEVICE $CONFIG $ONLY_PACKAGE
    WAIT_COUNT=0
    MAX_WAIT_COUNT=3
    docker logs -f $CONTAINER_NAME | while read line
    do
        echo $line
        [[ $line == "编译固件成功"* ]] && break
        [[ $line == "wait for /dev/"* ]] && WAIT_COUNT=$((WAIT_COUNT+1))
        if [ $WAIT_COUNT -gt $MAX_WAIT_COUNT ];then
            echo 'wait for dev timeout,now retry'
            [ `docker ps -a | grep $CONTAINER_NAME | wc -l` -eq 0 ] || docker rm -f $CONTAINER_NAME
            docker run -d \
            -v $BUILD_DIR/openwrt:/opt/openwrt \
            -v $BUILD_DIR/packit:/opt/openwrt_packit \
            -v $BUILD_DIR/kernel:/opt/kernel \
            -v $PWD/configs:/opt/configs \
            -v $PWD/scripts:/opt/scripts \
            -v $BUILD_DIR/artifact:/opt/artifact \
            -v $PWD/version.sh:/opt/version.sh \
            --net=host \
            --privileged \
            --name $CONTAINER_NAME $BUILD_IMAGE $DEVICE $CONFIG 1
            docker logs -f $CONTAINER_NAME  | while read sub_line
            do
                echo $sub_line
                [[ $sub_line == "编译固件成功"* ]] && break
            done
            break
        fi
    done
    if ls $BUILD_DIR/artifact/$DEVICE/*.7z &> /dev/null; then
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
    -v $PWD/version.sh:/opt/version.sh \
    --net=host \
    --privileged \
    --name $CONTAINER_NAME $BUILD_IMAGE 0 $CONFIG 
fi
