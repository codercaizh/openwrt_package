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

每层先在 `.owrt/cache/transfer/<family>.tar.zst` 生成独立的 POSIX pax 归档，保留纳秒级 mtime、软链和权限。恢复时先检查每一个 tar 路径、链接和归档校验，再在独立目录解包。manifest 记录 schema 3、仓库和分支、两种 Git SHA、当前输入身份、源码 ID、下载或编译状态、上游 URL、feeds 和外部包 pins、补丁指纹、目标设备、Run ID 和 Attempt。源码层另校验源码树及 catalogue 的摘要。Host 和 Toolchain 层记录构建输入指纹和对应 mtime、以及 `defconfig` 后的配置指纹。恢复 mtime 需要对应源文件指纹相同。

Host 在 `tools/` 和公共构建输入一致、Host 相关 Kconfig 一致时复用。Docker 软件包版本和运行模式不参与缓存指纹。只改 `tools/llvm-bpf/` 会清理该工具和已知依赖它的 bindgen。公共 Make 输入或其他 Host 工具变化会冷建共享 Host 层。Toolchain 检查设备、目标、交叉编译输入和实际 ABI 配置。hostpkg 另外检查准确的源码快照、完整配置和 Host 输入文件。Host 和交叉工具链在完成缓存构建时发布，普通源码更新保留兼容层。

先通过 REST 查询指定分支和缓存系列，再分别比较 builder 仓库与 ImmortalWrt 仓库的 Git 祖先关系。候选必须同时是两条引用的祖先。唯一最靠后的候选才可尝试恢复。同一 Git 版本有多个身份时优先匹配本次输入。Host 和 Toolchain 可以择一尝试恢复旧版归档，仍须通过文件和配置校验。分叉或版本不可比较时冷建。读取时传完整 Key，不使用前缀回退。feeds 和外部包提交在准备前固定，第二次验证 Runner 使用第一轮完整 pins。

## 发布和容量

每个缓存独立检查。固件编译和打包成功后，准备已校验的归档，再读取当前 builder 和 upstream 分支头、查询对应缓存列表及额度。已存在相同 Key 时幂等跳过。若构建已离开任一有效历史、出现已发布的新后代、同一 Git 版本的 feeds 已变化且存在另一身份，跳过写入。分支前进后仍在两条有效历史且当前没有新版对象时，可以保存该较旧的不可变对象。

固件仍并行编译。构建任务使用只读仓库令牌，将已校验的归档交给单独的发布任务。所有发布任务进入同一 GitHub Actions 并发队列，依次执行版本比较、容量检查、删除旧缓存、上传和确认。发布任务仅在可信 master 构建中取得 `actions: write`。

上传前，发布任务先清理已有唯一替代品的重复缓存，再用实时清单估算替换后的容量。新归档校验通过且容量足够时，先删除本次归档要替代的同作用域旧缓存，确认旧条目消失后再上传。同一 Git 版本的变体只有在当前 feeds 已核对时才可替换，版本不可比较的条目继续保留。上传后只核对新条目的 ID 和大小。未获确认时重试一次，仍失败则发布任务报错，保留可用于重跑的临时归档。

单层压缩归档上限分别为 Source 500 MiB、Host 1900 MiB、Toolchain 1600 MiB、Downloads 3900 MiB。Downloads 通过 tar 的 NUL 分隔文件清单生成归档，可处理大量文件，同时保留普通文件和 SHA-256 校验。默认发布预算为 9984 MiB（9.75 GiB），可用仓库变量 `OWRT_CACHE_BUDGET_MIB` 覆盖。容量计算包括所有分支的缓存，删除范围限于当前分支维护的 v3 作用域。若预计替换后仍超过预算，保留当前作用域的旧缓存并记录所需空间。GitHub 仍按其实际配额处理到期和淘汰。查看 [仓库缓存清单](https://github.com/codercaizh/openwrt_package/actions/caches)。

自动清理的范围限于本分支对应作用域的 v3 归档。v3 优先恢复共享 Downloads，未命中时可只读恢复旧 LLVM 归档，使用当前 LLVM 配方的 SHA-256 验证后才复用。其他从旧源码身份继承的下载文件在 `make download` 时按当前配方的哈希重新核验。N60 Pro 会把它实际下载的 LLVM、Linux、工具链和包归档合入共享层；其他设备缺少的目标专用下载在各自 Runner 取得。

## 验证和回滚

`Config Review Build` 可通过提交消息中的 `[verify n60pro cache]` 运行两轮独立的 N60 Pro 构建。它使用同一 builder、upstream 和 feeds pins，第一轮成功后才启动第二轮。日常可用 `OpenWrt Build` 工作流手动构建 N60 Pro。诊断证据包含 `CACHE_BASELINE`、各层 `CACHE_RESTORE`、`CACHE_CHECK`、`CACHE_STAMPS` 和 `CACHE_MAKE`。比对 LLVM C/C++ 编译计数、stamp、下载和编译耗时，以及工具链实际重编证据。

紧急停用 v3 可将仓库变量 `OWRT_DISABLE_V3_CACHE` 设为 `1`，工作流将使用普通构建入口。需要恢复旧版 GitHub 缓存行为时，可回滚本代码提交并使用原有的 v1/v2 Key。以上操作无需删除缓存对象。
