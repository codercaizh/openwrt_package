# ImmortalWrt v25.12.2 官方固件迁移与 n2 运行手册

## 0. 基线及边界

- 基线仓库：`codercaizh/openwrt_package`
- 基线分支：`codex/github-actions-ready`，起始 SHA `15a18075955951fbc31db5fc18a2cc1fa60d90ba`
- 本次分支：`immortalwrt-25.12.2`
- 官方固件源码：`https://github.com/immortalwrt/immortalwrt.git`，**固定 Git tag `v25.12.2`**。切勿替换为浮动的 `master` 或 `openwrt-25.12`，除非专门进行下次版本升级。
- 四个设备：`n60pro`（Netcore N60 Pro）、`360t7`（Qihoo 360T7）、`s905d`（N1/S905D）、`vplus`（H6）。
- 运行路径：`/data/immortalwrt-25.12.2`。旧 `/data/owrt-builder` 及其他项目**不做清理/迁移**。
- 所有源码和第三方 feed 的实际提交 hash 由 prepare snapshot 记录；用户要求的固件主源码固定到 release tag，第三方插件则有意跟随上游更新。刷新 feed 将得到新的 source snapshot，**不是完全固定依赖的可重复构建**。
- 这只是构建项目迁移，不代表设备本机能安全直接 sysupgrade；N60 Pro 的 SPI-NAND/UBI、BL2/FIP 分区与原第三方 U-Boot 需要先核实。

## 1. 源码和兼容性调整

`configs/devices.toml` 中 ARM 和 MediaTek 两类 source 均切换到 ImmortalWrt 官方 Git `v25.12.2`；设备平台分开记录，防止 ARM 和 mediatek snapshot 混用。

保留 `armsr/armv8` rootfs + `unifreq/openwrt_packit` 的 S905D/H6 后处理。ARM **开机镜像并非官方源码直接生成的盒子专属镜像**：打包器仍需要 `codercaizh/openwrt_package` 的 `kernel_stable` release 内核资产和 `unifreq/openwrt_packit`，这些外部组成不由 `v25.12.2` tag 锁定。制作后仍需在对应硬件上验证引导/DTB/网络。

MediaTek 官方 Filogic N60 Pro 和 360T7 的升级产物为 `*-squashfs-sysupgrade.itb`，打包器已支持收集/校验 `.itb`，不再只查找 `.bin`。不自动刷新 BL2/FIP，不自动部署到路由器。

删除 237 内核 6.6 专用的 IPTV/HNAT overlay、对应 SHA guard、手动 Rust 1.90 配方覆盖。采用官方 25.12 配套的内核 6.12/Rust。按用户最新要求恢复原分支的第三方 Go `27.x` feed，使最新 Tailscale 可编译；Go 版本校验同时支持新 `golang-values.mk` 和旧 `golang/Makefile` 声明。

配置片段由 `make defconfig` 补齐派生 symbol：ARM 不再带旧 `CONFIG_TARGET_BOARD=armvirt` / `CONFIG_TARGET_SUBTARGET=64` 等历史生成字段；N60 Pro/360T7 显式声明目标；PassWall 启用 nftables 而不是强制旧 iptables 透明代理；三个配置片段均包含 `luci-app-pushbot`（两个 ARM 盒子共享 `armv8.config`）。

## 2. 插件来源（优先上游原仓库）

| 插件 | Git 上游 / 获取方式 |
| --- | --- |
| PassWall LuCI | `Openwrt-Passwall/openwrt-passwall` |
| PassWall 运行依赖 | `Openwrt-Passwall/openwrt-passwall-packages` |
| Go 工具链 | `sbwml/packages_lang_golang` `27.x`，跟随最新补丁；本次核验为 1.27.3 |
| Tailscale 核心 OpenWrt 配方 | `openwrt/packages` master，仅稀疏同步 `net/tailscale`；同步脚本按 Tailscale 官方最新稳定 release 更新版本/SHA256，本次为 1.104.4 |
| Tailscale 社区 LuCI | `openwrt/luci` master，仅稀疏同步 `luci-app-tailscale-community` |
| FRP/FRPC 核心配方 | `openwrt/packages` master，稀疏同步 `net/frp`（源码为 fatedier/frp） |
| FRPC LuCI | `openwrt/luci` master，仅稀疏同步 `luci-app-frpc` |
| Lucky 核心及 LuCI | `gdy666/luci-app-lucky` main，同一源内有 lucky 与 luci-app-lucky |
| Pushbot | `zzsj0928/luci-app-pushbot` master |
| RTP2HTTPD | `stackia/rtp2httpd` |
| Cloudflare Speed Test LuCI | `stevenjoezhang/luci-app-cloudflarespeedtest` |
| 其他历史补充插件 | `kenzok8/openwrt-packages`；有同名包时以上特定上游优先 |

