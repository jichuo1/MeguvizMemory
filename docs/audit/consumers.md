# 真实消费者审计（2026-10-30，Asia/Shanghai）

结论：BIL 已包含 P1-07 准备 scratch 和 P2-05 帧账本计数优化，不能将其重新记为 MeguvizMemory 收益；packed reader 尚未进入所检查的 main 源码。真实 DexMeguviz 已有带寿命回调的 borrowed source C ABI，适合作为不改动现有项目的独立消费者接入口。后续定位确认 Compute/Media 的真实源码和安装 SDK 位于 `MEGUVIZ_PACK_WORKSPACE`；初次顶层路径未发现不能解释为 SDK 缺失。Memory 的家族桥本轮尚未实现或验证，不能用已有 SDK/研究计划宣称真实集成。

## 读取范围与操作边界

读取了 research/2026-10-28/PROJECT_SOURCE_MANIFEST.json、ENGINEERING_PLAN.md 的模块、消费者和量化章节，以及以下真实仓库的工作协议、当前代码和构建文件。只读访问外部仓库；审计阶段新增本报告，后续 Memory 内消费者实现和统一构建由独立回执记录。审计没有运行其他仓库的构建、安装、设备命令、全局 Git 配置、checkout、reset、stash、发布或网络变更。

跨用户 Git ownership 检查最初拒绝读取；后续使用精确的单次参数 `git -c safe.directory=BIL_WORKSPACE -C BIL_WORKSPACE ...`，其他仓库同理。没有修改全局 safe.directory。Git 提示当前用户不能读取 Administrator 的全局 ignore 文件，因此 status 的忽略口径可能与用户交互终端不同；跟踪文件修改仍可见。

## 当前来源锁

| 仓库 | 当前分支/提交 | 工作区观察 | 当前证据边界 |
|---|---|---|---|
| BIL_WORKSPACE | main / 00572940c354fbec7b6f311d4ecd6f02e66ea7c5 | docs/follow-feed-integration.md 已修改；.ai-memory/、.claude/、runtime/ 未跟踪 | 已读当前源码；未重新执行 Gradle、宿主或设备验证 |
| DEX_WORKSPACE | main / unborn，无有效 HEAD | 实现源码、测试和工程资料均未提交 | 公共头和实现真实存在；不能用 commit 锁定，接入前需源码清单/hash |
| D:\MeguvisDexDash | codex/m2-facts-20261028 / 7bb63898e610bf8436e62aa072f43fed7453d14c | 本轮 `status --short` 无输出 | 现有 Rust CLI；未读出 Memory allocator 注入合同，不优先引入语言桥 |
| D:\MeguvizAndroid | main / 18c61debbab804d4ac22e1d578d68957c5ca2205 | 大量状态、回执、集成和制品目录 dirty/untracked | 源码与本地发布记录存在；新 Memory 接入和本次安装状态未核 |
| D:\LumenCoacervationEngine | main / 0eab3d6aeda19d0a810b89350d35038acbeb6ae7 | .ai-memory/、.codebuddy/ 未跟踪 | 与研究 7e473d6473dfa938518deddb4306ad8f6969ac64 不同；未接管其视觉存储 |
| MEGUVIZ_PACK_WORKSPACE\MeguvizCompute | main / 7581256f1b35b9875b168717e8765a127e30890f | CMake、公开头、core runtime、STATUS、依赖锁等大量 dirty | 已读真实 C ABI/CMake 和本地安装 SDK；本次 Memory 桥未实现 |
| MEGUVIZ_PACK_WORKSPACE\MeguvizMedia | main / 4ebbfcd4b56a67e2ea14a6f30d614fe68ab1c765 | CMake、公开头、core、JNI、RIFE adapter、STATUS 等大量 dirty | 已读真实 frame/effect/lease 接口与 ARM64 安装 SDK；不能把旧制品视为当前 dirty 源码构建 |

研究包的 BIL 基线是 74d78e19095baa006b1315001190241952f1b84a。该提交当前仍出现在 MeguvizAndroid 的 BIL baseline worktree；原始 BIL main 已明显前进。`git worktree list --porcelain` 也显示多个 Claude 性能、Codex Android/Ref 集成及历史验证 worktree。本轮全部保留，没有假定其未提交变化已经验收。

## BIL：P1-07、P2-05 与计时边界

证据：

