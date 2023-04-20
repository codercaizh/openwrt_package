#!/bin/bash
set -e
BUILD_IMAGE=codercai/openwrt_package:2.0
# 构建运行参数
function assembleBuildArgs() {
    BUILD_DIR_PATH=$1
    BUILD_CONTAINER_NAME=$2
    BUILD_ARGS="-v $BUILD_DIR_PATH/openwrt:/opt/openwrt "
    BUILD_ARGS+="-v $BUILD_DIR_PATH/packit:/opt/openwrt_packit "
    BUILD_ARGS+="-v $BUILD_DIR_PATH/kernel:/opt/kernel "
    BUILD_ARGS+="-v $PWD/configs:/opt/configs "
    BUILD_ARGS+="-v $PWD/scripts:/opt/scripts "
    BUILD_ARGS+="-v $BUILD_DIR_PATH/artifact:/opt/artifact "
    BUILD_ARGS+="-v $PWD/version.sh:/opt/version.sh "
    BUILD_ARGS+="--net=host "
    BUILD_ARGS+="--privileged "
    BUILD_ARGS+="--name $BUILD_CONTAINER_NAME "
}

function usage() {
  echo "Complier Usage: ${0} [-c|--configName] [-d|--device] [-p|--only_package] [-n|--name] [-o|--output_path]" 1>&2
  echo "Make menuconfig Usage: menuconfig [configPrefixName]" 1>&2
  echo "Only run container Usage: go [workdir]" 1>&2
  echo "Only download openwrt Usage: download [workdir]" 1>&2
  exit 1 
}
if [ $# -eq 0 ];then
    usage
fi
if [ "$1" = "menuconfig" ];then
    CONTAINER_NAME="openwrt_menuconfig"
    assembleBuildArgs ${3:-"$PWD/openwrt_build_tmp"} $CONTAINER_NAME
    [ `docker ps -a | grep $CONTAINER_NAME | wc -l` -eq 0 ] || docker rm -f $CONTAINER_NAME
    docker run -it --rm $BUILD_ARGS $BUILD_IMAGE /opt/scripts/build_with_docker.sh $1 0 $2
    exit 0
fi
if [ "$1" = "go" ];then
    # 没有带参数则把当前目录挂载进去，否则挂载指定的目录
    MOUNT_PATH=${2:-$PWD}
    docker run -it --rm --net=host -v $MOUNT_PATH:/mount -w /mount --privileged $BUILD_IMAGE /bin/bash -c "echo -e 'now in container';/bin/bash"
    exit 0
fi
if [ "$1" = "download" ];then
    CONTAINER_NAME="openwrt_download"
    assembleBuildArgs ${3:-"$PWD/openwrt_build_tmp"} $CONTAINER_NAME
    [ `docker ps -a | grep $CONTAINER_NAME | wc -l` -eq 0 ] || docker rm -f $CONTAINER_NAME
    docker run -d $BUILD_ARGS $BUILD_IMAGE /opt/scripts/build_with_docker.sh $1 0 $2
    docker logs -f openwrt_download
    exit 0
fi
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
        -o|--output_path)
        OUTPUT_PATH=${2}
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
CONTAINER_NAME=${NAME:-"openwrt_build"}
BUILD_DIR=${OUTPUT_PATH:="$PWD/openwrt_build_tmp"} 
[ ! -f "./configs/$CONFIG.config" ] && echo '错误：configs目录中未找到'$CONFIG'.config配置文件' && exit -1
[ `docker ps -a | grep $CONTAINER_NAME | wc -l` -eq 0 ] || docker rm -f $CONTAINER_NAME
mkdir -p $BUILD_DIR
echo '当前选择编译的设备：'$DEVICE
echo '当前选择编译的配置：'$CONFIG
assembleBuildArgs $BUILD_DIR $CONTAINER_NAME
[ "$ONLY_PACKAGE" == "1" ] && CMD="package" || CMD="compile"
docker run -d $BUILD_ARGS $BUILD_IMAGE /opt/scripts/build_with_docker.sh $CMD $DEVICE $CONFIG
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
        docker run -d $BUILD_ARGS $BUILD_IMAGE /opt/scripts/build_with_docker.sh package $DEVICE $CONFIG
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