这些上游 HEAD 会变化。用户要求 Tailscale 保持最新稳定版，Go 通过原分支升级逻辑跟随 27.x 最新补丁；其他插件遇到兼容性问题时单独分析，内核/Rust 继续采用官方版本。

`owrt_builder/upstream_versions.py` 在 feed 克隆后、发布源码快照前同步 Tailscale。
它保留 OpenWrt 的启动脚本、配置和编译选项，从官方归档读取 `go.mod` 检查 Go 要求；
不兼容则立即失败。`.owrt-upstream.json` 记录 release/源码 SHA256，`feed_commits` 的
`tailscale-source` 记录真实 release 提交，因此配方仓库未更新时也能区分新 release。
已经发布的快照保持固定；需要更新时刷新源码，而不是修改正在使用的快照。

`feeds.py` 使用 OpenWrt 的原生 `scripts/feeds update/install` 以及轻量稀疏获取上游单包；仍保留必要的同名包冲突链接清理，避免错误使用官方收录的旧配方。源快照和发布之间不跟随移动分支。

## 3. n2 服务器环境/空间

核查时：
- 主机 n2，x86_64，2 vCPU，约 2 GiB RAM + 2 GiB swap；
- `/data`：50GB btrfs，已用 8.6GB、可用约 41GB（18%）。起始时**已有超过 30 GB 可用**，没有删除其他项目文件。
- `/`：30GB ext4，约 20GB 可用；Docker 根目录是 `/var/lib/docker`，**不是 /data**；镜像创建需关注系统盘。
- 第一次编译 Go/Rust、大量插件容易导致内存瓶颈；建议先 `--jobs 1` 跑单台 N60 Pro，然后视结果启动 ARM 构建。不要一次同时编译四台设备。
- 不允许把旧的 /data 其他目录误加入编译器缓存清理范围。

## 4. 快速使用

```bash
cd /data/immortalwrt-25.12.2
git status --short --branch
./owrt doctor --json
./owrt devices
./owrt build n60pro --jobs 1
./owrt build s905d --jobs 1
./owrt build vplus --jobs 1
./owrt build 360t7 --jobs 1
```

编译产物和日志按 BuildEngine 保存在 `OWRT_WORKSPACE`（通常是项目的 `.owrt`）的 `artifacts/`、`tasks/`；Web/worker 自定义 workspace 路径以部署配置为准。Docker/BuildEngine 默认并行配置**没有写死 1.5 CPU**。

**本次 n2 限速**：仅使用独立于仓库的临时 Docker CLI 包装器，在调用 `docker run --name owrt-build-...` 时附加 `--cpus=1.5 --cpu-shares=512`；只对本次 Shell 会话临时修改 PATH。GitHub Actions 和正常 `./owrt` 一律保留默认全核。不要把该 PATH 或参数加入仓库、service、Dockerfile 或 compose。

## 5. 验收点

1. 运行 Python 测试，并确认官方设备 source/tag、FeedSpec、Go 版本检测、`.itb` 固件收集、pushbot 配置；
2. 用实际官方源码准备 feed，运行原生 `make defconfig`，检查 `CONFIG_PACKAGE_luci-app-{passwall,pushbot,lucky,frpc}`、`CONFIG_PACKAGE_tailscale` 与目标 profiles 未被 Kconfig 退选；
3. 全量 `make download` / `make -j1` 编译后核验 N60 Pro 的 `.itb`、ARM `rootfs.tar.gz` 和 packit 镜像及 manifest；
4. 检查 APK 软件包（25.12 使用 apk，而非旧版 ipk）；记录每一个 feed 源仓库提交；
5. 编译成功 ≠ 可直接刷机。升级 N60 Pro 前备份 mtd、确认 U-Boot/UBI 布局和 sysupgrade 平台检查；两台 ARM 盒子先进行串口/备用存储启动验证；
6. 原 237 的 IPTV/HNAT 修复无法直接适配 Linux 6.12，请先用官方原版验证 IPTV，再决定是否重做专用补丁。

