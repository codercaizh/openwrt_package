#!/bin/sh
# Port of OpenWrt PR 26107 (3be1a0c4619d433c1ee8aa95d8de9459e6587b74).
set -eu

fail() {
    echo "mediatek-ppe-accounting: $*" >&2
    exit 1
}

[ "$#" -eq 1 ] || fail "usage: sh $0 /path/to/immortalwrt-source"
source_root=$1
bundle=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target=$source_root/target/linux/mediatek
patchdir=$target/patches-6.18

[ -d "$patchdir" ] && [ ! -L "$patchdir" ] &&
    grep -Eq '^KERNEL_PATCHVER[[:space:]]*:=[[:space:]]*6[.]18[[:space:]]*$' "$target/Makefile" ||
    fail "expected the official MediaTek Linux 6.18 target"

for patch in "$bundle"/patches/*.patch; do
    [ -f "$patch" ] || fail "missing patch: $patch"
    destination=$patchdir/${patch##*/}
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        [ ! -L "$destination" ] && cmp -s "$patch" "$destination" ||
            fail "existing kernel patch differs: $destination"
    fi
done
for patch in "$bundle"/patches/*.patch; do
    destination=$patchdir/${patch##*/}
    [ -f "$destination" ] || cp "$patch" "$destination"
done
