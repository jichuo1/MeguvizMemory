# MeguvizMemory 工程状态

更新：2026-10-30。当前交付 **0.2.0 功能工程预览 / native ABI 1.1**。公开仓库为 [jichuo1/MeguvizMemory](https://github.com/jichuo1/MeguvizMemory)。正式 Release、性能资格和 BIL 接入尚未完成。

## 当前已交付

- 核心：System/custom provider、有界 arena、唯一 backing 和 reservation 预算、owner/读写 lease、非阻塞 begin_close、collect/drain、reset/trim、C++ RAII、CMake install/export。
- 0.2 新能力：SYSTEM 完整外部 region 导入、runtime 路由和重叠拒绝、软预算；Android pressure worker、ComponentCallbacks2/JNI 异步关闭及自身 RSS/PSS/private-dirty；独立 packed reader。
- 0.2 功能证据：Host Release/ASan 各10项；packed Java32、真实 Kotlin oracle288、ART32；API33 arm64/4KB 设备原生9项、Android20项及针对自身包的 AMS 注入通过。详情见 [0.2阶段收尾](docs/FEATURE_STAGE_20261030.md)。这些是限定用例的功能证据，不构成长期稳定性、资源节省、R8 或产品资格。
- 真实消费者：安装后的 Memory SDK 已由冻结 DexMeguviz 源码和 P6 文件图像工作区消费。v2 在 API33 arm64/4KB 实际通过 core3 和消费者196项（DEX32、图像工作区156、图像计时合同8）。

## 历史实验与归因

真实 v2 采集器已经实现并执行；四组制品和协议在采集前冻结。pilot128任务完成；图像因精度门槛未进入formal，DEX formal完成4/12窗口、64次native后因环境门槛停止。设备窗口已结束，没有排队任务；不完整正式区间已撤销，见 [v2最终结果](docs/DEVICE_V2_RESULTS.md)。不得继续把这一轮称为“离线工具、尚无采集器、只交叉构建”或“pilot仍在运行”。

更早的独立 DEX A1/A3 实验完成32块/128任务，但存在趋势警告，性能为 INCONCLUSIVE。新旧轮次不混池；见 [历史性能报告](docs/PERFORMANCE_A1_A3.md)。

普通直接借用删除一份10,540,184B输入及复制；1080p三行环少2,067,840B payload，当前arena chunk下少2,008,064B backing。以上是普通消费者优化，不是 Memory 增量或 RSS 结论。已有BIL scratch/holder/staging复用不重复计功。自身 RSS/PSS 已能观测，但尚无同完成量资源净收益；能量没有合格方法。

## 本轮继续工作

本阶段按用户要求快速收尾：DEX外部导入、图像压力/预算/异步关闭组合及Host/arm64原生900秒循环通过；最终JNI锁修复Release/ASan各18项、8组原/新反例及双ABI真实R8短测通过。Android长稳没有取得资格：首个API33子项1260秒超时，closefix新轮仍停在iteration48/cycle.idempotent-close，缺根因栈；新API34轮仍有进度，但按用户快速收尾要求提前结束。旧模块API34完整900秒结果独立保留，不提升新模块资格。仅结束本项目诊断进程，不清应用数据、不停止其他会话。详情见[阶段交付与阻塞](docs/CONSUMER_CLOSEOUT_20261030.md)。

## 剩余边界

- 16KB真实loader、MTE/HWASan进程及故障试验、同完成量能源、正式性能和发布资格独立缺失。
- 本阶段新增0.2 x86_64 SDK及派生closefix1压力AAR，API34 R8短测通过；完整矩阵不从旧0.1升级，armv7未重新覆盖。
- Compute/Media真实SDK及合同已核实，Compute公开host descriptor没有外部CPU指针/provider注入口；反向完整CPU READ-map导入是候选，桥未实现。见[家族合同](docs/FAMILY_MEMORY_CONTRACTS.md)。Lumen/Bitmap/HWUI未接入。
- 压力适配JNI已有；通用ByteBuffer借用API、Rust、slab、Tile Heap、特殊placement/TLB策略未选择，不将其作为基础CPU库必需功能。
- BIL保持原实现，本轮不改其依赖或默认开关；无生产热点、接线及稳定净收益证据不宣称产品资格。

当前能力见[CAPABILITY_MATRIX](CAPABILITY_MATRIX.json)，MM任务与原依赖见[TASKS](TASKS.json)。原0.1与0.2收据、制品清单均保留；新继续阶段证据独立放入 `artifacts/continuation-20261030/`，不改历史原始实验。