- `Bilibili_Innocent_Lab/app/src/main/java/com/Bilibili_Innocent_Lab/xposedmodule/hook/feature/interpolation/FrameScratchArena.kt:19`：会话内五槽 direct buffer，allocator 默认 `ByteBuffer::allocateDirect`，容量只增不减；最大单槽 4096×2160×3 字节。
- 同文件 `:45` 起：scratch 明确 position=0、limit=requested、BIG_ENDIAN；不足时按 capacityHint 申请，旧槽先置空。该做法仅释放 Java 引用，不证明旧 backing 已被 GC，因此增长共存峰值仍需实测。
- 同文件 `:72` 起：zeroed 显式初始化有效范围并统计 zeroedBytes；close 清空 slots，不承诺即时归还系统。
- `FrameInterpolationEngineHolder.kt:183–204`：warm 与 capability 共用同一 arena；按本次最大尺寸提供容量 hint；finally 中记录计数并 close。
- `FrameInterpolationEngineHolder.kt:252–262`：warm 使用两个零输入和一个输出，started 在取 scratch 与清零之前，warm 计时仍包含分配和初始化。
- `FrameCapabilityProbe.kt:34–60`：四份独立输入有效区间和单独输出；清零在单次推理计时之前；预运行一次、计时五次；任一步非 OK 则本档失败；排序后 `times[1]` 是第二快，未改为中位数。
- `src/contractTest/.../FramePreparationScratchContractTest.kt:13–77`：明确写入 P1-07，并锁定上述接线、失败处理、第二快定义及关闭。存在测试不是本轮重跑通过。
- `git log -- <FrameScratchArena.kt>` 显示 e630ad56 `perf(frame-engine): 准备会话内复用独立 direct buffer，能力测量逐步检查状态`。
- `FrameBufferLedger.kt:31–45`：五类 holder 各占 4 位，一个 Int 保存，不再每 acquire 构造 holder HashMap；`:89–109` 保留 retain/release 与在途回收。
- 对应历史提交 061360f5 `perf(frame-engine): 帧账本持有者计数打包进一个 Int，不再每帧分配 HashMap`。

因此 MM-011 应记录“已有 A1 实现，需本轮验证/设备测量”，而非重新开发并计库收益。任何 native Memory 迁移会改变 Java direct backing/owner/清零与潜在复制，A2−A1 必须称迁移与接入总差，不能隔离为纯 ABI 税。FrameBufferLedger 仍是帧资源唯一 owner，不应另建 Memory 帧池替换在途语义。

## BIL：packed reader 尚未进入当前 main

- `hook/feature/danmakuwire/DanmakuWire.kt:56–95` 的 fromFlattened 仍创建 `ArrayList<ElemSignal>` 并逐元素构造数据对象；原生 flat IntArray 原本已存在。
- `hook/feature/DanmakuPurifyFeatureInstaller.kt:444` 仍调用 `DanmakuWireNative.scan(bytes, fields)?.let(DanmakuWire::fromFlattened)`。
- 同文件约 `:407–438` 保留 presence 判据、`!changed` 快路、rewrite、wire rescan、条数/改色复检和 host parseFrom。
- `engineScan` 的当前合同：native 不可用时才用 Kotlin oracle；native 已可用但拒绝时不重新尝试 Kotlin。packed 改动必须保留该区分。
- `git log --all --grep=packed --grep=P1-07 --grep=scratch --grep=P2-05 --regexp-ignore-case` 只发现 scratch/ledger/GL 相关提交，未发现可确认 packed 已完成的提交。此结果不证明每个其他 worktree 的未提交文件都不存在该改动；未读取其他聊天消息。

MM-010 可以独立于 Memory 推进，但它的对象构造削减和已有 !changed 快路不能计作 Memory 集成资格。若原生 allocator 不是这里的剩余热点，可以不接此路径。

## 实际构建与制品线索

BIL 当前可执行配置：`app/build.gradle.kts:188` NDK=30.0.16248370，`:536–569` 使用 MeguvizRef/UI/Android、DexKit 和 Lumen；`gradle/libs.versions.toml:5–8,24` 声明 Ref/UI/Android 1.0.0、DexKit 2.3.0、Lumen 1.5.6。声明版本不等于本轮解析结果，本轮未运行 dependency resolution。

`app/src/main/cpp/CMakeLists.txt:1–16` 使用 CMake≥3.31、C++17、ncnn 固定 20261020 archive 和 SHA256；`:20–26` 构建 JNI 共享库并显式链接 16KB max-page-size；弹幕另有 Rust static archive 和独立 JNI DSO。Memory 应提供静态 PIC 核心或清楚边界的独立 DSO，不携带第二份 ncnn runtime。

`app/build/outputs/apk/debug/app-debug.apk` 在本轮只读列举时存在，25,225,440 字节，文件时间 2026-10-30 00:07:32（本机显示）。这不是本轮构建或安装证据，也未证明与当前 HEAD 完整对应。

