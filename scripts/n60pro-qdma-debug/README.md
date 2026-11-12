# N60 Pro TX 超时诊断

分支 `codex/n60pro-qdma-debug` 推送后由 GitHub Actions 构建 N60 Pro 的 sysupgrade ITB。
固件使用 ImmortalWrt `master`，额外加入本目录的诊断补丁和采集服务。

内核在 TX watchdog 触发时读取 Frame Engine、QDMA、PSE、两口 MAC 和 Linux TX 队列状态。
首次超时立即记录，随后限频到每 15 秒最多一组。补丁沿用驱动原有的重置条件。
正常流量路径仅更新 TX NAPI 轮询计数、工作量及回收中止标记，无逐包日志。

- `reset_allowed` 是 watchdog 路径按原逻辑决定是否安排重置的结果。
- `ctx/dtx` 是 CPU 提交和 DMA 取走的指针，`crx/drx` 是 CPU 回收和 DMA 完成的指针。
- `blocked=1` 表示回收轮询遇到 DMA 仍持有的描述符，`blocked=2` 表示描述符没有软件缓冲区。
- `drv_stopped` 和 `stack_stopped` 是 Linux TX 队列停止位图。
- `qfsm/qfwd` 与官方 DMA hang monitor 使用同一组寄存器。快照也记录 monitor 的 WDMA、QDMA、ADMA 连续异常次数。

启动后用 `/etc/init.d/n60pro-qdma-debug status` 检查采集服务。
故障时即使 LAN 已失联，服务仍从本机内核日志读取快照，并写到
`/root/n60pro-qdma/`。每次开机保留第一组和最近一组，最多保留 8 个文件，
每个文件限制为 128 KiB。归档记录当前网口功能、收发计数、桥接 FDB、WAN 和中断状态。
重启后可用 SSH 或 SCP 取回。手动检查采集内容可运行 `/usr/sbin/n60pro-qdma-debug collect`。
