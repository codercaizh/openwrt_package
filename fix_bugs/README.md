# 官方固件问题修复

这里与 `configs/` 同级，集中保存需要随固件编译的修复脚本和固定补丁。
当前用于官方 ImmortalWrt `v25.12.2` 的 `armv8`、`immortalwrt-25.12-mediatek` 源。

源码准备阶段会按文件名顺序执行根目录的 `NNN-名称.sh`，唯一参数为源码树的绝对路径。
CLI、Web、GitHub Actions 共用此入口；全部成功后才发布源码快照。脚本失败时保留上一份快照。
脚本应可重复执行，拒绝不匹配的源码或冲突补丁，不在构建时下载修复内容。
新增脚本和配套文件都放在此目录，并记录上游来源、适用版本和验证方法。

脚本和配套文件的内容参与源码快照指纹；Markdown 文档不参与。
源码树的 `.owrt-fix-bugs` 保存该指纹。修复新增、修改或移除时，刷新设备缓存会清除
`build_dir/target-*` 和 `staging_dir/target-*`，重建内核及目标软件包，保留宿主工具、
交叉工具链和下载缓存；同一修复版本仍可增量编译。此目录也随 Web 镜像分发，并参与 worker 镜像校验。

## 010：FullCone 与 nlbwmon 共用 conntrack 事件

问题：FullCone 与 ctnetlink 的连接跟踪事件订阅相互覆盖或注销，可能导致 nlbwmon 没有统计数据。
这份修复来自 [ImmortalWrt 官方提交 ae5a064](https://github.com/immortalwrt/immortalwrt/commit/ae5a064f132b6476d1094fbb7fe2066db272c7d4)，
仅回移其 Linux 6.12 补丁及 generic config 默认项，补丁内容保持上游原样并校验 SHA256。

`010-fullcone-conntrack-events.sh` 将补丁放进 `target/linux/generic/hack-6.12/`，
由 OpenWrt 在内核 prepare 阶段正式应用。generic config 默认保持关闭；
官方 `fullconenat-nft` 包通过已有的 `KCONFIG` 启用 `CONFIG_NF_CONNTRACK_EVENTS=y` 和
`CONFIG_NF_CONNTRACK_CHAIN_EVENTS=y`。脚本会检查包配方具备这项配置。
它没有修改 HNAT/WED 配置，也没有引入旧 237/Linux 6.6 的 IPTV/HNAT 补丁。

可在单独的官方源码工作树中检查安装过程：

```sh
sh fix_bugs/010-fullcone-conntrack-events.sh /absolute/path/to/immortalwrt
```

项目构建入口会自动执行，无需手工调用。需要重新编译并刷入内核及模块匹配的完整固件，
单独升级 nlbwmon 无法启用此修复。刷机后应检查内核配置的 CHAIN_EVENTS 为 y，确认
FullCone 和 nlbwmon 同时运行时产生新的统计数据，并在路由器重启后复查。
