# MeguvizMemory 工程状态

更新：2026-10-30。当前交付 **0.2.0 功能工程预览（native ABI 1.1）**，公开仓库 [jichuo1/MeguvizMemory](https://github.com/jichuo1/MeguvizMemory)。正式 Release、BIL 默认切换与性能资格未完成。

本阶段收尾：packed reader、外部CPU内存安全导入、Android压力与自身RSS/PSS观测适配已实现。Host Release/ASan各10项、Java packed32/真实oracle288、Android原生9/ART32/压力20功能通过；家族仅合同审计，BIL不接入，性能与正式发布仍未资格。见[阶段收尾](docs/FEATURE_STAGE_20261030.md)。
v2最新结果：2210132C/API33/arm64/4KB上core3与196消费者功能通过；完整pilot128任务后图像因精度停止，DEX正式轮64次成功后在第5窗口因CPU2背景忙碌率门槛中止。正式计划未完成，性能未取得资格，BIL继续不接入。原报告的不完整区间已另行撤销，见[最终v2结果](docs/DEVICE_V2_RESULTS.md)。
研究回包的有限下一批已实施：冻结 Dex SDK 65 文件逐一核对，四种 source 策略完成宿主 32/32 功能验证；图像 full/三行环 × direct/System/arena 的持久工作区完成 156/156 独立 oracle/资源/异常/关闭检查。DEX 直接借用省去一份 10,540,184 B 输入与复制；1080p 三行环少 2,067,840 B payload、按当前 arena chunk 少 2,008,064 B backing。以上归普通消费者资源优化，不是 Memory 提速或 RSS 结论。新 DEX 正式/诊断及图像三个 arm64 程序已交叉构建，**未执行新设备实验**。

新离线协议工具按采集窗口汇总、绑定原生 stdout 和资源字段，36/36 检查通过。4 pilot窗口/12 formal窗口仍是草案；当前入口没有 live collector，不能启动设备或判性能 PASS。旧32块数据独立复算一致；planned/achieved 精度分别记录，原始 JSONL/plan/report 未改。见 [本轮证据与范围](docs/RESEARCH_EVIDENCE_20261030.md)、[DEX 合同](docs/DEX_BORROW_CONTRACT.md)、[新协议冻结清单](docs/PROTOCOL_FREEZE_CHECKLIST.md)。

上一批已完成 **API 37 / arm64 / 4 KB 真机生命周期、预算和关闭安全验证**。真实 DEX 的独立 A1/A3 正式比较完成 32 块、128 次任务，功能与环境完整，但性能判定为 **INCONCLUSIVE**；描述性点估计 1.08%，探索性区间 −0.02%～2.33%，前后半程趋势触发预注册警告。两组输入 backing 峰值相同。因此本批 **不接入 BIL**，不宣称稳定加速或内存节省。设备已协调释放。

已交付版本化 C ABI、System/custom provider、唯一 backing 记账、有界 arena、allocation owner/读写 lease、reservation 预算、非阻塞资源准入关闭、collect/drain 分工、reset/trim、C++ RAII、CMake install/export，以及真实 DexMeguviz 和文件图像工作区两个安装后消费者。Windows Release 与 ASan 的 4 项 CTest 全过，双消费者验证 24/24 通过；Android 三 ABI 构建安装，API 34 4 KB x86_64 模拟器核心/C/C++ 实际运行通过。

静态 Windows SDK 已实际安装并通过独立图像 22/22 验证；远端代码提交 `58ed07f` 的 Windows/Linux 构建均通过 4 项 CTest 和安装后的图像 22/22，见 [CI 回执](https://github.com/jichuo1/MeguvizMemory/actions/runs/40995571678)。本地 `dist/` 提供 Windows shared/static 与 Android 三 ABI 共五个 SDK ZIP，逐条校验内容等于安装目录，哈希见 ARTIFACTS。

Dex 最终验证采用 `build/locked-inputs/DexMeguviz` 冻结输入、`build/dex-locked` 构建和本地回放输入 ZIP。这样允许原始 Dex 会话继续推进，同时保持本批来源/制品/fixture 稳定；具体使用方法见 INTEGRATION。

具体资源结果保留 arena 的额外 backing，见 [BENCHMARK_RESULTS](docs/BENCHMARK_RESULTS.md)。没有从申请次数推导性能/RSS，不填研究模型为实测。

证据入口：

- [VERIFICATION](docs/VERIFICATION.md)：命令、环境、通过范围与失败经验。
- [ARM64_VALIDATION](docs/ARM64_VALIDATION.md)、[PERFORMANCE_A1_A3](docs/PERFORMANCE_A1_A3.md)、[BIL_ADOPTION_DECISION](docs/BIL_ADOPTION_DECISION.md)：本次真机、两轮原始试验和不接入决定。
- [ADR](docs/ADR-001-ownership-and-abi.md)、[ALLOCATION_OWNERSHIP](docs/ALLOCATION_OWNERSHIP.md)、[INTEGRATION](docs/INTEGRATION.md)：API、寿命、关闭和回退合同。
- `SOURCE_LOCK.json` / `ARTIFACTS.json`：源码、外部 parser、工具链及构建/安装制品哈希。
- `artifacts/verification-20261030/consumers/receipt.json`：24 个完整命令、原始输出与独立 oracle。
- [CAPABILITY_MATRIX](CAPABILITY_MATRIX.json)、[TASKS](TASKS.json)、`bench/protocol.json`：能力和 MM 任务的分维度状态。
- [消费者审计](docs/audit/consumers.md)：BIL 当前 HEAD 与已有 P1-07/P2-05、packed、真实家族 SDK 对齐。

后续研究先固定强普通基线，再冻结新制品、采集窗口、cadence、预算、时限和外层推断条件，补齐 live collector 后才开展新设备实验。旧轮次唯一一次工程重试已经结束，不追加或混池。当前没有已验证的池化/placement 净收益；BIL 还需真实调用热点、寿命与桥接成本证据。16 KB/MTE、RSS/能量各自仍需验证。Compute/Media 已有真实 SDK，CPU scratch import/owner 合同尚未确认，不能把 content lease 当 allocator 注入。JNI/Rust/slab/TLB placement 均没有当前必要消费者，保持未选择。

BIL main 核查为 `00572940c354fbec7b6f311d4ecd6f02e66ea7c5`；现有 scratch `e630ad56` 和 holder 打包 `061360f5` 不计为 Memory 增量。外部工作树和并行会话保留，没有把本批函数测试当其设备或发布验收。

2026-10-30 v2设备阶段：新设备2210132C/API33/arm64/4KB的core3与196消费者功能用例实际通过；真实采集器已实现，四组协议采前冻结，当前pilot按预排运行，尚无正式性能结论。见 [v2设备实验](docs/DEVICE_V2_20261030.md)。
