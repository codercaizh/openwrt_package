## 前言
本项目旨在提供便捷的方式用于编译各种矿渣盒子和外贸机顶盒的openwrt固件，支持使用github action流水线编译，以及在本地使用docker容器进行编译。
项目亮点在于本地编译时无需安装和解决各种复杂的环境依赖软件依赖，直接构建一个镜像和容器进行编译，编译完成后可以直接使用docker rm删除镜像和容器，即用即走，不对环境造成任何污染。

## 目录及脚本说明

| 目录或文件名             | 说明                  |
|------------------------|----------------------|
|.github/workflows|使用Github action编译构建命令，一般不需要改动
|scripts|用于存放编译过程中用到的自定义脚本，比如需要希望将某个插件的版本进行更新，或者是加入新的feeds源，都可以在里面进行自定义
|scripts/build_with_docker.sh|本地编译时容器启动后执行的脚本，一般无需更改
|common.config|包含常用插件的编译配置，包括一些必备的你懂的插件，如果有加减插件的需求可以改这个文件
|Dockerfile|docker镜像文件，用于本地构建一个编译环境镜像
|run_build_use_docker.sh|本地编译的执行脚本
|simple.config|仅包含最最基本的能够让盒子启动和写入到emmc的编译配置，建议可以基于此配置文件去创建你的自定义配置文件

## 支持的型号列表
`vplus`

`beikeyun`

`l1pro`

`rock5b`

`h88k` `h68k`

`r66s` `r68s`

`e25`

`s905` `s905d` `s905x2` `s905x3` `s912` `s922x` `s922x-n2`

`qemu`

## 编译说明
本项目使用的是[coolsnowwolf/lede](https://github.com/coolsnowwolf/lede) openwrt库源码、[flippy的打包脚本](https://github.com/unifreq/openwrt_packit)、[breakings维护的内核](https://github.com/breakings/OpenWrt) 感谢以上几位大佬。
注：每次全新编译打包默认使用最新的openwrt代码与内核

## 编译方法
### Github Action编译
Fork本项目，然后在Action中启动任务，在界面中可以选择盒子型号和配置，然后进行编译。由于github限制单任务最大时长只能6个小时，而一旦编译配置文件包含了如Node这些大型软件，编译很容易超时，推荐使用下面的**本地Docker编译**方法

### 本地Docker编译（推荐）
本地编译命令
```bash
./run_build_use_docker.sh -c 配置文件 -d 设备型号 [-p] [-r]
```
其中配置文件参数无需填写.config后缀，只要填文件名，例如：common；设备型号参考上面表格。
-p：（可选）表示在已编译出底包的前提下仅进行打包，不做编译操作，节约时间。适用于需要使用同个底包打包出多种盒子镜像的场景
-r：（可选）表示从头开始编译。使用该参数会删除之前所有的编译产物、源码、内核，重新拉取最新的源码和内核进行编译，实际效果和首次编译一样
具体用法：使用虚拟机或者在你的云服务器等搭一个linux系统（推荐Ubuntu20+，硬盘空闲空间不小于15G），安装好docker后，clone本项目，然后执行命令，如
```bash
./run_build_use_docker.sh -c common -d s905d
```
上述表示使用common.config的配置编译出N1盒子的openwrt固件。
### 如何自制配置文件
使用虚拟机或者在你的云服务器等搭一个linux系统，安装好docker后，clone本项目，在目录下执行
```bash
./run_build_use_docker.sh menuconfig custom
```
即可进入固件配置界面，配置完成，会在目录下生成custom.config配置，再执行类似如：
```bash
./run_build_use_docker.sh -c custom -d s905d
```
则会使用刚刚生成的自定义配置文件进行编译
