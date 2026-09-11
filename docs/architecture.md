# 源、feed、插件目录与配置接口

本文档是 `owrt_builder` 与 Web/build worker 之间的最小接口约定。源码准备和目录扫描都是同步函数；Web 层负责在线程/任务队列中调用它们，并把状态回调转成 SSE 或任务日志。

## 项目结构

仓库按运行边界分为以下几组，新增功能应优先放到拥有它的层中：

| 目录 | 职责 | 约束 |
| --- | --- | --- |
| `owrt_builder/` | CLI、Web 和构建核心 | 保持现有模块导入路径稳定；`paths.py` 统一定位仓库资源 |
| `owrt_builder/web_support/` | Web 的 DTO、catalog 适配和 Runtime 装配 | 保持 FastAPI 入口在 `web.py`；只依赖核心模块，不反向依赖路由 |
| `owrt_builder/static/` | Web 页面资源 | 通过 Python 包数据随 wheel 和 Web 镜像发布 |
| `configs/` | 设备目录和配置片段 | 只放审查过的设备/构建输入，不写入运行时状态 |
| `scripts/`、`docker/`、`deploy/` | 启动、构建镜像和部署 | 只负责环境边界，不复制构建业务逻辑 |
| `tests/` | CLI、Web、核心和部署契约 | 优先通过公开入口和注入依赖测试，避免依赖真实 Docker 或网络 |

`devices.py`、`sources.py`、`feeds.py`、`catalog.py` 和 `configuration.py` 组成源码/配置核心；`build.py` 和 `cache.py` 负责构建生命周期；`storage.py`、`auth.py`、`notifications.py` 和 `system.py` 提供 Web 所需的持久化与宿主能力；`web.py` 负责 API、队列和页面适配，`web_support/` 提供可独立测试的输入模型、目录 shaping 和运行时装配。仓库路径、设备目录、静态资源和固定补丁由 `paths.py` 提供，运行时工作区仍由 CLI/Web 自己管理。

构建核心仍保留平铺的 Python 模块名，是为了兼容 `owrt_builder.build`、`owrt_builder.web` 等已有 CLI、集成和外部脚本入口；Web 内部的新增边界放在 `web_support/` 子包中。只有当一个边界能在不增加兼容包装层的情况下独立演进时，才应进一步拆成子包。

## 设备与源码

| key | aliases | source | config fragment | 说明 |
| --- | --- | --- | --- | --- |
| `360t7` | `360-t7`, `qihoo_360t7` | `immortalwrt-mt798x` | `360t7.config` | 路由默认配置 |
| `netcore_n60-pro` | `n60pro`, `n60-pro`, `netcore-n60-pro`, `netcore_n60_pro` | `immortalwrt-mt798x` | `n60pro.config` | 路由默认配置，N60 Pro 真机验收 |
| `s905d` | `n1` | `armv8` | `armv8.config` | 盒子通用配置；`s905` 是不同设备，不作为别名 |
| `vplus` | `h6-vplus` | `armv8` | `armv8.config` | 盒子通用配置 |

源码仓库和分支是 `SourceSpec` 的常量；准备完成后以 git 实际 `HEAD` 作为 snapshot 的 source commit。接口不接受用户输入的 commit。snapshot 目录不可变，`current` 只是原子替换的符号链接；正在编译的任务只使用其创建时返回的 `PreparedSource.path`/`snapshot_id`，后续 feed 更新不会改变它。

## Feed

默认 feed 与原项目来源保持一致：Kenzo、rtp2httpd、luci-app-cloudflarespeedtest、Passwall packages、Passwall LuCI，以及原流程中的 `packages_lang_golang` 27.x 替换。Passwall 跟随仓库默认分支，取消旧的固定 commit。Tailscale 核心包采用 OpenWrt 官方 `https://github.com/openwrt/packages.git` 的 `master` 分支，仅浅克隆/稀疏检出 `net/tailscale`，原样同步到 `feeds/packages/net/tailscale`；LuCI 应用采用 OpenWrt 官方 `https://github.com/openwrt/luci.git` 的 `master` 分支，仅浅克隆/稀疏检出 `applications/luci-app-tailscale-community`，原样同步到 `feeds/luci/applications/luci-app-tailscale-community`。两个官方仓库先克隆到不会被 package scanner 扫描的 `staging/upstream` 临时目录，再在 native feeds install 前覆盖目标目录，因此继续使用当前源码 feed 的 golang/luci 基础设施，也不会把整个官方仓库作为额外 package tree。不使用个人打包仓库，也不复制或维护 Tailscale 的旧版本、配方、init/config 或 LuCI 脚本；官方包可能略慢于 Tailscale 最新 release。每次 prepare/刷新都重新获取官方 master 的最新 HEAD，并在 `feed_commits` 中记录两个官方仓库的实际提交；单个 snapshot 因而可按记录的 HEAD 复现，后续刷新则能获取官方最新内容。准备阶段在 feeds install 后只移除旧的 `luci-app-tailscale` 链接，保留官方 `tailscale` 与 `luci-app-tailscale-community` 链接。Rust 1.90.0 的替换仅在实际 Makefile 版本匹配时执行；替换文件不存在或补丁失败要报告错误。

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

构建缓存可能保留上一任务的软件包，因此构建开始前会记录
`openwrt/bin` 下所有 `.apk` 和 `.ipk` 的内容状态。编译结束后只将新建或内容
发生变化的包按其相对 `bin` 路径写入 `packages.tar.gz`；没有变化时不创建归档。
旧的 `IPK_ARCHIVE_NAME` 常量和 `ipk-packages.tar.gz` 文件名只用于读取历史产物，
新构建统一使用中性的 `PACKAGE_ARCHIVE_NAME`。

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
