FROM ubuntu:20.04
MAINTAINER codercaizh <545347837@qq.com>
RUN export DEBIAN_FRONTEND=noninteractive && apt update -y && apt full-upgrade -y && apt install -y \
  ack antlr3 asciidoc autoconf automake autopoint binutils bison build-essential \
  bzip2 ccache clang clangd cmake cpio curl device-tree-compiler ecj fastjar flex gawk gettext gcc-multilib \
  g++-multilib git gperf haveged help2man intltool lib32gcc-s1 libc6-dev-i386 libelf-dev libglib2.0-dev \
  libgmp3-dev libltdl-dev libmpc-dev libmpfr-dev libncurses5-dev libncursesw5 libncursesw5-dev libreadline-dev \
  libssl-dev libtool lld lldb lrzsz mkisofs msmtp nano ninja-build p7zip p7zip-full patch pkgconf python2.7 \
  python3 python3-pip python3-ply python-docutils qemu-utils re2c rsync scons squashfs-tools subversion swig \
  texinfo uglifyjs upx-ucl unzip vim wget xmlto xxd zlib1g-dev && apt-get autoremove --purge && apt-get clean
ENV FORCE_UNSAFE_CONFIGURE=1
RUN echo 'echo -e "The Openwrt compilation environment is ready!\n\nYou can enter the container and perform compilation operations by executing the command "docker exec -it $ID /bin/bash".\n\nAlternatively, you can mount the compilation script to /opt/run.sh and then restart the container. The container will automatically run the script.😁" && /usr/bin/tail -f /dev/null' > /opt/run.sh
ENTRYPOINT ["/bin/bash"]
CMD ["/opt/run.sh"]