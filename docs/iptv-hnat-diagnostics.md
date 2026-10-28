> 历史资料：仅针对第三方 237 / Linux 6.6 固件。
> ImmortalWrt v25.12.2 分支已移除此专用内核补丁和诊断工具。
> 本文仅供原问题溯源，不要按下面步骤操作官方 6.12 固件。

# MT7986 IPTV 错送修复与诊断固件

本次修复针对 `padavanonly/immortalwrt-mt798x-24.10` 的
`openwrt-24.10-6.6` 分支，已核对上游提交
`ec9ef10efc65da1e6d1de4e2c043c0e13d08eed8`。构建项目的指定分支是
`codex/github-actions-ready`。选择 `n60pro` 正常构建即可，不需要另选诊断包。

## 已找到的原因

上游 `9999-02-mtk_enhance.patch` 在 `mtk_poll_rx()` 中加入了 `sent_ppd`：

```c
int sent_ppd = 0;             /* 整批 NAPI 轮询只初始化一次 */
while (done < budget) {
    /* 某个特殊回注包触发 */
    if (mac == 4 || reason == HIT_BIND_FORCE_TO_CPU)
        sent_ppd = 1;
    /* 后面的普通包也会走进这里 */
    if (sent_ppd && ppd_dev)
        skb->dev = ppd_dev;
}
```

标志没有逐包重置。一个特殊包把它置为 1 后，同批剩余普通包也被改送到
`ppd_dev`；后续补丁还给这些包设置 `0x78681415` mark。

2026-10-28 的实机 `any`/SLL2 抓包同时观察到正常 IPTV 路径和另一 LAN 接口：
正常路径在共同序号窗口内缺失的 **226 个 RTP 序号全部出现于错误 LAN 接口**，
两处合并后缺口为零。原始抓包保存在诊断主机，未提交用户网络抓包到本公开仓库。
这解释了父接口抓包缺口、短簇缺包、全局关闭 HNAT 恢复，以及原来处于家庭桥时
可以观看而隔离后异常的现象。后者属于结合代码与拓扑的解释，仍需刷入后验收。

修复默认逐描述符清除 `sent_ppd`。正常单播 HNAT、VLAN 配置、广播隔离均保留。
**尚未在已刷入的新固件上完成长期 IPTV/吞吐验收，不把代码修复等同于现场验收。**

## 一次编译包含的内容

补丁位于 `scripts/iptv-hnat/overlay/target/linux/mediatek/patches-6.6/`。
默认启用状态清零修复；诊断记录和所有额外绕过选项默认关闭。

- DMA 描述符阶段、skb 建立阶段、最终送入协议栈阶段的目标 RTP 序号统计。
- 特殊包触发后的“旧代码会错送”计数与事件；记录原始描述符、触发描述符、
  NAPI 轮询编号、描述符下标、source port、CPU reason、最终接口编号。
- 驱动提前释放、内存分配失败、XDP 分支的计数。全局计数不代表目标 RTP 丢包数。
- 13 个 HNAT NF hooks 的入口、返回 verdict、DROP/STOLEN 事件；回调可能消费 skb，
  返回后只使用事先复制的包头摘要，避免读取已释放 skb。
- 独立跳过任意 NF hook、跳过 Wi-Fi hook 函数体，及真正移除 Wi-Fi RX/TX hook 指针。
- 目标流跳过 HNAT NF hooks、清除 RX HNAT 元数据、跳过 GRO、定向选择接收 MAC
  的实验开关；这些都是后续诊断选项，不作为默认修复。
- 独立控制 PPE 的 SMA=2/3，定时器尊重该选择；不需要再通过 `/dev/mem` 改寄存器。
- `legacy_sticky_ppd` 运行时开关可以短时重现旧缺陷，随后恢复修复模式。

诊断内存固定分配，事件环容量 1024，记录统计与包头摘要，不记录视频载荷；
没有逐包 printk、日志定时刷盘或开机自动采样。采集器另可保存有大小上限的短头 PCAP。

## 编译与缓存

```sh
./owrt build n60pro
```

GitHub Actions 的 `OpenWrt Build` 工作流选择本分支、设备 `n60pro`。
本提交不主动触发工作流，避免未经用户选择消耗两小时构建。

