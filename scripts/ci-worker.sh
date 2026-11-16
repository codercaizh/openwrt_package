#!/usr/bin/env bash
set -euo pipefail

mkdir -p .owrt
user=(--user "$(id -u):$(id -g)")
case "$OWRT_DEVICE" in
  s905d|n1|vplus|h6-vplus) user=(--privileged --volume /dev:/dev) ;;
esac

docker run --rm --init "${user[@]}" \
  --env OWRT_WORKSPACE=/workspace/work --env OWRT_CLI_CONTAINER=1 --env OWRT_CI_CACHE=1 \
  --volume "$PWD:/workspace/repo:ro" --volume "$PWD/.owrt:/workspace/work" \
  --workdir /workspace/repo owrt-builder:ci python3 -m "$@"
