#!/bin/sh
# OpenWrt PR 26107, pinned at 3be1a0c4619d433c1ee8aa95d8de9459e6587b74.
# Prevent 4 GiB phantom increments and repeated/omitted PPE flow accounting.
set -eu

fail() {
    echo "mediatek-ppe-accounting: $*" >&2
    exit 1
}

check_sha256() {
    [ -f "$1" ] && [ ! -L "$1" ] || fail "missing or unsafe patch: $1"
    actual_sha256=$(sha256sum "$1")
    [ "${actual_sha256%% *}" = "$2" ] || fail "patch checksum mismatch: $1"
}

[ "$#" -eq 1 ] || fail "usage: sh $0 /path/to/immortalwrt-source"
source_root=$1
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
pending=$source_root/target/linux/generic/pending-6.12
kernel_config=$source_root/target/linux/generic/config-6.12
target_makefile=$source_root/target/linux/mediatek/Makefile
netfilter_patch=708-netfilter-flowtable-fetch-final-hardware-counters.patch
ppe_patch=736-06-net-ethernet-mediatek-preserve-ppe-flow-accounting.patch

[ -f "$kernel_config" ] || fail "expected the official Linux 6.12 generic config"
[ -f "$target_makefile" ] || fail "missing official MediaTek target"
grep -Eq '^KERNEL_PATCHVER[[:space:]]*:=[[:space:]]*6[.]12[[:space:]]*$' "$target_makefile" ||
    fail "expected the official MediaTek Linux 6.12 target"

# Reject changed baselines rather than silently applying a fix reviewed against
# a different kernel patch stack. All checks complete before either copy.
check_sha256 "$pending/736-03-net-ethernet-mtk_eth_soc-improve-keeping-track-of-of.patch" \
    a59f0fb96ab3ad38594444ab796df438cf698524bc0f137765a7516aec474808
check_sha256 "$pending/736-04-net-ethernet-mediatek-fix-ppe-flow-accounting-for-L2.patch" \
    b95c79b8b6c0e30a1d6a495a4b8bc330a4cb4c34b2bb97e1734bb95d56800307
check_sha256 "$pending/736-05-net-ethernet-mtk_eth_soc-zero-initialize-PPE-flow-ta.patch" \
    5782957cb404115ab163b60ce4c0e2268e29a6de82cacb7c06d76f83c68243ba
check_sha256 "$script_dir/patches/$netfilter_patch" \
    7e4c98e29219941e0055480a618c7307b7962a5c53801e4c31c186427ec0479d
check_sha256 "$script_dir/patches/$ppe_patch" \
    ff70527bdb485b1becbf32bd7b889367b5555be72f1ef7ce1c8664ad79484877

for name in "$netfilter_patch" "$ppe_patch"; do
    target=$pending/$name
    if [ -e "$target" ] || [ -L "$target" ]; then
        [ ! -L "$target" ] && cmp -s "$script_dir/patches/$name" "$target" ||
            fail "existing kernel patch differs; review upstream before replacing it: $target"
    fi
done
for name in "$netfilter_patch" "$ppe_patch"; do
    if [ ! -f "$pending/$name" ]; then
        cp "$script_dir/patches/$name" "$pending/$name"
    fi
done
echo "mediatek-ppe-accounting: installed Linux 6.12 PPE and final-counter fixes"
