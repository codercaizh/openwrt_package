#!/bin/sh
# Backport ImmortalWrt ae5a064f132b6476d1094fbb7fe2066db272c7d4 (Linux 6.12 only).
# FullCone and ctnetlink/nlbwmon must share the conntrack event notifier chain.
set -eu

fail() {
    echo "fullcone-conntrack-events: $*" >&2
    exit 1
}

[ "$#" -eq 1 ] || fail "usage: sh $0 /path/to/immortalwrt-source"
source_root=$1
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
patch_name=952-net-conntrack-events-support-multiple-registrant.patch
patch_source=$script_dir/patches/$patch_name
patch_target=$source_root/target/linux/generic/hack-6.12/$patch_name
kernel_config=$source_root/target/linux/generic/config-6.12
fullcone_makefile=$source_root/package/network/utils/fullconenat-nft/Makefile
expected_sha256=d927212c830ad7cea4835e54b7e27b5e41f7011a0d4ed20cb6a4042ab33fa6d4

[ -f "$kernel_config" ] || fail "expected the official Linux 6.12 generic config"
[ -f "$fullcone_makefile" ] || fail "missing official fullconenat-nft package"
grep -q 'CONFIG_NF_CONNTRACK_CHAIN_EVENTS=y' "$fullcone_makefile" ||
    fail "FullCone package does not enable CONFIG_NF_CONNTRACK_CHAIN_EVENTS"
[ -f "$patch_source" ] || fail "missing vendored upstream patch"
actual_sha256=$(sha256sum "$patch_source")
[ "${actual_sha256%% *}" = "$expected_sha256" ] || fail "upstream patch checksum mismatch"
if [ -e "$patch_target" ] || [ -L "$patch_target" ]; then
    [ ! -L "$patch_target" ] && cmp -s "$patch_source" "$patch_target" ||
        fail "existing kernel patch differs; review upstream before replacing it"
fi

# Keep the generic default disabled, exactly as upstream does. The selected
# FullCone package sets both CONFIG_NF_CONNTRACK_EVENTS and CHAIN_EVENTS to y.
if ! grep -Eq '^(# )?CONFIG_NF_CONNTRACK_CHAIN_EVENTS(=| is not set)' "$kernel_config"; then
    grep -Eq '^(# )?CONFIG_NF_CONNTRACK_EVENTS(=| is not set)' "$kernel_config" ||
        fail "missing conntrack event config anchor"
    config_tmp=$(mktemp "$kernel_config.XXXXXX")
    trap 'rm -f "$config_tmp"' EXIT HUP INT TERM
    awk '
        /^(# )?CONFIG_NF_CONNTRACK_EVENTS(=| is not set)/ {
            print "# CONFIG_NF_CONNTRACK_CHAIN_EVENTS is not set"
        }
        { print }
    ' "$kernel_config" > "$config_tmp"
    chmod --reference="$kernel_config" "$config_tmp"
    mv "$config_tmp" "$kernel_config"
    trap - EXIT HUP INT TERM
fi
mkdir -p "$(dirname -- "$patch_target")"
if [ ! -f "$patch_target" ]; then
    cp "$patch_source" "$patch_target"
fi
echo "fullcone-conntrack-events: installed official Linux 6.12 notifier-chain fix"
