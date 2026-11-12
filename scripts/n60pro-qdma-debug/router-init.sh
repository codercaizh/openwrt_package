#!/bin/sh /etc/rc.common

START=94
USE_PROCD=1

start_service() {
    grep -qx 'netcore,n60-pro' /tmp/sysinfo/board_name || return 0
    procd_open_instance
    procd_set_param command /usr/sbin/n60pro-qdma-debug watch
    procd_set_param respawn
    procd_set_param stderr 1
    procd_close_instance
}
