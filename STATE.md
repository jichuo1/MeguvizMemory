# MeguvizMemory 工程状态

最近补充：2026-11-01。当前交付 **0.2.0 功能工程预览 / native ABI 1.1**。公开仓库为 [jichuo1/MeguvizMemory](https://github.com/jichuo1/MeguvizMemory)。正式 Release、性能资格和 BIL 接入尚未完成。

## 统一验证准备（2026-11-01）

- 新增[托管域真机诊断与冻结协议](docs/ANDROID_MANAGED_DEVICE_PROTOCOL.md)：System/Arena 的预算、合成压力、跨关闭 lease、最终账目与回调关闭采用独立 instrumentation；同一源码的宿主 JVM/JNI 加 Android 桩 2 项通过。arm64 与 x86_64 诊断 APK 已完成 R8 构建及 37 个 JNI 声明检查；arm64 后续真机短测见下，x86_64 尚无本诊断运行证据。[构建限定证据](artifacts/managed-probe-20261101/build-only.json)
- 独立 runner 已完成假 ADB 的准入、签名、冻结、超时和后验离线回归；真实冻结计划默认仅写入被忽略的 `build/`。首轮真机安装返回 `INSTALL_FAILED_USER_RESTRICTED`，诊断没有运行；后续只读观察在可见用户中未发现该包，具体取消原因未知。[脱敏记录](artifacts/managed-probe-20261101/device-install-incomplete.json) 设备观察不构成共享锁，短诊断也不能作为 900 秒资格。
- 随后重试安装并完成 [API33 arm64/4KB 的 R8 真机短诊断](artifacts/managed-probe-20261101/device-short-pass-20261101.json)：3 组、306 项检查通过，覆盖合成压力、预算和关闭，最终 backing 为 0；安装包身份和运行后核验通过。transport 耗时 0.79 秒；进程 cgroup 获得 0 个样本，`NOT_OBSERVED` 不表示未冻结。该结果不证明长稳、真实系统低内存、性能、BIL 或其他 ABI。
- 新增[Linux 宿主候选隔离 CI 入口](docs/LINUX_HOST_CANDIDATE_CI.md)，按指定提交从源构建并验证安装消费者。[首轮](artifacts/managed-probe-20261101/linux-host-first-attempt.json)、[第二轮](artifacts/managed-probe-20261101/linux-host-second-attempt.json)均失败且未上传制品；[第三轮](artifacts/managed-probe-20261101/linux-host-third-attempt.json)的受控诊断将静态库处理错误归类为未知选项，但不能独立证明根因。构建器此前解析工具符号链接可能改变 LLVM 多调用程序的模式，现保留请求的入口名称并新增回归；本机 22 项通过、1 项 POSIX 专属测试跳过，修复后的 Linux 结果见下。runner 的 34 项离线检查通过；CI sentinel 不等同用户本机私有标识扫描。
- 修复后的 `085cc13` [Linux x86_64 工程候选](artifacts/managed-probe-20261101/linux-host-candidate-pass.json)已在 GitHub-hosted Linux 构建并通过安装消费者检查：图像默认 12 组、合成压力 12 组、负例 10 组及 pressure v1/v2；43 步的 86 个日志流摘要经校验。Actions 制品下载回读的大小和哈希匹配，另存本地独立候选。CI 标识扫描不等于用户本机私有标识扫描；设备、性能和正式发布不因此取得资格。
- 独立[托管域长稳诊断与采集协议](docs/ANDROID_MANAGED_STABILITY_PROTOCOL.md)已补齐：包含已知前台身份、预算和关闭循环、停滞优先、保守采样时间和自身窗口退场检查。宿主 8 项、构建器 11 项及采集器 30 项离线检查通过；arm64 与 x86_64 的 R8 APK 已本机构建并以虚拟设备标识完成无 ADB 冻结检查。[限定记录](artifacts/managed-probe-20261101/stability-preparation.json)不代表 Android 运行、900 秒设备长稳、真实系统压力或性能资格。
- 长稳工具提交 `8939ab6` 的 [Windows/Linux CI](artifacts/managed-probe-20261101/stability-ci-8939ab6.json) 均通过，包含构建器、采集器与宿主测试；它们不是设备长稳结果。

## 修复后工程候选（2026-11-01）

- 从提交 `2e25092` 的独立源码快照分别生成本地三 ABI Android 候选和 Windows x86_64 宿主候选；两包的 261 项源码记录与该提交一致。包名、SHA-256 和[限定证据](artifacts/delivery-20261101/final-candidates-2e25092.json)已固定，候选仅留本地 `dist/`，不等于正式 Release。
- Android 的三个 AAR/SDK 内核心与 JNI 字节分别一致；真实图像、压力图像及显式 `android_jni` 目标均完成三 ABI 交叉编译和链接，arm64 AAR 完成 Android SDK 示例源码编译。该交叉编译环节没有运行 Android 可执行文件、ART 或设备。[安装消费记录](artifacts/delivery-20261101/android-installed-link-2e25092.json)
- Windows 包从归档回读后运行真实 Java 图像默认 12 组、合成压力 12 组、失败 10 组，以及 pressure v1/v2 消费者；37 个步骤的 74 份私有日志均与 journal 摘要一致。此前 `4aa2874` Windows 旧轮的覆盖记录不被改写。该阶段尚无 Linux 宿主候选，后续结果见上节。
- [最新源码 Windows/Linux CI](artifacts/delivery-20261101/ci-host-2e25092.json) 均通过；两端候选构建器的 29 项合同检查均全部执行，包括三个真实日志符号链接负例。本机 Windows 因权限跳过三项的原记录保留。上述限定 arm64 R8 短测以外的设备和 R8 运行矩阵，以及 16 KB loader、MTE、性能、BIL 和正式发布资格仍独立待验证；项目许可证尚未确定。

## 压力并发安全与 JNI 安装目标（2026-11-01）

- 修复 Android 回调适配器并发 `close()`：同线程重入继续由外层处理；另一线程遇正在执行的关闭会得到可重试异常，不会把未完成的注销或清理准入误报为成功。`isClosed()` 只表示停止事件准入。[宿主新旧反例](artifacts/pressure-races-20261101/host.json)
- 修复 pressure v1/v2 控制器并发创建时先取快照、后占附着槽导致的过期预算：现在先预占唯一附着，再读取预算；失败时在锁外归还 owner。四种版本交叉竞争的旧失败与新通过、失败回滚及回调锁序均有限定宿主证据。
- Android SDK 新增可选的 `MeguvizMemory::android_jni` 安装目标，core-only 包仍可使用；显式组件会校验架构、ELF 和同安装前缀。配置合同 32 项通过；构建器补强日志保护后，本机 26 项离线合同通过、3 项因权限跳过，随后两端 CI 的 29 项全部通过。最终源码制品见上节，运行验证仍待完成。
- 此前本地 `c45723f` 工程候选早于上述修复与 JNI 目标，不能作为修复后的新版制品。更广设备和 R8 运行范围、真实低内存和性能资格仍待统一验证。

## 图像访问轨迹工具（2026-11-01）

- 新增显式启用的[图像源码级采集与回放](consumers/image/TRACE_CONTRACT.md)：覆盖实际 P6 图像算法的 direct、System 和 Arena 路径及 full/ring 工作区。轨迹只记录相对访问与生命周期，不包含像素或原始地址；完整输入、原始轨迹和输出留在本机。
- 安装版宿主核心上的合成 P6 功能验证通过采集 6 正例、4 反例及回放 12 正例、53 反例；本机测试环境未能创建符号链接，但后续 Windows/Linux CI 的该拒绝路径均通过。[本机证据](artifacts/image-trace-20261101/host.json)、[公开 CI](artifacts/image-trace-20261101/ci-host.json)；不插桩目标不运行记录逻辑。
- 尚未取得用户业务输入或 BIL 产品访问轨迹；源码语义回放不证明 CPU 指令顺序、硬件机制或性能收益。输入文件原始 header 和实际加载映像身份也未认证，详见合同。
- 新增[宿主工程候选构建器](docs/HOST_CANDIDATE_BUILDER.md)，从独立源码快照构建核心、宿主 JNI 与纯 JVM 类；Windows 归档回读和真实图像消费者通过，21 项离线合同检查通过。基于 `4aa2874` 的旧 Windows 候选有一条原始日志被同名步骤覆盖，不能声称日志链完整；该轮保留，修复后的候选见上节。此时 Linux 分支只有离线合同检查，后续 Linux 构建证据见文件顶部。
- `4aa2874` 的 [Windows/Linux 公开 CI](artifacts/delivery-20261101/ci-host-4aa.json) 均通过；[该提交两份本地候选的回读记录](artifacts/delivery-20261101/prior-candidates-4aa.json)分别保留 Android 日志完整和 Windows 单条日志覆盖的差异。CI 不包含后续日志保护修复。

## 多域诊断与 Android 接入示例（2026-11-01）

- 新增 [MM-021 多域 Arena 诊断](bench/TLB_MULTI_ARENA.md)，以相同工作量比较两种访问顺序，检查预算、持有 lease 时关闭及最后归零；Windows 宿主 5 个正例与 8 个拒绝路径通过。它是单线程功能工具，不证明 TLB 机制或性能收益。[限定证据](artifacts/multi-arena-20261101/host.json)
- 新增[独立 Android 使用示例](consumers/android_managed/README.md)，显式处理后台注册、合成压力票据、注销与关闭；宿主 JNI 和 framework 类型桩通过，并用本批 arm64 AAR 与真实 Android SDK 编译通过。ART、真机和 R8 运行仍须分别验证。
- 新增[Android 工程候选构建器](docs/ANDROID_CANDIDATE_BUILDER.md)，该阶段从 `c45723f` 的独立源码快照构建并检查三 ABI AAR 与 SDK；14 项离线合同检查通过，独立消费者的三 ABI 交叉编译、链接及 AAR/SDK 身份回读通过。[历史候选证据](artifacts/android-candidate-20261101/candidate.json)；该候选早于后续修复，新版见上节。
- 本批 [Windows/Linux 公开 CI](artifacts/android-candidate-20261101/ci-host.json) 均通过：各自核心 39 项、托管域 49 项、多域诊断 5 正例和 8 反例，以及候选工具与 Android 示例的宿主检查。

## 托管域同域压力继续开发（2026-11-01）

- 新增显式选用的同域压力绑定：手动预算和压力事件由同一控制器仲裁；事件排队、应用、失败、取消与过期分开观察。关闭先结束压力控制器，再清理托管域；已有租约不被强制释放。Android 回调桥提供拒绝计数和最近状态，但回调路径会取短锁，不承诺硬实时。见[压力合同](docs/ANDROID_MANAGED_PRESSURE_CONTRACT.md)。
- Windows 宿主验证覆盖核心压力合同、托管绑定并发和关闭、确定性取消，以及图像消费者的 System/Arena 合成压力场景；[公开 CI](artifacts/managed-pressure-20261101/ci-host.json) 的 Windows 与 Linux 均通过核心 39 项、托管域 49 项及图像默认 12／压力 12／失败 10 组。详细源码和范围见[本次记录](artifacts/managed-pressure-20261101/host.json)。这些结果不证明 Android ART、R8 运行、真实系统低内存、设备长稳或性能收益。
- 之前的预览包和历史设备证据均不包含本次绑定；上节的新工程候选包含该绑定，但尚未取得设备或正式发布资格。设备和性能资格留待功能阶段结束后统一验证；BIL 保持未接入。

## Java 托管域上一开发阶段（2026-11-01）

- 新增纯 Java 可调用的 `ManagedMemoryDomain`、allocation 和受控 lease；自有 System/Arena 域分配时先清零，显式软预算，关闭先停止准入并在后台等待 owner、lease 和相关调用。未知结果进入隔离，不强制释放；该阶段尚未接入同域压力 worker，后续进展见上节。[托管合同](docs/ANDROID_MANAGED_DOMAIN.md)和[压力接入合同](docs/ANDROID_MANAGED_PRESSURE_CONTRACT.md)。
- Windows 与 Linux CI 均通过真实宿主 JVM 的 46 项受控故障/并发检查及 1 项无注入检查；独立 Java 图像消费者迁移到托管 API 后，两端安装共享核心与宿主 JNI 的 12 组像素对照、8 组拒绝及无诊断 sidecar 普通路径通过。[证据范围](artifacts/managed-domain-20261101/README.md)
- 新源码已完成三 ABI 交叉构建及独立脱敏候选，arm64 R8 名称/签名静态检查通过；原始 AAR 未脱敏且不可分享。当时最新接口的 R8 运行、Android ART、真机、性能和正式发布资格均待统一验证；后续限定 arm64 真机短测见本文件顶部。此前预览包不包含托管域和本次消费者迁移。

## 后续功能继续开发（2026-11-01）

- C++ RAII 新增 checked 外部导入及粘性隔离；释放结果不确定时不重试或销毁原域，保留的资源不能计为正常归零。[合同](docs/CPP_CHECKED_IMPORT.md)，限定宿主检查10项通过。
- Java 受控借用的首次关闭不再在 Java monitor 内执行原生释放，并提供非等待的关闭状态观测；宿主 fake-JNI/真实 JVM 检查通过。状态仅描述本注册，不能替代 domain 归零证据。[合同](docs/ANDROID_NATIVE_BORROW.md)
- 新增独立 Java/JNI P6→P5 图像消费者，使用安装后的共享核心和单独构建的宿主 JNI；12组像素对照及8组失败路径通过。Java 数组和 JNI 复制成本单独记录，不宣称性能收益。[消费者](consumers/java_image/README.md)
- 上一份 `Feature-Preview-20261101.zip` 不包含本节后续源码；该后续阶段的三 ABI Android 和 Windows 宿主候选已重建，但不包含之后新增的 Java 托管域。arm64 R8 诊断及关闭状态 v2 用例已有[构建和宿主模拟记录](artifacts/followup-delivery-20261101/android-borrow-v2/host-receipt-final.json)，故障域按合同保留，尚未执行 ART/R8 运行、设备、性能或发布资格。宿主证据见[记录](artifacts/continuation-next-20261101/host-dev-checks.json)，Android 构建身份见[记录](artifacts/continuation-next-20261101/android-build/metadata-index.json)，BIL 仍未接入。
- 仓库尚未提供项目许可证文件；既有 LLVM notice 已保留，正式发布前的项目授权条款仍需确定。
- 交付候选后的 CI 发现 Windows 检出字节漂移和历史多命令步骤掩盖失败；修复后的 Windows/Linux CI 已分别通过。旧 Linux 宿主桩标准错误未被保存、具体内容仍未知。结果见[跨平台记录](docs/CI_BYTE_IDENTITY_20261101.md)，既有预览包和旧回执未改写。

## 功能继续开发（2026-11-01）

- 外部导入增加可报告 origin 释放失败的可选扩展；失败时保留不确定 claim 和账目，旧 ABI 版本值不变。旧接口及新接口各自的地址复用边界见[导入合同](docs/EXTERNAL_IMPORT.md)。
- 新增无逃逸指针的 Java 原生 lease 字节访问、受限暂存和独立 Android 测试入口，见[接口合同](docs/ANDROID_NATIVE_BORROW.md)与[设备协议](docs/ANDROID_BORROW_DEVICE_PROTOCOL.md)。宿主开发检查、设备运行器离线回归和脱敏版独立 R8 测试 APK 构建完成；ART、R8 运行和设备验证尚未执行。[构建记录](artifacts/feature-development-20261101/android-borrow-r8-build.json)。
- 新增可选 Compute CPU READ 桥，映射和独立 lease 由原 owner 保活，组件独立构建，不改变核心或 BIL 的依赖图。以最终核心和真实 CPU reference SDK 的宿主检查4项通过，独立安装消费者的正常与故障隔离路径通过；Android/GPU 和性能资格仍待统一验证，详见[桥合同](adapters/compute/README.md)。
- 已核实 Lumen 当前软件工作区及压力接口；现有公开 API 不接受外部 allocator，未改动或接入 Lumen。[合同记录](docs/LUMEN_MEMORY_CONTRACTS.md)区分实际代码、可选协调方向和未验证事项。
- Android arm64、x86_64、armeabi-v7a 核心、JNI 与单 ABI AAR 已交叉构建，并生成独立脱敏候选；加载段、导出、依赖及静态库相关段和重定位核对见[记录](artifacts/feature-development-20261101/android-sanitized-v2/manifest.json)。这不等于三种 ABI 均已在设备运行。16 KB、MTE/HWASan、长期稳定与性能验证仍按原范围单列。
- 脱敏工程预览包及内容哈希见 [ARTIFACTS.json](ARTIFACTS.json)；它未绑定最终提交，也不代表设备或正式发布资格。

## 当前阶段

API33停滞已在独立复现中观察到目标自身cgroup冻结，触发者及历史每次是否同因未知。相同APK后台再次冻结；另一次在instrumentation内启动前台Activity时，[Activity尚未resumed即观察到冻结](artifacts/android-close-investigation-20261030/freezer-control-foreground/receipt.json)。先由shell预启动受保护诊断窗口、再以no-restart保持同一进程的组合，短对照完成1000周期且采样未见冻结。两种前台启动方式和重启时序不同，不构成单变量因果结论。JNI的独立自动通知及错误分支缺口已修复，Host Release/ASan各29项通过。

设备环境补充：用户说明手机安装了相关冻结模块并导致上述冻结。这是用户提供的信息；原始采集只记录了进程冻结，未独立识别每次冻结的触发者，历史回执保持不变。

最终去debug路径的安装版在API33 arm64真机独立运行 901.089 秒、14959 周期，366次自身cgroup采样未见冻结；窗口正常结束，预算、跨关闭lease、无调用者poll自动收尾、未知原因及正常域归零通过。API34 x86_64独立模拟器的同版短测通过，未建立API34长稳。BIL未接入，性能未测。详见[关闭调查与验证](docs/ANDROID_CLOSE_INVESTIGATION_20261030.md)。

## 当前已交付

- 核心：System/custom provider、有界 arena、唯一 backing 和 reservation 预算、owner/读写 lease、非阻塞 begin_close、collect/drain、reset/trim、C++ RAII、CMake install/export。
- 0.2 新能力：SYSTEM 完整外部 region 导入、runtime 路由和重叠拒绝、软预算；Android pressure worker、ComponentCallbacks2/JNI 异步关闭及自身 RSS/PSS/private-dirty；独立 packed reader。
- 0.2 首阶段功能证据：Host Release/ASan 各10项；packed Java32、真实 Kotlin oracle288、ART32；API33 arm64/4KB 设备原生9项、Android20项及针对自身包的 AMS 注入通过。详情见 [0.2阶段收尾](docs/FEATURE_STAGE_20261030.md)。该阶段仅有上述限定证据，不单独构成后续长期稳定性、资源节省、R8或产品资格。
- 真实消费者：安装后的 Memory SDK 已由冻结 DexMeguviz 源码和 P6 文件图像工作区消费。v2 在 API33 arm64/4KB 实际通过 core3 和消费者196项（DEX32、图像工作区156、图像计时合同8）。

## 历史实验与归因

真实 v2 采集器已经实现并执行；四组制品和协议在采集前冻结。pilot128任务完成；图像因精度门槛未进入formal，DEX formal完成4/12窗口、64次native后因环境门槛停止。设备窗口已结束，没有排队任务；不完整正式区间已撤销，见 [v2最终结果](docs/DEVICE_V2_RESULTS.md)。不得继续把这一轮称为“离线工具、尚无采集器、只交叉构建”或“pilot仍在运行”。

更早的独立 DEX A1/A3 实验完成32块/128任务，但存在趋势警告，性能为 INCONCLUSIVE。新旧轮次不混池；见 [历史性能报告](docs/PERFORMANCE_A1_A3.md)。

普通直接借用删除一份10,540,184B输入及复制；1080p三行环少2,067,840B payload，当前arena chunk下少2,008,064B backing。以上是普通消费者优化，不是 Memory 增量或 RSS 结论。已有BIL scratch/holder/staging复用不重复计功。自身 RSS/PSS 已能观测，但尚无同完成量资源净收益；能量没有合格方法。

## 上一阶段历史

本阶段按用户要求快速收尾：DEX外部导入、图像压力/预算/异步关闭组合及Host/arm64原生900秒循环通过；最终JNI锁修复Release/ASan各18项、8组原/新反例及双ABI真实R8短测通过。Android长稳没有取得资格：首个API33子项1260秒超时，closefix新轮仍停在iteration48/cycle.idempotent-close，缺根因栈；新API34轮仍有进度，但按用户快速收尾要求提前结束。旧模块API34完整900秒结果独立保留，不提升新模块资格。仅结束本项目诊断进程，不清应用数据、不停止其他会话。详情见[阶段交付与阻塞](docs/CONSUMER_CLOSEOUT_20261030.md)。

## 剩余边界

- 16KB真实loader、MTE/HWASan进程及故障试验、同完成量能源、正式性能和发布资格独立缺失。
- 本阶段新增0.2 x86_64 SDK及派生closefix1压力AAR，API34 R8短测通过；完整矩阵不从旧0.1升级，armv7未重新覆盖。
- Compute/Media真实SDK及合同已核实，Compute公开host descriptor没有外部CPU指针/provider注入口；反向完整CPU READ-map桥已实现独立宿主路径，Android、产品和性能资格未取得。见[家族合同](docs/FAMILY_MEMORY_CONTRACTS.md)和[桥合同](adapters/compute/README.md)。Lumen/Bitmap/HWUI未接入。
- 压力适配JNI已有；通用ByteBuffer借用API、Rust、slab、Tile Heap、特殊placement/TLB策略未选择，不将其作为基础CPU库必需功能。
- BIL保持原实现，本轮不改其依赖或默认开关；无生产热点、接线及稳定净收益证据不宣称产品资格。

当前能力见[CAPABILITY_MATRIX](CAPABILITY_MATRIX.json)，MM任务与原依赖见[TASKS](TASKS.json)。原0.1与0.2收据、制品清单均保留；新继续阶段证据独立放入 `artifacts/continuation-20261030/`，不改历史原始实验。
