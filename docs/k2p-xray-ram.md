# K2P Router：Xray 核心开机加载到 RAM

适用：`k2p` 标准路由版（K2P A1/A2，MT7621，16MiB NOR、128MiB RAM）。
`k2p-ap` 不包含以下脚本，也不会执行自动下载。

## 为什么这样做

K2P 的路由版保留 PassWall、dnsmasq-full、Firewall4、LuCI、LAN/WAN、
Wi-Fi 和 PPPoE，但不选择 `CONFIG_PACKAGE_xray-core`，也不勾选 PassWall
的 `INCLUDE_Xray`（该选项会强制打包 `xray-core`）。
启动脚本只在 RAM `/tmp/k2p-xray/xray` 放置一个可执行核心。
固件中 `/usr/bin/xray` 是指向该 RAM 路径的符号链接；
PassWall 自带的默认 `global_app.xray_file` 为 `/usr/bin/xray`，
因此不需要修改持久化 UCI 配置。PassWall 会在运行时检查文件是否可执行。

固定版本为官方 **Xray v26.10.21**，官方资产
`Xray-linux-mips32le.zip`（约 22MiB），仅解压 `xray_softfloat`
（约 34MiB，针对 MT7621 无硬件 FPU 的 MIPSLE 处理器）。
脚本内固定压缩包 SHA256：
`1e9953aecee4024d9da6427a42929817b53d1660f5122ec6b22629e11cab4012`。

## 启动与失败行为

固件 `files` overlay 在构建时注入：

- `etc/init.d/k2p-xray-ram`（START=98；PassWall 为 START=99）
- `etc/rc.d/S98k2p-xray-ram` 指向前述启动脚本
- `usr/bin/xray` 指向 `/tmp/k2p-xray/xray`
- `etc/config/k2p_xray` 提供可修改的 HTTPS 下载地址

每次开机：检查 `/tmp` 空间和可用 RAM → 下载精确版本 →
SHA256 完整性校验 → 提取软浮点 Xray → 设置执行权限并原子发布。
脚本在 PassWall 启动之前同步执行。临时 ZIP 随后删除。

**启动需要直连可用的 WAN、DNS 和 GitHub Releases。**
如果网络、TLS、内存、校验或解压出问题，核心不会发布，
会记录 `logread` 日志；普通 DHCP、NAT、LAN/WAN 继续工作，
PassWall 的 Xray 代理无法启动。不下载或执行校验不通过的文件。
开机等待下载最长约 90 秒，之后继续正常启动。未将核心写入闪存，
所以每次断电/重启都须重新下载。内存紧张时可能无法加载或触发 OOM。

检查：

```sh
logread -e k2p-xray-ram
ls -lh /tmp/k2p-xray/xray
/usr/bin/xray version
df -h /tmp
free -m
```

网络恢复后手动重试，无须重启设备：

```sh
/etc/init.d/k2p-xray-ram start
/etc/init.d/passwall restart
```

## GitHub Releases 无法直连

允许把**完全相同版本、内容未变化**的官方 ZIP 镜像托管到能访问的 HTTPS
地址（例如自己的香港服务器），然后配置镜像：

```sh
uci set k2p_xray.settings.url='https://你的镜像域名/固定文件名.zip'
uci commit k2p_xray
/etc/init.d/k2p-xray-ram start
/etc/init.d/passwall restart
```

允许修改下载 URL，但 **SHA256 校验值不可通过 UCI 修改**。更换 Xray
版本时须更新仓库脚本中的固定 URL 与哈希，并重新打包固件。
不要使用动态 latest 下载链接或关闭哈希校验。

## 容量与性能限制

K2P 128MiB RAM 默认 `/tmp` 通常为内存型文件系统，ZIP 与解压出的
核心会短暂共存，峰值占用约 56MiB 以上。脚本要求至少 60000KiB
的 `/tmp` 空闲空间，且 `/proc/meminfo` 提供该指标时 `MemAvailable`
不得小于 65536KiB。

该方案 **只节省闪存**，不减少 Xray 的内存与 CPU 消耗。MT7621 的吞吐
和代理连接数可能受到明显限制；不建议同时启用大量分流规则或节点。
Xray 支持 VLESS/Reality；**Hysteria2 并非 Xray 核心支持的协议**，
现有 Hy2 节点不能直接迁移到此方案。

务必先通过 GitHub Actions 的 `k2p` 矩阵任务进行镜像大小与包依赖
验证，随后在已备份且有救援途径的真机上测试；不要仅凭 `.config`
就认定已满足 16MiB 闪存和 128MiB RAM 的限制。
