# 构建缓存设计说明

本文描述 `immortalwrt-master` 当前七设备构建矩阵的实际缓存实现。所有设备使用官方 ImmortalWrt
`master`，共享一套 Host/LLVM 22.1.3 缓存；目标交叉工具链仍按设备隔离。

## 1. 缓存目录

默认 GitHub Actions/便携 CLI 工作区为 `.owrt`。核心目录如下：

```text
.owrt/
└── cache/
    ├── dl/                         # 全设备共享的下载归档
    │   └── llvm-project-22.1.3.src.tar.xz
    ├── host-tools/
    │   └── master/
    │       ├── build_dir/host/      # tools/*，重点是 tools/llvm-bpf
    │       └── staging_dir/host/   # Host 工具安装结果
    ├── builds/
    │   ├── netcore_n60-pro/
    │   │   ├── cache.json
    │   │   └── openwrt/
    │   │       ├── build_dir/
    │   │       │   ├── host -> ../../../../host-tools/master/build_dir/host
    │   │       │   ├── hostpkg/     # 设备独立，不共享
    │   │       │   ├── toolchain-*/ # 目标交叉工具链
    │   │       │   └── target-*/    # 内核、目标软件包编译树
    │   │       ├── staging_dir/
    │   │       │   ├── host -> ../../../../host-tools/master/staging_dir/host
    │   │       │   ├── hostpkg/
    │   │       │   ├── toolchain-*/
    │   │       │   └── target-*/
    │   │       └── dl -> ../../../dl
    │   ├── 360t7/
    │   ├── s905d/
    │   ├── vplus/
    │   ├── r3g/
    │   ├── k2p/
    │   └── k2p-ap/
    ├── locks/                      # 设备锁、Host 锁、元数据锁
    ├── active.json                 # 当前活跃构建记录
    ├── .metadata.lock
    └── .eviction.lock
```

实际 symlink 相对路径由工作树位置计算，不要求固定写成上图中的字面相对路径。

## 2. 三类 GitHub Actions 缓存

GitHub-hosted runner 是临时机器，每个矩阵 job 各自有独立文件系统。Actions Cache 是 job 之间唯一的持久化通道；它不会让七个运行中的 job 实时共享同一块磁盘。

### A. 共享 Host/LLVM 编译缓存

缓存路径：

- `.owrt/cache/host-tools/master/build_dir/host`
- `.owrt/cache/host-tools/master/staging_dir/host`

Key：

```text
owrt-host-v2-Linux-master-llvm-22.1.3-v1
```

Restore prefix：

```text
owrt-host-v2-Linux-master-
```

包含：

- OpenWrt `tools/*` 的 Host 编译工作目录；
- `tools/llvm-bpf` 的 CMake/build 输出；
- 其他 Host 工具的中间产物；
- `staging_dir/host` 中安装的 Host 工具。

七个设备的上游分支和 Host LLVM 版本一致，因此全部使用这一份缓存。
`host_revision=llvm-22.1.3-v1` 是手动缓存 epoch；LLVM 版本、编译选项或 runner Host 编译环境发生不兼容变化时，应递增该 revision。

保存方式：`actions/cache/restore@v4` 只负责恢复；只有 N60 Pro 构建成功、缓存未命中时，
`actions/cache/save@v4` 才负责写入。其他六个 job 不写这份共享缓存，避免矩阵并发产生多个不同内容的同名 Host cache。

### B. 每设备目标工具链缓存

缓存路径按 canonical device 分开：

- `.owrt/cache/builds/<device>/cache.json`
- `.owrt/cache/builds/<device>/openwrt/build_dir/hostpkg`
- `.owrt/cache/builds/<device>/openwrt/build_dir/toolchain-*`
- `.owrt/cache/builds/<device>/openwrt/staging_dir/hostpkg`
- `.owrt/cache/builds/<device>/openwrt/staging_dir/toolchain-*`

Key：

