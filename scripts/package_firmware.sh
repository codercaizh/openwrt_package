#!/bin/bash
package_firmware(){
    packit_dir=$1
    rootfs_tar_path=$2
    device=$3
    whoami=$4
    if [ ! -d "$packit_dir/.git" ]; then
        echo '未找到打包源码，正在检出源码'
        rm -rf $packit_dir/*
        git clone https://github.com/unifreq/openwrt_packit --depth=1 $packit_dir
    fi
    rm -rf $packit_dir/*rootfs.tar.gz
    rm -rf ./output/*
    cp $rootfs_tar_path $packit_dir/
    cp $whoami ./
    cd $packit_dir
    rm -rf tmp
    echo '打包源码与底包准备完毕'
    # Set the default packaging script
    SCRIPT_VPLUS_FILE="mk_h6_vplus.sh"
    SCRIPT_BEIKEYUN_FILE="mk_rk3328_beikeyun.sh"
    SCRIPT_L1PRO_FILE="mk_rk3328_l1pro.sh"
    SCRIPT_R66S_FILE="mk_rk3568_r66s.sh"
    SCRIPT_R68S_FILE="mk_rk3568_r68s.sh"
    SCRIPT_H66K_FILE="mk_rk3568_h66k.sh"
    SCRIPT_H68K_FILE="mk_rk3568_h68k.sh"
    SCRIPT_E25_FILE="mk_rk3568_e25.sh"
    SCRIPT_ROCK5B_FILE="mk_rk3588_rock5b.sh"
    SCRIPT_H88K_FILE="mk_rk3588_h88k.sh"
    SCRIPT_S905_FILE="mk_s905_mxqpro+.sh"
    SCRIPT_S905D_FILE="mk_s905d_n1.sh"
    SCRIPT_S905X2_FILE="mk_s905x2_x96max.sh"
    SCRIPT_S905X3_FILE="mk_s905x3_multi.sh"
    SCRIPT_S912_FILE="mk_s912_zyxq.sh"
    SCRIPT_S922X_FILE="mk_s922x_gtking.sh"
    SCRIPT_S922X_N2_FILE="mk_s922x_odroid-n2.sh"
    SCRIPT_QEMU_FILE="mk_qemu-aarch64_img.sh"
    SCRIPT_DIY_FILE="mk_diy.sh"
    [[ -n "${SCRIPT_VPLUS}" ]] || SCRIPT_VPLUS="${SCRIPT_VPLUS_FILE}"
    [[ -n "${SCRIPT_BEIKEYUN}" ]] || SCRIPT_BEIKEYUN="${SCRIPT_BEIKEYUN_FILE}"
    [[ -n "${SCRIPT_L1PRO}" ]] || SCRIPT_L1PRO="${SCRIPT_L1PRO_FILE}"
    [[ -n "${SCRIPT_R66S}" ]] || SCRIPT_R66S="${SCRIPT_R66S_FILE}"
    [[ -n "${SCRIPT_R68S}" ]] || SCRIPT_R68S="${SCRIPT_R68S_FILE}"
    [[ -n "${SCRIPT_H66K}" ]] || SCRIPT_H66K="${SCRIPT_H66K_FILE}"
    [[ -n "${SCRIPT_H68K}" ]] || SCRIPT_H68K="${SCRIPT_H68K_FILE}"
    [[ -n "${SCRIPT_E25}" ]] || SCRIPT_E25="${SCRIPT_E25_FILE}"
    [[ -n "${SCRIPT_ROCK5B}" ]] || SCRIPT_ROCK5B="${SCRIPT_ROCK5B_FILE}"
    [[ -n "${SCRIPT_H88K}" ]] || SCRIPT_H88K="${SCRIPT_H88K_FILE}"
    [[ -n "${SCRIPT_S905}" ]] || SCRIPT_S905="${SCRIPT_S905_FILE}"
    [[ -n "${SCRIPT_S905D}" ]] || SCRIPT_S905D="${SCRIPT_S905D_FILE}"
    [[ -n "${SCRIPT_S905X2}" ]] || SCRIPT_S905X2="${SCRIPT_S905X2_FILE}"
    [[ -n "${SCRIPT_S905X3}" ]] || SCRIPT_S905X3="${SCRIPT_S905X3_FILE}"
    [[ -n "${SCRIPT_S912}" ]] || SCRIPT_S912="${SCRIPT_S912_FILE}"
    [[ -n "${SCRIPT_S922X}" ]] || SCRIPT_S922X="${SCRIPT_S922X_FILE}"
    [[ -n "${SCRIPT_S922X_N2}" ]] || SCRIPT_S922X_N2="${SCRIPT_S922X_N2_FILE}"
    [[ -n "${SCRIPT_QEMU}" ]] || SCRIPT_QEMU="${SCRIPT_QEMU_FILE}"
    [[ -n "${SCRIPT_DIY}" ]] || SCRIPT_DIY="${SCRIPT_DIY_FILE}"
    case $device in
        vplus)    [[ -f "${SCRIPT_VPLUS}" ]] && ./${SCRIPT_VPLUS} ;;
        beikeyun) [[ -f "${SCRIPT_BEIKEYUN}" ]] && ./${SCRIPT_BEIKEYUN} ;;
        l1pro)    [[ -f "${SCRIPT_L1PRO}" ]] && ./${SCRIPT_L1PRO} ;;
        r66s)     [[ -f "${SCRIPT_R66S}" ]] && ./${SCRIPT_R66S} ;;
        r68s)     [[ -f "${SCRIPT_R68S}" ]] && ./${SCRIPT_R68S} ;;
        h66k)     [[ -f "${SCRIPT_H66K}" ]] && ./${SCRIPT_H66K} ;;
        h68k)     [[ -f "${SCRIPT_H68K}" ]] && ./${SCRIPT_H68K} ;;
        rock5b)   [[ -f "${SCRIPT_ROCK5B}" ]] && ./${SCRIPT_ROCK5B} ;;
        h88k)     [[ -f "${SCRIPT_H88K}" ]] && ./${SCRIPT_H88K} ;;
        e25)      [[ -f "${SCRIPT_E25}" ]] && ./${SCRIPT_E25} ;;
        s905)     [[ -f "${SCRIPT_S905}" ]] && ./${SCRIPT_S905} ;;
        s905d)    [[ -f "${SCRIPT_S905D}" ]] && ./${SCRIPT_S905D} ;;
        s905x2)   [[ -f "${SCRIPT_S905X2}" ]] && ./${SCRIPT_S905X2} ;;
        s905x3)   [[ -f "${SCRIPT_S905X3}" ]] && ./${SCRIPT_S905X3} ;;
        s912)     [[ -f "${SCRIPT_S912}" ]] && ./${SCRIPT_S912} ;;
        s922x)    [[ -f "${SCRIPT_S922X}" ]] && ./${SCRIPT_S922X} ;;
        s922x-n2) [[ -f "${SCRIPT_S922X_N2}" ]] && ./${SCRIPT_S922X_N2} ;;
        qemu)     [[ -f "${SCRIPT_QEMU}" ]] && ./${SCRIPT_QEMU} ;;
        diy)      [[ -f "${SCRIPT_DIY}" ]] && ./${SCRIPT_DIY} ;;
        *)        echo -e "找不到合适的打包脚本" && continue ;;
    esac
}
