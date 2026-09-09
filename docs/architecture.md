# 源、feed、插件目录与配置接口

本文档是 `owrt_builder` 与 Web/build worker 之间的最小接口约定。源码准备和目录扫描都是同步函数；Web 层负责在线程/任务队列中调用它们，并把状态回调转成 SSE 或任务日志。

## 设备与源码

| key | aliases | source | config fragment | 说明 |
| --- | --- | --- | --- | --- |
| `360t7` | `360-t7`, `qihoo_360t7` | `immortalwrt-mt798x` | `360t7.config` | 路由默认配置 |
| `netcore_n60-pro` | `n60pro`, `n60-pro`, `netcore-n60-pro`, `netcore_n60_pro` | `immortalwrt-mt798x` | `n60pro.config` | 路由默认配置，N60 Pro 真机验收 |
| `s905d` | `n1` | `armv8` | `armv8.config` | 盒子通用配置；`s905` 是不同设备，不作为别名 |
| `vplus` | `h6-vplus` | `armv8` | `armv8.config` | 盒子通用配置 |

源码仓库和分支是 `SourceSpec` 的常量；准备完成后以 git 实际 `HEAD` 作为 snapshot 的 source commit。接口不接受用户输入的 commit。snapshot 目录不可变，`current` 只是原子替换的符号链接；正在编译的任务只使用其创建时返回的 `PreparedSource.path`/`snapshot_id`，后续 feed 更新不会改变它。

## Feed

默认 feed 与原项目来源保持一致：Kenzo、rtp2httpd、luci-app-cloudflarespeedtest、Passwall packages、Passwall LuCI，以及原流程中的 `packages_lang_golang` 27.x 替换。Passwall 跟随仓库默认分支，取消旧的固定 commit。Tailscale 是项目自有的 overlay package，不从默认 packages feed 取包：它固定使用官方仓库 `v1.102.3`（commit `53a0d659afa51835dd7a9283873cca44261454f8`，源码归档 SHA256 `0e94d961c31ce7d33e8b7ce4ac6fdbec83ee5658784eed69eb7fce300729d717`）和项目维护的 OpenWrt Go 打包配方。`luci-app-tailscale-community` 从其 Git 仓库固定到 `99d7dea5d83d175ec95e777b606086cf415c369d`，不早于安全修复 commit `f6fbeb749a989b6e7aa6fc33fddc6e3f6faaf392`；它只提供 LuCI、RPC 和 settings init 脚本，`/etc/init.d/tailscale` 与 `/etc/config/tailscale` 由官方 Tailscale 包单独提供。准备阶段移除默认 feed 中同名旧包链接，并把官方 commit、recipe hash 和 LuCI commit 写入 `feed_commits`，因此两个 source 分支、Web 和 GitHub Actions 使用相同的包来源。Rust 1.90.0 的替换仅在实际 Makefile 版本匹配时执行；替换文件不存在或补丁失败要报告错误。

Feed 准备完成后会读取 Go feed 的 `GO_VERSION_MAJOR_MINOR`/`GO_VERSION_PATCH`，并只扫描带有 `GO_PKG` 或 `golang/host` 的 OpenWrt package Makefile 目录下的 `go.mod`。`vendor`、测试、示例和生成目录会被跳过；已存在的模块若声明了高于工具链的 `go` 版本，准备立即失败。检查发生在临时 staging 树切换为 `current` 之前，因此失败时上一份可用快照仍然保留。远程源码压缩包在后续 `make download` 阶段才获取，未出现在准备树中的模块不会被预扫描。

`prepare_source()` 在临时 staging 目录完成源码检出、feed 检出、feed install、必要补丁和 catalog 扫描，全部成功后才切换 `current`。authoritative catalog 会保存为快照旁的 `catalog.json`；发布前清除 `tmp`、`staging_dir`、`build_dir`、`dl`、feed 临时索引和 `feeds/base` 准备阶段链接，避免把宿主准备环境的绝对链接带入后续 Docker 构建。构建/Web 通过 `PreparedSource.catalog_path` 读取已生成目录，不对清理后的 source 重新扫描。任一步骤失败，旧的 current 和 catalog 保持可用。`prepare_feeds_sync()` 提供给首次 Web 异步初始化、手动更新和北京时间 04:00 调度；它接受状态回调，但不持有 Web 事件循环。

