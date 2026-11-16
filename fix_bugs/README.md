# 当前分支固件问题修复

当前 `immortalwrt-master` 的七种设备均使用 ImmortalWrt 官方 `master`。
本目录只保留当前仍需本地维护的 MediaTek Linux 6.18 PPE 计数修复：

- N60 Pro 和 360T7 使用相同的 MediaTek/Filogic 上游源码快照，共用同一补丁 bundle。
- S905D、V-Plus、R3G、K2P、K2P-AP 不应用本地内核补丁。
- FullCone 与 ctnetlink/nlbwmon 的 conntrack notifier chain 多订阅支持由当前官方 master 提供，不再回移旧版实现。
- 本分支不包含 Linux 6.12 的补丁、安装脚本或测试夹具。25.12.2 的旧实现只存在于独立归档分支，不由本分支构建。

## MediaTek Linux 6.18 PPE 计数修复

`mediatek-6.18/` 将 [OpenWrt PR #26107](https://github.com/openwrt/openwrt/pull/26107)
的 `3be1a0c4619d433c1ee8aa95d8de9459e6587b74` 适配到官方 ImmortalWrt
`4765b657625ec5bbda74b44400465fe043802b30`（Linux 6.18.60）。

修复处理 PPE 表项重绑定、桥接子流解绑和统计快照并发问题：解绑或重绑定前先读取并保存硬件计数，
避免同一缓存计数重复累加；锁内初始化统计快照；避免累计计数回退被当作无符号流量增量。
同时在 flow 删除前补收最后一段硬件计数，并保留 Linux 6.18 对应的
`NF_FLOW_HW_DEAD` 状态处理。

`020-mediatek-ppe-accounting.sh` 只接受官方 MediaTek `KERNEL_PATCHVER=6.18`，
将 `patches/*.patch` 安装到 `target/linux/mediatek/patches-6.18/`。
已有同名文件必须与仓库补丁完全一致；遇到冲突会停止，不覆盖上游内容。
重复运行是幂等的。

源码准备阶段按 source ID 选择补丁 bundle。脚本和补丁内容参与源码快照指纹，
Markdown 文档不参与；源码树的 `.owrt-fix-bugs` 保存该指纹。
修复内容变化时，刷新设备编译缓存会清除 `build_dir/target-*` 和
`staging_dir/target-*`，重建内核及目标软件包，同时保留 Host 工具、目标工具链和下载缓存。

## 回归测试

`tests/test_ppe_accounting.py` 从 Linux 6.18.60 的函数摘录中提取原始和修复后的真实函数，
应用仓库中的补丁，再通过 `harness.c.in` 编译执行硬件状态桩。
覆盖计数回退、重复计数、正常增量和删除前最后计数。测试不替代真实硬件 MMIO、
并发和持续转发验证。

运行：

```sh
python3 -m pytest tests/test_local_patches.py tests/test_ppe_accounting.py
```