项目会将补丁 bundle 的 SHA256 加入不可变快照指纹，并校验已发布快照的补丁版本；
旧快照会失效。Web 方式需部署本提交的 builder 后刷新源码，再提交任务。
补丁指纹变化时，在 `make defconfig` 之后执行一次 `make target/linux/clean`，
保留工具链与下载缓存，避免沿用已展开的旧内核。仅在正式编译成功后记入新缓存标记。
rootfs 包含 `/etc/iptv-hnat-diag.version`，用于核验固件实际包含的 bundle。

准备阶段会校验相关上游源码/已有补丁的 SHA256，若上游变动会提前失败并指出文件，
避免不兼容补丁等到编译末尾才失败。`reviewed.json` 不是上游 commit 选择器，
项目仍跟随配置分支，但变动涉及本补丁时必须重新审核。

## 刷入后的第一步

先按原方式备份和安装这个项目为 N60 Pro 生成的固件，保留现有网络拓扑。
本补丁没有改变分区或引导布局，不涉及跨刷官方 FIT 布局。

```sh
iptv-hnat-diag status
```

应看到 `abi=1`、`legacy_sticky_ppd=0`、`enable=0`、NF/Wi-Fi skip 为 0。
HNAT 保持原有开启配置。先正常观看受影响频道，确认画面，再做短采样：

```sh
iptv-hnat-diag capture 30
```

采集器会按 debugfs 中的目标组/端口，通过本机 `127.0.0.1:5140` 读取直播以维持入组。
默认目标为诊断中使用的 `239.0.1.164:3002`；本机应运行原来的 rtp2httpd。
若端口/服务地址不同，请调整采集脚本，或手动维持直播并采集 debugfs。

命令输出结果目录 `/tmp/iptv-hnat-diag.XXXXXX`。它包含各阶段 stats、events、
只读寄存器快照、接口编号、HNAT 状态、softnet/ethtool 计数，以及最多 4×8MB
的短头 PCAP（可能轮转，分析时按包时间戳排序；不要把轮转边界当丢包）。
命令结束会恢复之前的运行时设置；中断信号也会回退，并有独立 20 分钟守护。
采集器先检查 `/tmp` 空间。没有写 UCI、重启网络、刷新全部系统日志或上传数据。
SSH 意外终止后可检查该目录的 `restore.sh`，正常结束有 `DONE` 标记。

如需一轮完整 A/B，对照每项 15 秒：

```sh
iptv-hnat-diag matrix 15
```

顺序为：修复模式 → 旧缺陷模式 → 旧模式跳过全部 NF → 旧模式移除 Wi-Fi hooks
→ 旧模式同时跳过两者 → 再次修复。**matrix 会主动短时恢复旧错误行为，可能再次卡顿**；
它只供验证原因，不需要为了日常使用而执行。默认安装后始终是修复模式。
不要并发运行多个控制实验。若进程被 SIGKILL，待守护恢复后确认锁状态，再清理旧锁。

手动恢复正常修复模式：

```sh
iptv-hnat-diag restore
```

该命令不改变 UCI，也不强制改变原来的 HNAT hook_toggle。

## 如何看结果

`/sys/kernel/debug/mtk_iptv_diag/` 下的主要数据：

| 节点 | 作用 |
|---|---|
| `stats` | 版本、模式、各阶段计数、按来源/SSRC/接口分开的序号统计、CPU reason 直方图 |
| `events` | 有界事件环；有累计写入与覆盖数量，读取不清空 |
| `registers` | 适用于 MT7986/NETSYS v2 的只读 FE/PSE/PDMA/PPE 寄存器快照 |
| `reset` | 写 1 清统计与事件，不改变运行时控制 |
| `enable` | 0/1，仅控制采样，默认 0 |
| `dst`, `src` | 主机序 IPv4 整数，0 为任意；如目的组写 `0xef0001a4` |
| `port`, `vlan` | 目的 UDP 端口、VLAN；0 为任意，VLAN 默认不过滤，兼容硬件剥标签 |
| `sample_every` | 0 只记录首包/缺口/特殊动作；N 则另每 N 个目标包采样一个事件 |
| `legacy_sticky_ppd` | 0 修复，1 恢复旧循环外标志残留，仅诊断时使用 |
| `nf_skip_mask` | 13 个 NF hook 的位掩码；8191 跳过全部回调函数体 |
| `wifi_skip_mask` | bit0 TX、bit1 RX，跳过函数体；真正移除指针用下述 HNAT 节点 |
| `multicast_bypass` | 只对所过滤的 RTP 流跳过 NF hooks，不是硬件 ACL |
| `rx_clear_hnat` | 只对所过滤的 RTP 流清 magic、设置 alg；不改硬件 |
| `no_gro` | 所过滤的 RTP 流直接 `netif_receive_skb` |
| `rx_mac` | 0/1/2 实验性接收 MAC 覆盖，默认 UINT32_MAX 表示不覆盖；不要随意改变 |

