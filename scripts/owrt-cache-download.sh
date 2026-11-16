#!/bin/sh
set -eu
archive=$1
expected=$2
temporary=$3
mkdir -p "$temporary"
(
    flock -x 9
    [ -f "$archive" ] || exit 0
    case "${#expected}" in
        64) actual=$(sha256sum -- "$archive") ;;
        32) actual=$(md5sum -- "$archive") ;;
        *) actual="" ;;
    esac
    if [ -z "$actual" ] || [ "${actual%% *}" != "$expected" ]; then
        rm -f -- "$archive"
        printf '%s\n' rejected
    fi
) 9>"$temporary/.owrt-cache-download.lock"
