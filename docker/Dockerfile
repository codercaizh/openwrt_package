# Reproducible OpenWrt build worker.  The source tree, feed snapshot and
# generated artifacts are mounted at runtime; keeping them out of the image
# makes image rebuilds independent of a moving source branch.
FROM debian:bookworm-slim AS owrt-base

ENV DEBIAN_FRONTEND=noninteractive \
    FORCE_UNSAFE_CONFIGURE=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    HOME=/tmp \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bc bison build-essential bzip2 ca-certificates ccache clang cmake \
      cpio curl device-tree-compiler file flex gawk gettext git gnutls-dev \
      gperf help2man libelf-dev libncurses-dev libssl-dev libtool \
      libz-dev make patch perl pkg-config python3 python3-pip python3-ply \
      python3-setuptools qemu-utils rsync squashfs-tools subversion swig \
      tar unzip util-linux wget xz-utils zip zlib1g-dev zstd jq p7zip-full \
      dosfstools e2fsprogs fdisk parted btrfs-progs uuid-runtime \
 && ln -sf /usr/bin/python3 /usr/bin/python3.9 \
 && ln -sf /usr/bin/python3-config /usr/bin/python3.9-config \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/repo
# Keep the image executable-agnostic so callers can pass ``python3`` (or a
# shell command) directly.  An implicit bash entrypoint would treat the
# Python binary as a script and fail with exit 126.
ENTRYPOINT []
CMD ["python3", "-m", "owrt_builder.cli", "--help"]

# The Web control plane uses the same complete toolchain as the compiler when
# it prepares feeds and runs native OpenWrt metadata generation. Keeping this
# as a stage avoids a second, drifting apt dependency list.
FROM owrt-base AS web
WORKDIR /opt/owrt-builder
COPY requirements.txt pyproject.toml ./
RUN python3 -m pip install --break-system-packages --no-cache-dir -r requirements.txt
COPY owrt_builder ./owrt_builder
COPY configs ./configs
COPY scripts ./scripts
COPY docs ./docs
COPY docker ./docker
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OWRT_DATA_DIR=/var/lib/owrt-builder \
    OWRT_BIND_HOST=0.0.0.0 \
    OWRT_BIND_PORT=8000
VOLUME ["/var/lib/owrt-builder"]
EXPOSE 8000
CMD ["python3", "-m", "uvicorn", "owrt_builder.web:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

# Keep the default image target as the standalone compiler image used by
# BuildEngine and GitHub Actions.
FROM owrt-base AS worker
WORKDIR /workspace/repo
ENTRYPOINT []
CMD ["python3", "-m", "owrt_builder.cli", "--help"]
