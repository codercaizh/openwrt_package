FROM ubuntu:22.10
MAINTAINER codercaizh <545347837@qq.com>
EXPOSE 22
ENV DEBIAN_FRONTEND=noninteractive
# RUN sed -i "s@http://.*archive.ubuntu.com@http://mirrors.tuna.tsinghua.edu.cn@g" /etc/apt/sources.list && sed -i "s@http://.*security.ubuntu.com@http://mirrors.tuna.tsinghua.edu.cn@g" /etc/apt/sources.list
# openwrt固件编译依赖
RUN apt-get update && apt-get install -y fdisk btrfs-progs parted uuid-runtime dosfstools build-essential asciidoc binutils bzip2 gawk gettext git libncurses5-dev libz-dev patch python3.10 python2.7 unzip zlib1g-dev lib32gcc-s1 libc6-dev-i386 subversion flex uglifyjs git-core gcc-multilib p7zip p7zip-full msmtp libssl-dev texinfo libglib2.0-dev xmlto qemu-utils upx libelf-dev autoconf automake libtool autopoint device-tree-compiler g++-multilib antlr3 gperf wget curl swig rsync && apt-get autoremove --purge && apt-get clean 
ENV FORCE_UNSAFE_CONFIGURE=1
COPY ./scripts /opt/scripts
ENTRYPOINT ["/bin/bash", "/opt/scripts/build_with_docker.sh"]
