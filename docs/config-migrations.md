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
