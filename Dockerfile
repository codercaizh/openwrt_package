FROM debian:11
MAINTAINER codercaizh <545347837@qq.com>
RUN export DEBIAN_FRONTEND=noninteractive && apt update -y && apt full-upgrade -y && apt install -y ack antlr3 asciidoc autoconf automake autopoint binutils bison build-essential \
bzip2 ccache clang cmake cpio curl device-tree-compiler ecj fastjar flex gawk gettext gcc-multilib \
g++-multilib git gnutls-dev gperf haveged help2man intltool lib32gcc-s1 libc6-dev-i386 libelf-dev \
libglib2.0-dev libgmp3-dev libltdl-dev libmpc-dev libmpfr-dev libncurses-dev libpython3-dev \
libreadline-dev libssl-dev libtool libyaml-dev libz-dev lld llvm lrzsz mkisofs msmtp nano \
ninja-build p7zip p7zip-full patch pkgconf python3 python3-pip python3-ply python3-docutils \
python3-pyelftools qemu-utils re2c rsync scons squashfs-tools subversion swig texinfo uglifyjs \
upx-ucl unzip vim wget xmlto xxd zlib1g-dev zstd && apt-get autoremove --purge && apt-get clean
ENV FORCE_UNSAFE_CONFIGURE=1
RUN echo 'echo -e "The Openwrt compilation environment is ready!\n\nYou can enter the container and perform compilation operations by executing the command "docker exec -it $ID /bin/bash".\n\nAlternatively, you can mount the compilation script to /opt/run.sh and then restart the container. The container will automatically run the script.😁" && /usr/bin/tail -f /dev/null' > /opt/run.sh
ENTRYPOINT ["/bin/bash"]
CMD ["/opt/run.sh"]