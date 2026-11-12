#!/bin/sh
set -eu
umask 077

destination=/root/n60pro-qdma

section() {
    printf '\n$ %s\n' "$*"
    timeout -k 1 1 "$@" 2>&1 || printf '[exit %s]\n' "$?"
}

kernel() {
    date -Is
    cat /proc/uptime /proc/sys/kernel/random/boot_id
    uname -r
    printf '\nKernel TX diagnostics\n'
    dmesg | grep -E 'n60pro-qdma:|NETDEV WATCHDOG|mtk_soc_eth.*(reset|Link is|cycle failed)' | tail -160
}

state() {
    section cat /proc/uptime
    section ubus call network.interface.wan status
    for dev in eth0 eth1 lan1 lan3; do
        section ethtool -S "$dev"
    done
    section cat /proc/interrupts /proc/net/softnet_stat /proc/net/dev
    section ip -s -d link show
    section bridge vlan show
    section bridge -s fdb show br br-lan
    section nft list flowtable inet fw4 ft
    for dev in eth0 eth1 eth1.10 eth1.20; do
        section ethtool -k "$dev"
    done
    for dev in eth0 eth1; do
        section ethtool "$dev"
        section ethtool -a "$dev"
    done
    for queue in /sys/class/net/eth[01]/queues/tx-[0136]; do
        for name in tx_timeout byte_queue_limits/inflight byte_queue_limits/limit; do
            if [ -r "$queue/$name" ]; then
                printf '%s %s\n' "$queue/$name" "$(cat "$queue/$name")"
            fi
        done
    done
    if command -v tc >/dev/null; then
        section tc -s qdisc show
    fi
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
        timeout -k 1 2 "$0" kernel 2>&1 || printf '[kernel collection exit %s]\n' "$?"
    } | head -c 65536 > "$temporary"
    if ! publish "$temporary" "$file"; then
        rm -f "$temporary"
        return 1
    fi

    set -- $(ls -t "$destination"/*.log)
    if [ "$#" -gt 8 ]; then
        shift 8
        rm -f -- "$@"
    fi

    temporary=$(mktemp "$destination/.capture.XXXXXX") || return 1
    {
        cat "$file"
        timeout -k 1 12 "$0" state 2>&1 || printf '[state collection exit %s]\n' "$?"
    } | head -c 131072 > "$temporary"
    if ! publish "$temporary" "$file"; then
        rm -f "$temporary"
        return 1
    fi
}

publish() {
    fsync "$1" && mv "$1" "$2" && fsync "$destination"
}

watch() {
    mkdir -p "$destination"
    boot=$(cat /proc/sys/kernel/random/boot_id)
    logread -f -z 0 -e 'n60pro-qdma:' |
        while IFS= read -r event; do
            case "$event" in
                *'n60pro-qdma: snapshot end'*|*'n60pro-qdma: reset end'*)
                    save_snapshot "$event" || logger -t n60pro-qdma-debug 'failed to save TX diagnostics'
                    ;;
            esac
        done
}

case "${1:-}" in
    watch) watch ;;
    collect) kernel; state ;;
    kernel) kernel ;;
    state) state ;;
    *) exit 2 ;;
esac
