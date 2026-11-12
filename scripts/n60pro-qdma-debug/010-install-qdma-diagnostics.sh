#!/bin/sh
set -eu

src=${1:?missing OpenWrt source directory}
bundle=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target=$src/target/linux/mediatek/patches-6.18
name=999-net-ethernet-mediatek-capture-n60pro-qdma-tx-timeout.patch

test -d "$target"
test -f "$src/target/linux/mediatek/dts/mt7986a-netcore-n60-pro.dts"
grep -Eq '^KERNEL_PATCHVER[[:space:]]*:=[[:space:]]*6[.]18$' "$src/target/linux/mediatek/Makefile"

if [ -e "$target/$name" ] && ! cmp -s "$bundle/$name" "$target/$name"; then
    echo "conflicting MediaTek diagnostics patch: $target/$name" >&2
    exit 1
fi
install -m 0644 "$bundle/$name" "$target/$name"
install -D -m 0755 "$bundle/router-init.sh" "$src/files/etc/init.d/n60pro-qdma-debug"
install -D -m 0755 "$bundle/router-collect.sh" "$src/files/usr/sbin/n60pro-qdma-debug"
mkdir -p "$src/files/etc/rc.d"
ln -sfn ../init.d/n60pro-qdma-debug "$src/files/etc/rc.d/S94n60pro-qdma-debug"
echo "installed N60 Pro TX diagnostics and persistent collector"
