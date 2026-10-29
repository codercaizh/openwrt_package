# 配置迁移说明

设备默认配置必须以当前源码快照生成的 `.packageinfo` 和
`.config-package.in` 为准。旧配置中无法在快照中找到的包或子选项会在
authoritative `make defconfig` 校验阶段报告为错误，不会被静默忽略。

## N60 Pro

N60 Pro 的当前 PassWall 源码仍提供 `Iptables_Transparent_Proxy`、
`Include_Haproxy`、`Include_Xray` 和 `Include_V2ray_Geodata`，因此这些选项
继续保留。

上游当前的 PassWall 配置已经移除旧版本中的 `INCLUDE_tuic_client`、
`INCLUDE_V2ray`、`INCLUDE_Trojan_GO` 和 `INCLUDE_Trojan_Plus`。它们没有
等价的一对一替代项，不能通过改写为其他选项来假装保持原语义；N60 默认
配置已删除这些条目。

Kenzo 当前提供 `luci-theme-argon`，不再提供旧拼写
`luci-theme-argonne`，所以默认配置已迁移为 `luci-theme-argon`。Kenzo
当前没有 `luci-app-pushbot`；可用的相关包是 `luci-app-wechatpush`，但
它不是同一个包，默认配置不会未经用户选择自动替换。编译结果通知由工具
自身的 PushPlus 集成负责。

旧配置中仅用于排除已经不存在的 `luci-app-ssr-plus`、
`luci-app-accesscontrol` 或不存在的 `ddns-scripts` 子选项的行也已移除。

## ARMv8

ARMv8 默认配置与 ImmortalWrt `openwrt-25.12` 快照的生成元数据保持一致。
旧配置中的 `luci-app-filetransfer`、`luci-app-pushbot`、`luci-app-turboacc`、
`luci-theme-bootstrap-mod` 以及旧版 PassWall 子选项已经从当前 feeds 中移除，
因此不再写入默认配置。`luci-app-fileassistant`、`luci-app-amlogic` 和
`luci-app-lucky` 等当前可用包继续保留。

## 2026-10-29 配置精简（360T7 / ARMv8）

- 360T7：继续使用 `qihoo_360t7` 普通布局 profile，升级前必须核对 U-Boot/UBI 布局，
  与 `qihoo_360t7-ubi` 镜像不能混刷。新增 nlbwmon/Watchcat（Watchcat 需用户设定规则），
  显式启用 PassWall SingBox，清理冗余主题、zsh、whereis。
- ARMv8：共享 `armv8.config` 软件包列表；`s905d.config` 增加 Amlogic 管理，
  `vplus.config` 不预装 Amlogic 专用 UI；`#CONFIG_INCLUDE` 限定同目录展开。
- ARM 打包仍用外置 `kernel_stable` 内核及其模块。Packit 解开 rootfs 时删除原内核
  模块，故取消显式 `kmod-fs-*`，保留 Btrfs、Ext4、F2FS 等用户态工具。
  **最终各文件系统是否可用需核对外置内核模块及真机表现。**
- 仅启用 ARM tar.gz rootfs，取消重复 ext4 镜像及对最终 Packit 分区大小无意义的
  `CONFIG_TARGET_ROOTFS_PARTSIZE=720`。移除默认值及可自动加入的库/不常用工具。
- 注意：上游 `unifreq/openwrt_packit` 中一些检查仍使用 `/usr/lib/opkg/info`，
  官方 25.12 已改用 APK；本次保留上游脚本不做盲目替换，后续应在完整镜像中
  验证 NTFS、APK 包数据库、`uname -r` 与已安装 kmod 的一致性。