## 6. 交接与故障定位

遇到网络拉取错误先检查 `/data`、`/var/lib/docker` 磁盘、GitHub 访问、`sources` 与 `tasks` 日志；遇到 Go/Rust 编译 OOM 优先减少 `--jobs`、检查 `dmesg`，不要在此分支恢复 237 专用补丁。

若首次编译失败，保留日志并记录**第一次实际失败的 make 步骤**；目前文档中列出的插件被选中不等同于已经在实际 25.12 feed 中通过全量编译。

本分支默认不修改任何正在运行的路由器或 n2 上已存在的服务配置。

## 7. 2026-10-28 构建故障记录

- n2 首次任务 `114b357cdfe94da38d2ed2a4443fd402` 在配置合成时报缺少 `defconfig/mt7975-ipailna-high-power.config`：这是旧 237 源码的 CONFIG_APPEND，官方 v25.12.2 不提供。提交 `fe20e35` 移除 N60 Pro 和 360T7 的过时引用，并增加回归检查。重试复用了已有源码/feed 快照，日志为 `/data/immortalwrt-25.12.2-build/n60pro-build-retry.log`。
- GitHub Actions run `40620839038` 的 `build-n60pro` 在 `./owrt doctor --json` 阶段因 `find scripts` 失败，根因是 Git 不保留空目录，Ubuntu 22.04 Python 3.10 走容器后备路径时触发。启动器现在将 scripts 作为可选目录，若目录存在仍纳入镜像指纹；回归测试模拟无该目录的检出。
- 本次 n2 worker 的 1.5 vCPU 限额仅属于临时启动参数，仓库默认 CPU 设置不变。配置阶段通过并不表示完整固件已编译或设备已验证。
- Actions run `40621979179` 已通过 doctor，但在 `package/feeds/packages/tailscale/compile` 失败：Tailscale `1.104.1` 的 `go.mod` 要求 Go `1.27.1`，而官方配套工具链为 `1.26.8`，报 `GOTOOLCHAIN=local`。固定 OpenWrt 上游配方提交 `8de0abeb462df4b2ec0634abce8119fd20da659a`，提供要求 Go `1.26.6` 的 Tailscale `1.102.3`；其他插件继续跟随原上游。FeedSpec 支持完整提交号并核对实际 HEAD，准备版本升至 14，避免无声复用旧版不兼容快照。旧服务器任务 `d7264d84e2714cb98f0aa8d56f68e56b` 已正常取消，原日志保留。
- 此次修复验证：全套 Python 测试 `140 passed`；实际从 OpenWrt 上游稀疏拉取固定提交，核对 Tailscale `1.102.3` 源码包与配方的 SHA256 一致，并从该归档中读取 `go.mod` 确认要求 Go `1.26.6`。完整固件编译仍以新任务最终结果为准。
- 用户随后要求采用最新 Tailscale，上一条降级方案由本条取代：恢复原分支 Go `27.x` feed，撤销 Tailscale 提交固定，加入官方最新稳定 release 同步和归档 Go 版本检查；本次确认 Go `1.27.3` / Tailscale `1.104.4`。准备版本升至 15，使之前固定 1.102.3 的快照失效；旧服务器任务 `e26bdc891d494793b2cdc9ffa9287d9d` 已取消，旧 Actions run `40640471934` 已请求取消。
- 最新版本方案验证：`143 passed`；实际生成源码快照 `dda90379afb26a2af2465de10690502d4ae7482ec94811c097795bdaede9954e`，Go feed 提交为 `cd8953c31e0f64ea2853f87bb0f32256646a160d`，Tailscale release 提交为 `27047635f237d1f3e0b08c0f487674757f30d2f7`。N60 Pro 原生 `make defconfig` 通过，Tailscale、其社区 LuCI、PassWall、Pushbot、FRPC、Lucky 均保留选中；原生 packageinfo 确认 Tailscale `1.104.4` 和 Go `1.27.3`。完整固件编译仍待新任务完成。
