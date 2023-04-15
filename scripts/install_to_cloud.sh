#!/bin/bash
# 本脚本可用于将编译出来的OP固件安装到VPS中，支持腾讯云、阿里云的云服务器
# 使用方法，将编译出来的openwrt-x86-64-generic-squashfs-combined.img.gz文件放到和本脚本相同路径下
# 使用root执行 ./install_to_cloud.sh后在控制台用VNC登录，屏幕出现一堆加载信息后，按回车就能进去交互，执行以下步骤就能正常使用
# vi /etc/ssh/sshd_config 把#PermitRootLogin XXX 改为PermitRootLogin yes，允许root登录
# vi /etc/config/network 把lan口的proto改为dhcp，并把下面ipaddr和netmask选项删除
# 编辑完成后执行reboot使其生效
IMAGE=/boot/op.img.gz
SOURCE_IMAGE=openwrt-x86-64-generic-squashfs-combined.img.gz
GRUB_FILES="/boot/grub/grub.cfg /boot/grub2/grub.cfg /boot/boot/grub.cfg /boot/grub.cfg"
INSTALL_OP_SCRIPT=/boot/install_op.sh
copy() {
    [ -f "$SOURCE_IMAGE" ] && cp $SOURCE_IMAGE $IMAGE || (echo "找不到$SOURCE_IMAGE" && exit 1)
}
create_install_script() {
    cat <<-EOF >$INSTALL_OP_SCRIPT
#!/bin/bash

IMAGE=$IMAGE

main() {
    mkdir -p /run/tmp
    mount -o remount,ro /
    mount -t tmpfs -o size=512M tmpfs /run/tmp || return 1
    cp -a $IMAGE /run/tmp/op.img.gz || return 1
    zcat /run/tmp/op.img.gz | dd of=/dev/vda bs=1M
    echo "install success, rebooting..."
    return 0
}
main && exit 0
echo "install failed!" >&2
exec /bin/bash -i
EOF
    chmod 755 $INSTALL_OP_SCRIPT || exit 1
}
modify_grub() {
    grep -sFq "init=$INSTALL_OP_SCRIPT" $GRUB_FILES && return 0
    chmod 644 $GRUB_FILES 2>/dev/null
    sed -s -i -Ee 's,^([ \t]*linux[ \t]+.*$),\1 init='"$INSTALL_OP_SCRIPT"',g' $GRUB_FILES
    if ! grep -qF $INSTALL_OP_SCRIPT $GRUB_FILES; then
        echo "不支持的操作系统，请先将系统换成 Debian 或者 Ubuntu 再试." >&2
        echo "Unsupported operating system, please install Debian or Ubuntu first." >&2
        exit 1
    fi
}

copy && create_install_script || exit 1
modify_grub && echo 'setup success, rebooting...'
sleep 5
reboot