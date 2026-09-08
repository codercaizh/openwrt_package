# OpenWrt Builder

这是一个面向个人使用的 OpenWrt 固件编译工具。宿主机只需要 Docker
（以及 Docker Compose，使用 Web 控制面时需要），源码、feeds、编译环境和
产物都由工具管理。

目前支持四个 ARM 设备：

| 设备 | 别名 | 源码目标 |
| --- | --- | --- |
| `360t7` | `360-t7`、`qihoo_360t7` | MediaTek Filogic |
| `netcore_n60-pro` | `n60pro`、`n60-pro` | MediaTek Filogic |
| `s905d` | `n1` | armsr/armv8 |
| `vplus` | `h6-vplus` | armsr/armv8 |

设备、源码分支和默认插件配置集中在
[configs/devices.toml](configs/devices.toml) 与 `configs/*.config`。构建时不
接受用户输入 commit；工具会检出配置中声明的分支，记录实际源码和 feed
提交，并将准备好的不可变快照用于本次构建。

## 命令行和 GitHub Actions

最简单的本地构建命令是：

```bash
./owrt build n60pro
```

也可以使用规范设备名：

```bash
./owrt build netcore_n60-pro
```

宿主机有 Python 时，CLI 直接调用本项目；没有 Python 时，`owrt` 会自动构建
`owrt-builder:local` 并在 Docker 中运行相同的 CLI。编译本身始终在完整的
builder 镜像中执行。可用命令：

```bash
./owrt devices       # 列出设备和别名
./owrt doctor        # 检查 Docker 与目录配置
./owrt build n60pro --json
# ./owrt build n60pro --jobs 4 --no-reuse-cache
```

`--jobs` 必须是 1 到当前进程可用逻辑核心数的整数；省略时自动使用该上限。
`--reuse-cache` 是默认值，`--no-reuse-cache` 会先清理并重建该设备的编译缓存。
`make defconfig` 和 `make download` 保持串行，只有最终编译阶段使用 `make -jN`，
因此命令行和 GitHub Actions 不需要设置 `OWRT_JOBS`。

编译结果、manifest、配置和日志默认写入 `.owrt/`。manifest 记录源快照、配置
SHA256、产物大小和每个产物的 SHA256；设备别名和规范名使用同一个规范设备
目录，便于 Action 上传。

`.github/workflows/build.yml` 的 `workflow_dispatch` 只需要选择 `device`。
GitHub runner 会调用与本地相同的 `./owrt build <device>`，并上传
`.owrt/artifacts/**/*` 和构建证据。

## Web 控制面

首次启动前复制环境模板并填写绝对路径：

```bash
cp .env.example .env
# 修改 OWRT_REPO_HOST_PATH、OWRT_DATA_HOST_PATH、OWRT_PUBLIC_ORIGIN
./owrt web -d
```

首次部署创建本地管理员。命令会交互式读取密码，不会把密码放进 shell 历史：

```bash
docker compose run --rm web python3 -m owrt_builder.admin create-admin admin
```

页面启动后会异步准备两个本地 source/feed 快照，页面可以显示插件来源、简介、
默认选中项和 package-local 子选项。feeds 可以手动刷新，也会每天北京时间
04:00 自动刷新；刷新失败不会替换上一份可用快照。选择设备后，工具自动加载
该设备的内置默认配置，用户可以增减插件、编辑子选项并提交任务。任务队列、
实时 SSE 日志、取消、历史任务、manifest 和下载产物都需要管理员登录。

提交构建时可以设置“并行核心数”，默认使用 Web 服务所在主机的系统逻辑核心数，
取值范围为 `1` 到该上限；服务端会用严格整数类型再次校验，不能用布尔值、小数
或字符串绕过限制。“复用该设备编译缓存”默认开启，关闭后本次任务会要求编译器
清理该设备的可复用缓存。任务历史会保存 `parallel_jobs` 和 `reuse_cache`，旧的
数据库记录会按 `1` 和 `true` 兼容读取。`GET /api/devices` 的 `runtime` 字段返回
`logical_cpus`、`default_parallel_jobs` 和 `max_parallel_jobs`，供网页初始化输入框。