## Catalog 与插件选项

`scan_catalog(root)` 优先使用 OpenWrt 生成的 `tmp/.packageinfo` 和 `tmp/.config-package.in`：前者是 package metadata 的权威清单，后者包含 package 与其 Kconfig 子选项（通常来自 `luci.mk`、`include` 或 `Config.in` 的宏展开）。只有生成文件不存在时，才退回实际 package Makefile 的 `define Package/<name>` 元数据和 package config 块，并明确标记 `authoritative=False`。它不会把 `.config` 中所有 `CONFIG_PACKAGE_` 行当成包，也不会扫描 target/toolchain 的底层 Kconfig 菜单。一个包不存在或没有 metadata 时不会凭配置文件猜测其存在。

包配置选项保留 Kconfig 的类型（bool、tristate、string、int、hex、choice 等）、prompt、default、depends on、select、range、help 和 choice 关系。UI 只展示 catalog 里的 package/plugin 与这些包自己的 config；原生 `make defconfig` 产生的 `.config` 是最终权威。

## 配置校验结果

`validate_fragment()` 返回结构化 issues，不吞掉失效配置：

* `unknown_package` / `unknown_option`：当前 snapshot 没有相应 package 或 package 子选项；
* `invalid_value` / `invalid_type`：值不符合 catalog 的 Kconfig 类型；
* `auto_dependency`：defconfig 自动加入的依赖；
* `cannot_remove`：请求 `n` 但 defconfig 仍保留 `y`/`m`；
* `choice_ignored`：请求的 choice 选项未成为原生结果。

在没有原生 defconfig 结果时只能给出静态 metadata 校验，并返回 `authoritative=False`；不能把静态结果冒充最终成功。`validate_with_defconfig()` 在隔离 build 目录中运行 `make defconfig`，命令错误直接成为失败结果。

## Worker 最小数据结构

```text
DeviceSpec(key, aliases, source_id, config, profile, packager)
PreparedSource(source_id, snapshot_id, path, catalog_path, source_commit, feed_commits)
BuildRequest(task_id, device, snapshot_id, packages, options, jobs, reuse_cache)
BuildResult(artifacts, config_path, manifest)
```

`jobs` 是服务端校验后的正整数，范围为 1 到运行 Web/worker 的有效逻辑核心数；
实现优先读取进程 affinity，再按 cgroup CPU quota 限制，最后回退到
`os.cpu_count()`。`reuse_cache` 是是否复用该设备编译缓存的布尔值，默认开启。
Web API 为兼容浏览器协议仍使用 `parallel_jobs` 字段，队列适配器把它转换为核心
DTO 的 `jobs`。只有最终 `make` 编译阶段使用 `-jN`，defconfig 和 download 保持
串行。

编译工作树按规范设备保存为 `workspace/cache/builds/<device>`，同设备由 flock
串行访问，元数据原子替换。新源码快照会同步到现有树，同时保留 `build_dir`、
`staging_dir` 和共享 `cache/dl`；失败或取消在完成前保持 `ready=false`。没有复用
或设备缓存不存在时，估算值优先采用同设备历史占用，其次采用其他编译树的保守最大
值，完全没有样本时使用 8 GiB。空间不足时按创建时间 FIFO 淘汰其他缓存；每个候选
删除前都要取得其设备 flock，锁忙则跳过。旧版本的 `builds/<task-id>/openwrt`
只有在 result/manifest 提供完成状态、设备和源码快照且所有绝对链接都能验证/修复时
才原地迁移，否则保留任务证据并记录跳过原因。

Web 层可以只依赖 `DeviceCatalog.resolve()`, `SourceManager.prepare_source()`, `SourceManager.get_snapshot()`, `scan_catalog()` 和 `validate_with_defconfig()`；不需要知道 git、feed 目录或 Kconfig 解析细节。
