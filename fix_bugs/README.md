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

## 020：MediaTek PPE 流量虚增及连接结束漏计

问题：PPE 表项失效、重绑定或同一软件桶中的其他硬件槽位被错误解绑后，驱动的累计值可能回退。
`mtk_flow_offload_stats()` 的无符号减法再经过 conntrack 的 32 位入参转换，会产生接近 4 GiB
的假增量；过期表项的缓存计数还可能被重复加入。nlbwmon 随后将错误的内核计数累计到数据库。
此外，硬件流删除前没有最后一次读取，会漏掉上次轮询后的尾段流量。

`020-mediatek-ppe-accounting.sh` 固定采用
[OpenWrt PR #26107](https://github.com/openwrt/openwrt/pull/26107) 的
`3be1a0c4619d433c1ee8aa95d8de9459e6587b74` 版本，并保留
[PR #26081](https://github.com/openwrt/openwrt/pull/26081) 的尾段计数修复。
截至 2026-10-29，两份 PR 均尚未合并。这是明确固定的上游候选修复回移，不追踪远程最新内容。

脚本校验官方 Linux 6.12 目标、736-03/04/05 基线补丁和两份修复的 SHA256，全部通过后才复制：

- `pending-6.12/736-06-net-ethernet-mediatek-preserve-ppe-flow-accounting.patch`：
  PPE 部分保持上游文件原样，保留其他槽位的绑定、解绑前保存计数且只保存一次、
  在锁内初始化统计快照，并避免递减累计值产生假增量。
- `pending-6.12/708-netfilter-flowtable-fetch-final-hardware-counters.patch`：
  将连接删除前补收硬件计数的逻辑适配到 Linux 6.12，保留该版本的 `NF_FLOW_HW_DEAD`
  状态转换。上游 6.18 版本不能直接替换此文件。

两份补丁随当前两个官方 Linux 6.12 源安装；PPE 驱动部分只在选中相应 MediaTek 驱动时编译。
新增修复自动改变源码/设备缓存指纹，已有未修复内核及目标软件包需要重建。
重复执行时接受相同补丁，遇到基线变化、文件篡改或冲突会停止准备并保留原有源码快照。

测试通过实际补丁修改 GPL 内核函数片段，再编译并运行原版/修复版函数的硬件状态桩，覆盖
4 GiB 虚增、同一过期计数多次轮询、正常增量和硬件删除前尾段计数，检查 `NF_FLOW_HW_DEAD` 状态保留。
运行：

```sh
python3 -m pytest tests/test_ppe_accounting.py tests/test_local_patches.py
```

需要 C 编译器、git 和项目测试依赖。这些回归检查不替代 MMIO、并发及真实转发验证。
刷入完整固件后，应在硬件卸载开启时以已知大小的实际传输对账，并覆盖空闲重绑定、
连接立即关闭和长期混合流量。确认源头正确后再处理先前已污染的流量历史；构建脚本不删除路由器统计。
