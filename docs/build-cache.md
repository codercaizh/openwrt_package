# ImmortalWrt master 构建缓存

GitHub-hosted Runner 每次从独立文件系统启动。活跃分支的七种设备使用官方 ImmortalWrt `master`，定时矩阵在北京时间 23:00 启动。`build.yml` 也支持单独选择 N60 Pro 等设备。新缓存只在 `codercaizh/openwrt_package` 的 `refs/heads/immortalwrt-master` 上启用。

## 路径和所有权

| 层 | Runner 中的内容 | 写入者 |
| --- | --- | --- |
| Source | `.owrt/cache/source-workspaces/<source_id>/snapshots/<snapshot_id>/{manifest.json,catalog.json,source/}`。包括 upstream `.git`、feeds、项目补丁和构建输入 | 同 source ID 的成功任务 |
| Host | `.owrt/cache/host-tools/master/{build_dir,staging_dir}/host/` | N60 Pro |
| Toolchain | `.owrt/cache/builds/ci-device/openwrt/{build_dir,staging_dir}/{toolchain-*,hostpkg}/` | 对应的规范设备 |
| Downloads | `.owrt/cache/dl/` 中的非空普通下载文件及 SHA-256、字节数清单 | N60 Pro |

Runner 内部设备工作树的物理路径固定为 `/workspace/work/cache/builds/ci-device/openwrt`。OpenWrt 的 prepared 指纹会包含文件路径和 mtime，CMake 也会写入绝对路径。每个 Runner 只有一台设备。交叉工具链靠缓存 Key 和 manifest 中的规范设备、目标及 ABI 隔离。

源码快照独立于设备编译树。缓存恢复后先验证输入指纹和 Git 状态，再复制快照到编译树。已有编译树按文件内容和模式同步，保留相同文件的 mtime。对于旧的源码祖先，Git 更新发生在快照的私有副本中，重新应用项目补丁并按固定的 commit 准备 feeds。重生成的相同文件继承原 mtime。下载文件、编译目录、staging、临时目录、日志和固件产物没有进入源码缓存。源码中 Git 跟踪的下载种子由 Git 对象还原并验证。

本地 CLI 和 Web 使用原来的设备目录 `cache/builds/<device>`、共享下载目录和设备锁。工作区容量不足时，编译缓存仍按已完成构建的创建时间清理；任务证据和固件产物由各自的保留策略管理。

## Key 和 manifest

```text
owrt-source-v3-linux-x64-immortalwrt-master-mediatek-<builder SHA>-<upstream SHA>-<identity>
owrt-host-v3-linux-x64-master-<builder SHA>-<upstream SHA>-<identity>
owrt-toolchain-v3-linux-x64-netcore_n60-pro-<builder SHA>-<upstream SHA>-<identity>
owrt-dl-v3-linux-x64-master-<builder SHA>-<upstream SHA>-<identity>
```

每层先在 `.owrt/cache/transfer/<family>.tar.zst` 生成独立的 POSIX pax 归档，保留纳秒级 mtime、软链和权限。恢复时先检查每一个 tar 路径、链接和归档校验，再在独立目录解包。manifest 记录 schema 3、仓库和分支、两种 Git SHA、当前输入身份、源码 ID、下载或编译状态、上游 URL、feeds 和外部包 pins、补丁指纹、Docker 环境、目标设备、Run ID 和 Attempt。源码层另校验源码树及 catalogue 的摘要。Host 和 Toolchain 层记录构建输入指纹和对应 mtime、以及 `defconfig` 后的配置指纹。恢复 mtime 需要对应源文件指纹相同。

Host 在 `tools/` 和公共构建输入一致、Docker 软件包版本和运行模式一致、Host 相关 Kconfig 一致时复用。只改 `tools/llvm-bpf/` 会清理该工具和已知依赖它的 bindgen。公共 Make 输入或其他 Host 工具变化会冷建共享 Host 层。Toolchain 检查设备、目标、构建环境、交叉编译输入和实际 ABI 配置；hostpkg 另外检查准确的源码快照和完整配置。ARM 打包使用特权容器，和普通容器的运行模式不同，因此会使用其自身工具链或重新构建不兼容的 Host 工具。Host 和交叉工具链在完成缓存构建时发布，普通源码更新保留兼容层。

先通过 REST 查询指定分支和缓存系列，再分别比较 builder 仓库与 ImmortalWrt 仓库的 Git 祖先关系。候选必须同时是两条引用的祖先。唯一最靠后的候选才可尝试恢复。若同一版本有多个输入身份，优先其中与这次 pins 对应的候选；分叉或版本不可比较时冷建。读取时传完整 Key，不使用前缀回退。feeds 和外部包提交在准备前固定，第二次验证 Runner 使用第一轮完整 pins。

## 发布和容量

每个缓存独立检查。固件编译和打包成功后，准备已校验的归档，再读取当前 builder 和 upstream 分支头、查询对应缓存列表及额度。已存在相同 Key 时幂等跳过。若构建已离开任一有效历史、出现已发布的新后代、同一 Git 版本的 feeds 已变化且存在另一身份，跳过写入。分支前进后仍在两条有效历史且当前没有新版对象时，可以保存该较旧的不可变对象。

检查和上传之间允许有另一个 Runner 完成上传。A 和 B 的 Key 独立，Cache API 的不可变对象确保 A 无法覆盖 B。后续的祖先选择会优先 B，即使 A 后到。若需要保证 A 连旧对象也不能创建，需使用串行 Publisher，把校验、比较和保存放在同一独占窗口，并通过单独的传输把构建产物交给它。当前实现保留并行编译及只读仓库 token。

单层压缩归档上限分别为 Source 500 MiB、Host 1900 MiB、Toolchain 1600 MiB、Downloads 2100 MiB。上传前再次查仓库用量。默认发布预算 8704 MiB，可用仓库变量 `OWRT_CACHE_BUDGET_MIB` 调低。预算与 GitHub 的实际配额独立，并发检查也没有原子容量预留。预算或 Runner 磁盘空间不足时跳过相应缓存写入，GitHub 管理已有对象的过期与淘汰。日志记录压缩字节数、REST 确认大小、恢复原因及下载增量。查看 [仓库缓存清单](https://github.com/codercaizh/openwrt_package/actions/caches)。

旧 Host/Toolchain 对象保留以便回滚。v3 优先恢复共享 Downloads，未命中时可只读恢复旧 LLVM 归档，使用当前 LLVM 配方的 SHA-256 验证后才复用。其他从旧源码身份继承的下载文件在 `make download` 时按当前配方的哈希重新核验。N60 Pro 会把它实际下载的 LLVM、Linux、工具链和包归档合入共享层；其他设备缺少的目标专用下载在各自 Runner 取得。

## 验证和回滚

`Config Review Build` 可通过提交消息中的 `[verify n60pro cache]` 运行两轮独立的 N60 Pro 构建。它使用同一 builder、upstream 和 feeds pins，第一轮成功后才启动第二轮。日常可用 `OpenWrt Build` 工作流手动构建 N60 Pro。诊断证据包含 `CACHE_BASELINE`、各层 `CACHE_RESTORE`、`CACHE_CHECK`、`CACHE_STAMPS` 和 `CACHE_MAKE`。比对 LLVM C/C++ 编译计数、stamp、下载和编译耗时，以及工具链实际重编证据。

紧急停用 v3 可将仓库变量 `OWRT_DISABLE_V3_CACHE` 设为 `1`，工作流将使用普通构建入口。需要恢复旧版 GitHub 缓存行为时，可回滚本代码提交并使用原有的 v1/v2 Key。以上操作无需删除缓存对象。