MeguvizAndroid `docs/release/PRODUCTION_READINESS.md:56–60` 记录 1.0.0 GitHub source/maven 发布与独立消费者历史结果；`verification/publication/manifest.json` 却是更旧的 0.1.0-SNAPSHOT、device/release blocked 清单。不同时间/范围的记录不能合并为本次全通过。当前 Memory 没有实际平台桥，不能据此勾选 MM-015。

## 独立真实消费者候选：DexMeguviz borrowed source

现成接口直接可用：

- `DEX_WORKSPACE\include\dexmeguviz\dexmeguviz.h:107–116`：dmg_buffer_lease 含 struct_size、abi_version、context、retain、release、immutable、reserved；回调要求 noexcept、thread-safe，并保持字节不可变。
- 同头 `:371–374`：dmg_snapshot_open_copy_in 与 dmg_snapshot_open_borrowed 是不同存储合同；` :404–416` 有 snapshot/result retain/release 和借用视图寿命。
- `src/c_api/source_input.cpp:115–120`：检查有效参数/预算后，在 prepare/发布 snapshot 前 retain；失败路径由源对象释放借用。
- `tests/native/source_lifetime_tests.cpp:121–156`：现有测试覆盖调用方先释放、result 跨 snapshot 保活、最后 result 释放归零、损坏输入和 digest mismatch 的 retain/release 配对、immutable=0 与预算拒绝。这些是已读测试内容，不是本轮执行结果。
- `CMakeLists.txt:1–23`：C++20 core 静态库、CLI、Threads 和本地 miniz；`cmake/dependencies.cmake:1–12` 无网络下载。现有工程没有所见的 find_package/install 导出，不应虚构已安装 Dex SDK。
- `build/host-debug/dexmeguviz.exe`、`build/host-debug/Debug/dmg_miniz.lib` 等存在，但当前源码有并发更新，不应未经哈希和匹配检查直接复用历史二进制。

建议：在 Memory 工作区建立独立 consumer 工程，从安装后的 Memory package 导入 mgmem C ABI；为真实 DEX 字节申请 backing 并加载输入；通过一个明确寿命的、原子 refcount 的适配 context 把 Memory lease 接到 dmg_buffer_lease；只在最终 SDK retain 释放后释放 Memory owner。调用真实 DexMeguviz 快照/查询并完整消费结果，同时检查 Memory live/retiring/drain。不要让回调抛异常或在 close 超时后强行释放。Dex 源码可作为明确只读外部源码依赖、二进制目录放在 Memory 工作区；该接入不是“安装了 Dex SDK”。缺版本化源码时先建立参与编译文件 SHA 清单，并在构建前后核一致。

此消费者处理解析/查询，区别于图像工作区，可作为 MM-014 的真实候选；只有实际从已安装 Memory 制品运行后才满足资格。外部现有 miniz 是 Dex 的既有依赖，不能因此给 Memory core 增加新生产依赖；接入时仍需按用户生产依赖确认边界决定是否复用整个外部项目。

初读 hash（后续构建前须复核）：

| 文件 | SHA256 |
|---|---|
| BIL FrameScratchArena.kt | ff87f8347f04ad880ad93718478fcd9d7b7d3b1140fb76408bd38c91dcdab17f |
| Dex public header | c0ef3f65ba0900f5c4e9ab6159a107b3d7cf80f49b73697acc7f006a58abda7f |
| Dex source_input.cpp | 8a97781b36a3b622878606fbf150063e48874ee56ea11a0bcb53413343a9e822 |
| Dex CMakeLists.txt | 45a9e60efe1ad7041068161206d34a34c99680b3690f4e0079d651ffff7ba076 |

## 当前不能宣称的结论

审计本身没有取得本次 Android 设备能力、PMU/PFN、16KB loader、LSPosed/宿主、Memory 安装消费者、正式性能或能量数据；Memory 后续构建/消费者结果请读对应统一验证回执。本报告未操作设备。Compute/Media 的真实 SDK 和 lease/retirement 源码已定位，但未核完整设备资格，未发现当前公开头提供外部 CPU scratch allocator 注入点，不能把研究接口或 fixture 当实际家族集成；GPU resource 释放继续由原 owner 完成。其他聊天的状态未直接读取，只核可访问 Git refs、文件和历史回执。所有性能目标及研究模型仍是条件模型，不填 LOCAL_MEASURED。

## 后续纠正：MeguvizPack 中的真实 Compute/Media

定位后只读核查了 `MEGUVIZ_PACK_WORKSPACE\MeguvizCompute` 与 `MEGUVIZ_PACK_WORKSPACE\MeguvizMedia`，两者各自是 Git 仓库，MeguvizPack 外层不是仓库。初次只探测 `COMPUTE_WORKSPACE`/`MEDIA_WORKSPACE` 的范围不完整；这两个路径不存在并不构成外部制品缺失阻塞。已知另一会话在推进这些项目和 GPU 设备工作，本轮没有改动、重建、安装或争用其设备。