```text
owrt-toolchain-v2-Linux-master-<canonical-device>-<UTC-week>
```

例如：

```text
owrt-toolchain-v2-Linux-master-netcore_n60-pro-2026-W47
```

Restore prefix：

```text
owrt-toolchain-v2-Linux-master-<canonical-device>-
```

它只复用相同设备的 `hostpkg` 和目标交叉工具链。ARM、MediaTek、Ramips 的目标 ABI、
配置和工具链不能混用。Key 按 UTC 周滚动；新周没有 exact hit 时会尝试恢复该设备上一个
可用的周缓存，构建成功后由 `actions/cache@v4` 的 post step 保存新周 Key。

注意：Actions 缓存路径不包含 `build_dir/target-*` 和 `staging_dir/target-*`。
因此 GitHub Actions 每次新 job 都会重新构建内核和目标软件包；这些目录只会在持久化的
本地工作区中保留。当前 Actions cache 不是完整的 OpenWrt build tree 快照。

### C. LLVM 源码归档缓存

唯一缓存文件：

```text
.owrt/cache/dl/llvm-project-22.1.3.src.tar.xz
```

Key：

```text
owrt-llvm-source-v1-Linux-22.1.3
```

这是源码下载归档，不是 LLVM 编译产物。GitHub Release 对该文件公布的大小为
167,045,212 bytes，约 159.3 MiB。它已经是 xz 压缩包，Actions Cache 再打包时体积变化通常不大。

### 明确不进入 GitHub Actions Cache 的内容

- `.owrt/cache/dl` 中除 LLVM 源码归档以外的完整下载目录；
- `build_dir/target-*`、`staging_dir/target-*`；
- 完整源码快照、feeds、任务日志和 manifest；
- 固件和 APK/IPK 包归档。

固件、包归档和构建证据通过 `actions/upload-artifact@v4` 单独上传，保留 7 天；它们不是 Actions Cache 配额的一部分。

## 3. 构建器如何使用缓存

1. `BuildCacheManager.acquire()` 按 canonical device 获取 `cache/locks/<device>.lock`，同一设备不能同时修改同一份目标编译树。
2. 检查 `cache.json` 的 schema、设备、source ID、`ready` 状态及缓存工作树。成功完成的同设备缓存优先复用。
3. 源码快照变化时，`refresh_source()` 用新快照替换源码文件，但保留 `build_dir`、`staging_dir` 和共享 `dl`。
4. `.owrt-fix-bugs` 指纹变化时，只删除 `build_dir/target-*` 和 `staging_dir/target-*`，保留 Host 工具、hostpkg、交叉工具链和下载归档。
5. `BuildEngine` 将每个设备的 `build_dir/host`、`staging_dir/host` 链接到固定的 `cache/host-tools/master` 路径。
6. make 参数为：

```make
BUILD_DIR_HOST=$(if $(IS_PACKAGE_BUILD),$(BUILD_DIR_BASE)/hostpkg,<shared-host-build-dir>)
STAGING_DIR_HOST=<shared-host-staging-dir>
```

因此 `tools/llvm-bpf` 和其他 `tools/*` 使用共享 Host 路径；`package/host` 构建则通过
`IS_PACKAGE_BUILD` 回到设备私有的 `hostpkg`。这是有意隔离，不能将 hostpkg 混成跨设备缓存。

7. 本地构建通过 `cache/locks/host-tools-master.lock` 对共享 Host 工具树加 flock，锁覆盖 make pipeline 和打包阶段，防止一个设备在另一个设备打包时替换共享 `fwtool` 或 LLVM 文件。
8. 构建开始时 `ready=false`；make、固件打包及包归档全部成功后才写入 `ready=true`。失败或取消的缓存不会被标记为可复用。

`cache.json` 保存 schema version、canonical device、source ID、snapshot ID、源码 commit、
配置 SHA256、创建/使用时间、最后 task ID 和 ready 状态。

## 4. 本地磁盘容量与淘汰

