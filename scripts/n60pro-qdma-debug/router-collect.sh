#!/bin/sh
set -eu
umask 077

destination=/root/n60pro-qdma

section() {
    printf '\n$ %s\n' "$*"
    timeout -k 1 1 "$@" 2>&1 || printf '[exit %s]\n' "$?"
}

collect() {
    date -Is
    cat /proc/uptime /proc/sys/kernel/random/boot_id
    uname -r
    printf '\nKernel TX diagnostics\n'
    dmesg | grep -E 'n60pro-qdma:|NETDEV WATCHDOG|mtk_soc_eth.*(reset|Link is)' | tail -120
    section cat /proc/interrupts /proc/net/softnet_stat /proc/net/dev
    section ip -s -d link show
    section bridge -s fdb show br br-lan
    section ubus call network.interface.wan status
    section nft list flowtable inet fw4 ft
    for dev in eth0 eth1 eth1.10 eth1.20; do
        section ethtool -k "$dev"
    done
    for dev in eth0 eth1; do
        section ethtool -S "$dev"
    done
    printf '\nRecent system log\n'
    timeout -k 1 2 logread -l 150 |
        sed -E 's/(CHAP (Challenge|Response)).*/\1 [redacted]/I; s/((password|passwd|secret|token)[=:])[[:graph:]]+/\1[redacted]/Ig'
}

save_snapshot() {
    local file="$destination/$boot-first.log" temporary
    [ ! -f "$file" ] || file="$destination/$boot-latest.log"
    temporary=$(mktemp "$destination/.capture.XXXXXX") || return 1
    {
        printf '%s\n' "$1"
        timeout -k 1 12 "$0" collect 2>&1 || printf '[collection exit %s]\n' "$?"
    } | head -c 131072 > "$temporary"
    if fsync "$temporary" && mv "$temporary" "$file" && fsync "$destination"; then
        set -- $(ls -t "$destination"/*.log)
        if [ "$#" -gt 8 ]; then
            shift 8
            rm -f -- "$@"
        fi
    else
        rm -f "$temporary"
        return 1
    fi
}

watch() {
    mkdir -p "$destination"
    boot=$(cat /proc/sys/kernel/random/boot_id)
    logread -f -z 0 -e 'n60pro-qdma: snapshot end' |
        while IFS= read -r event; do
            save_snapshot "$event" || logger -t n60pro-qdma-debug 'failed to save TX diagnostics'
        done
}

case "${1:-}" in
    watch) watch ;;
    collect) collect ;;
    *) exit 2 ;;
esac