公网部署时应让 Caddy 或其他反向代理提供 HTTPS，并设置严格的
`OWRT_PUBLIC_ORIGIN`。如果公网入口会使用变化的 IP 或多个域名，可以显式设置
`OWRT_ALLOWED_ORIGINS` 为逗号分隔的 HTTP(S) Origin，或设置为 `*` 接受任意合法
HTTP(S) Origin；通配符不会绕过会话登录和 CSRF token 校验。Web 容器需要访问 Docker socket 来启动隔离的编译容器，
因此应将该服务只暴露给可信管理员，并保管好管理员密码。PushPlus 是可选的：
填写 `PUSHPLUS_TOKEN` 后，成功、失败、取消或中断结果会异步推送到微信；通知
失败不会改变编译结果。

Compose 默认让 Web 只监听宿主机回环地址，并由 Caddy 接管 80/443。需要直接从
`8000` 访问时，设置 `OWRT_WEB_BIND_ADDRESS=0.0.0.0`、对应的 HTTP
`OWRT_PUBLIC_ORIGIN` 和 `OWRT_SECURE_COOKIE=0`，然后只启动 Web 服务：

```bash
docker compose up --build -d web
```

这样不会自动启动 Caddy；使用 HTTPS 反向代理时恢复模板中的回环绑定和安全 Cookie
设置，并启动 `caddy` 服务。

## feeds、配置和产物

本项目保留原有的源码分支选择、Kenzo、rtp2httpd、cloudflarespeedtest、
Passwall packages/LuCI 和 Go 26 feed；Passwall 跟随配置的默认分支，不再固定
旧 commit。Rust 兼容补丁只在检测到对应版本时应用。feeds 位于工作区的源快照
和缓存目录中，不直接修改仓库源码。

配置校验优先使用 OpenWrt 原生生成的 `.packageinfo` 和
`.config-package.in`。插件子选项的符号可能不是 `CONFIG_PACKAGE_` 前缀，工具
使用生成 catalog 的明确归属来校验和写入，不依赖字符串前缀猜测；提交前还会
在隔离目录运行原生 `make defconfig`。

编译下载使用 `https://sources.cdn.openwrt.org`，可通过
`OWRT_DOWNLOAD_MIRROR` 覆盖；下载缓存位于 `.owrt/cache/dl`，避免每次构建重复
拉取。构建日志默认采用 OpenWrt 详细模式，长时间没有 stdout 时会产生阶段心跳，
所以 Web 页面不会把正在工作的 host 工具误判为无响应。

编译工作树按规范设备持久化在 `.owrt/cache/builds/<device>`，任务日志、配置、
manifest 和固件仍按 task-id 分开保存。默认复用同设备缓存；源码快照变化时只替换
源码文件并保留 `build_dir`、`staging_dir` 和下载目录。缓存元数据使用原子写入和设备
锁，失败或取消的任务不会把缓存标记为可复用。首次没有样本时按 8 GiB 保守估算，
随后优先使用同设备历史占用；空间不足时按创建时间 FIFO 删除其他已完成的编译缓存，
逐次记录路径、大小、删除前后可用空间和估算需求。下载缓存、源快照、日志与产物
永远不参与淘汰。升级时可识别的旧 `.owrt/builds/<task-id>/openwrt` 会在安全校验
通过后原地迁移，绝对路径链接会改成相对链接；无完成证据或含未知绝对链接的目录
会跳过并记录原因。

ARM 的 rootfs 打包只在构建内部为 `s905d` 和 `vplus` 调用专用 packager。仓库
不再提供交互式 menuconfig、仅下载、仅打包、x86 或云服务器安装入口。
MediaTek 编译容器使用默认 Docker 隔离；只有 ARM packit 阶段按设备需要启用
受限的 `--privileged` 并以容器 root 完成分区挂载，随后自动恢复工作区的宿主
UID；编译容器不使用 host network。

更完整的模块边界和任务状态见 [docs/architecture.md](docs/architecture.md)。