本地 `BuildCacheManager` 的设备缓存位于 `cache/builds/<device>`。估算新建设备缓存需要空间时：

- 有该设备历史缓存：取同设备最大实际占用；
- 没有同设备历史缓存：取其他已知编译树的最大占用；
- 完全没有样本：使用 8 GiB 默认估算；
- 历史样本估算额外乘 1.25，且不低于 8 GiB。

占用统计使用文件系统实际分配块（`st_blocks`），避免只看逻辑文件大小低估大量小文件的构建树。
空间不足时，按 `created_at` FIFO 选择其他设备的编译缓存，删除前必须取得对应设备锁；
活跃或锁忙的缓存会跳过。下载缓存、源码快照、日志和 artifacts 不参与此淘汰。

**当前实现边界：** 本地容量淘汰器扫描 `cache/builds/*` 和旧版 task build tree，
不把 `cache/host-tools/master` 或 `cache/dl` 作为淘汰对象。因此这两部分会额外占用工作区磁盘；
磁盘容量规划时应把它们单独计入。

## 5. 容量估算

下表是规划范围，不是本次新缓存的实测值。旧 Actions cache 已清理，新的 Host cache 尚未完成一次完整预热。

| 缓存部分 | 预期压缩后占用 | 共享方式 | 说明 |
|---|---:|---|---|
| Host/LLVM 编译产物 | 约 1.5–2.5 GiB | 七设备共享 1 份 | 主要是 `build_dir/host` + `staging_dir/host`；LLVM-BPF 是重点 |
| 每设备 hostpkg + 目标 toolchain | 约 0.3–0.9 GiB / 台 | 每台独立 | 七台合计约 2.1–6.3 GiB；不同 target ABI 不混用 |
| LLVM 22.1.3 源码归档 | 约 159.3 MiB | 七设备共享 1 份 | GitHub Release 资产原始文件大小 167,045,212 bytes |
| 其他 OpenWrt 下载归档 | Actions Cache 中为 0 | 不持久化 | Actions 每个 job 重新下载；本地 `cache/dl` 会跨设备共享 |
| target 内核/软件包中间产物 | Actions Cache 中为 0 | 本地按设备保留 | GitHub workflow 没有包含 target-* 路径 |
| cache.json / 锁 / active 状态 | 通常远低于 1 MiB | 按设备/工作区 | 元数据开销可忽略 |

按一代有效缓存估算，GitHub Actions 侧大约是 **3.8–9.0 GiB**：
一份共享 Host cache + 七份设备工具链 cache + 一份 LLVM 源码归档。
每周滚动的设备 Key 在旧缓存未过期前可能短暂保留上一代，因此高峰值会高于一代估算。

旧方案清理前，GitHub Actions 缓存清单曾有 8 个对象，合计
11,222,143,487 bytes（约 10.45 GiB）；当时把 Host 工具、目标工具链和完整下载目录
按设备/来源重复缓存。该合计不能直接拆分成当前三类缓存的实测值。

本次清理后，GitHub Actions Cache API 显示 **0 个缓存、0 bytes**。新缓存的真实压缩大小
要等 N60 Pro 成功写入 Host cache、以及各设备成功写入 toolchain cache 后才能确认。
后续应以 GitHub Actions cache usage 和每个 cache key 的 `size_in_bytes` 为准，更新上表的估算列。

## 6. 冷启动与预热注意事项

GitHub 矩阵的七个 job 同时启动，所有 job 在启动时各自执行 cache restore。
当 Host cache 为空时，它们不会等 N60 Pro 在稍后完成并上传缓存；因此第一次冷启动矩阵中，
多个 runner 可能各自重复编译 LLVM-BPF。只有 N60 Pro 成功结束后才会写入共享 Host cache。

要避免第一次矩阵重复编译，应先单独运行 N60 Pro 完成 Host cache 预热，再启动完整七设备矩阵。
预热完成后的后续 job 才能在开始编译前恢复同一份 Host cache。