`/sys/kernel/debug/hnat/diag_wifi_detach`：0 原样、1 移除 TX、2 移除 RX、3 两者移除。
恢复时检查现有 hook_toggle；不会在全局 HNAT 关闭时擅自恢复 Wi-Fi hooks。
`diag_sma`：2 仅送 CPU、3 送 CPU 并建表；更改后不清空既有 BIND 条目。
清空条目仍使用 SDK 原有 `hnat_entry`，无需新的任意地址写入工具。

阶段编号：0 为描述符读出后的原始包头（通过 DMA 同步读取）；1 为 skb 建立后；
2 为所有 PPD 改写完成、即将交给协议栈；3 为驱动失败分支；4 为旧逻辑会错送的普通包。
8..20 对应 NF 入口，24..36 对应 NF 返回。事件 action：0 普通、1 DROP、2 STOLEN、
3 跳过、4 RX 失败、5 旧逻辑错送候选。

NF 位编号固定：0 IPv4 local_in，1 IPv6 local_in，2 IPv4 conntrack，3 IPv6 conntrack，
4 IPv4 pre，5 IPv6 pre，6 IPv6 post，7 IPv6 local_out，8 IPv4 post，9 IPv4 local_out，
10 bridge local_in，11 bridge local_out，12 pong。若 SDK 调整 bridge hook 挂载位置，
编号仍跟随原数组槽位。

全局 counter：0/1 Wi-Fi TX/RX 调用，2/3 相应跳过；4 无效 MAC，5 resetting，
6 分配/映射失败，7 XDP 分支，8 skb 构造失败；10 清 RX 元数据，11 GRO 提交，
12 直接提交，14 旧逻辑错送候选，15 PPD 触发。16+id 为 NF 调用，32+id 为 DROP，
48+id 为 STOLEN。全局 counter 可能包含其他业务，只有带目标流摘要的记录才属于该 RTP。

`missing_window64` 是即时序号估计，可消化 64 个序号内的迟到与重复；更深乱序或
序号重置会进入 `outside_window`，此时不可把它当精确丢包数。保留的 PCAP 用于离线
按流、SSRC、回绕与统一首末范围重新计算。每阶段最多四条来源/SSRC/接口组合，
超出在 overflow 计数中明确报告。

修复验收：默认模式中正常接收接口不再缺序号，错误 LAN 接口不再收到这些 RTP，
HTTP TS 连续性无异常，电视流畅，随后验证外网吞吐/CPU与广播隔离。
`stage=4` 的“旧代码会错送”事件在修复模式仍可出现，它表示已避免的旧逻辑条件，
不是修复后仍丢包。判断实际送到哪里应看 stage 2 的 ifindex 和同时抓包。

## 已完成的构建验证

- 校验 Linux 6.6.133 官方 tarball SHA256，与上游 include/kernel-6.6 一致。
- 按真实顺序应用 generic backport/pending/hack 和全部 MediaTek 补丁，再应用本补丁。
- 使用现网 `/proc/config.gz` 准备 ARM64 内核，编译新增诊断、Ethernet 和所有 HNAT C
  编译单元并链接 HNAT 对象；这不是完整固件构建或刷入后的硬件验收。
- Python 测试覆盖补丁指纹/上游漂移拒绝/旧快照失效/内核缓存清理和失败重试标记。
- 采集脚本通过 POSIX shell 语法检查，现场完整采集需要新固件的 debugfs 节点。
