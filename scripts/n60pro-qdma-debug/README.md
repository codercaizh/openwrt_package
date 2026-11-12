# N60 Pro TX 超时诊断

分支 `codex/n60pro-qdma-debug` 推送后由 GitHub Actions 构建 N60 Pro 的 sysupgrade ITB。
固件使用 ImmortalWrt `master`，额外加入本目录的诊断补丁和采集服务。

内核在 TX watchdog 或驱动自身的 DMA 检测触发时读取 Frame Engine、QDMA、PSE、PPE、
两口 MAC 和 Linux TX 队列状态。TX watchdog 首次超时立即记录，之后每 15 秒最多一组。
诊断使用现有的一秒 DMA 检测定时器，在内存中保留最近 12 次采样。
重置工作额外记录开始、硬件初始化结果和尝试重启的 MAC 位图。
正常流量路径在内存中更新计数和各 MAC 最近一次提交的 SKB 元数据。
采集只读取寄存器，沿用驱动原有的提交、回收和重置条件。

对照固件仅关闭 N60 Pro `eth0` 固定 CPU 链路的接收 PAUSE，保留发送 PAUSE。
链路启动时记录 `rx_pause=0`、`tx_pause` 和 `mac_ctrl` 的寄存器回读值，超时快照也记录 `mac_ctrl`。

- `reset_allowed` 是 watchdog 路径按原逻辑决定是否安排重置的结果。
- `trigger=dma-monitor` 时，`monitor` 标明 WDMA、QDMA 或 ADMA 检测分支。
- `next` 是软件准备好的下一描述符指针。与 `ctx` 对照可查是否有准备后尚未通知硬件的描述符。
- `ctx/dtx` 是 CPU 提交和 DMA 取走的指针，`crx/drx` 是 CPU 回收和 DMA 完成的指针。
- `blocked=1` 表示回收轮询遇到 DMA 仍持有的描述符，`blocked=2` 表示描述符没有软件缓冲区。
  同时记录该描述符的 `txd3/4/5`。后续轮询会刷新回收阻塞标记。
- `last` 记录每个 MAC 最近一次 SKB 的队列、帧长、外层类型、VLAN、GSO/TSO、描述符数、
  预估描述符数和是否写提交寄存器。它是最近一次提交的元数据，无法直接指认触发故障的包。
- `fe_seen` 是本次启动以来在中断处理路径观察到的 Frame Engine 错误位。
- `qtx_page` 是采集时的页选择值，队列槽位 1 和 3 的配置、调度、头和尾均为只读采样。
- `drv_stopped` 和 `stack_stopped` 是 Linux TX 队列停止位图。
- `qfsm/qfwd` 与官方 DMA hang monitor 使用同一组寄存器。快照也记录 monitor 的 WDMA、QDMA、ADMA 连续异常次数。

启动后用 `/etc/init.d/n60pro-qdma-debug status` 检查采集服务。
故障时即使 LAN 已失联，服务仍从本机内核日志读取快照，并写到
`/root/n60pro-qdma/`。先同步内核现场到闪存，再补网口功能、收发计数、桥接 FDB、WAN
和中断状态。每次开机保留第一组和最近一组，最多保留 8 个文件，每个文件限制为 128 KiB。
重启后可用 SSH 或 SCP 取回。手动检查采集内容可运行 `/usr/sbin/n60pro-qdma-debug collect`。