实际公共接口：

- `MeguvizCompute/include/meguviz/compute.h:62–107`：usage=0 为显式 host storage，ResourceDesc 由 Compute 描述并创建资源；RuntimeDesc 的预算只覆盖注册 resource range，排除私有 ncnn/cache/driver 成本。
- 同头 `:110–127`：MgcLeaseDesc 持有 content_context/retain_content/release_content，content 寿命独立于 allocation 引用。
- 同头 `:282–301`：真实 mgc_resource_create、lease create/retain/release、map/unmap、wait_ready、publish_read；map 指针借用到 unmap，明确不能自行 free。所见公开头没有外部 CPU provider vtable 或接受任意 Memory backing 的 host-scratch import API；不能把 content_context 回调误称为 allocator 注入点。
- 同头 `:311–314`：session drain 覆盖已受理执行、host finalize、回调和提交借用 lease，消费者持有的输出 lease 在其范围外继续有效。Memory 的 CPU collect/drain 不能替代该 retirement 合同。
- `MeguvizCompute/include/meguviz/compute_android.h:43–71`：AHB 引用、fd 复制、内容 lease、外部 READ/WRITE reservation 与 fence 配对均有原始 owner；不能由 Memory 接管 AHB backing。
- `MeguvizMedia/include/meguviz/media.h:42–68` 的 MgmFrameDesc 以 MgcLease* content 引用像素，`:109–112` 的 frame_create retain content；`:120–126` 的 effect 接受输入转移、回调输出和 drain/release 合同真实存在。

实际构建与安装制品：

- Compute `CMakeLists.txt:151–187` 建立静态 PIC core、`MeguvizCompute::core`，按选项导出私有 ncnn SDK，并有实际 install/export。
- Media `CMakeLists.txt:11–23,58–63` 优先从安装包 find_package Compute，导出 `MeguvizMedia::core`；既有 composition 可提供单一 Compute/ncnn 闭包。
- Media `distribution/android/CMakeLists.txt:8–52` 的官方 composition 明确使用单一最终 `meguviz_media_rife_ncnn` DSO、静态 STL，并整合 Compute/RIFE/Media；该代码仍有本地 dirty 变更，本轮只读，不代表重建资格。
- `MEGUVIZ_PACK_WORKSPACE\MeguvizMedia\artifacts\M6\identity-sdk\android-arm64-v8a\install` 真实存在 Config、Targets、公开头以及 `libmeguviz_compute_core.a`（3,580,680 B）、`libmeguviz_media_core.a`（703,824 B）、`libmeguviz_media_android.a`（495,654 B）。对应 armv7 Config 也存在。
- 所读 ARM64 identity-sdk 的 MeguvizComputeConfig 中 ncnn 条件为 OFF，私有 fingerprint 为空；MeguvizMediaConfig 的 RIFE 条件为 OFF。因此它是实际 CPU/reference identity profile，不能充当完整 ncnn/RIFE GPU SDK 资格。
- Compute STATUS 当前 profile 为 bil-rife-ncnn-android、IN_PROGRESS/INCONCLUSIVE；Media STATUS 为 BLOCKED、bil_qualified=false、gpu_qualified=false。它们保留旧构建和设备记录，也指出当前候选需重建/设备验收；本报告不把这些记录合并成当前全部通过。

本次只读固定值（活跃外库后续可能变化，重新接入前必须复核）：

| 输入 | SHA256 |
|---|---|
| Compute 当前 compute.h | fd35292124d2a385c32e364c1aafbe7a0b7739c0d1d1d1ab20a560ef9d9792cc |
| Media 当前 media.h | 84b32e6f9840b1f96e77958593e7f630f66c054152252c898e373fd91e05a253 |
| identity-sdk ARM64 libmeguviz_compute_core.a | 73ac4e40124e0f78e389ae9262f86efe88e23f3617e783bd2fdddef09f584bb6 |
| identity-sdk ARM64 libmeguviz_media_core.a | 8e7c958107334a71b3fa9c180f9d63cc4434ac2677a461bdc9bb4e0030de32ca |

MM-016 的实际下一步是固定并确认可用 CPU scratch adapter 合同及最终 SDK profile，保留 origin-owner 与唯一 backing 记账，然后在不争用另一会话资源的环境接入与验证。当前阻塞不是“没有任何 Compute/Media SDK”，也没有足够证据直接将现有 GPU/frame 资源搬入 Memory。
