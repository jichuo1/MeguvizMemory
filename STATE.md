# MeguvizMemory 工程状态

更新：2026-10-30。当前交付 **0.1.0 CPU 功能工程预览**，公开仓库 [jichuo1/MeguvizMemory](https://github.com/jichuo1/MeguvizMemory)。正式 Release、BIL 默认切换与性能资格未完成。

本次推进已完成 **API 37 / arm64 / 4 KB 真机生命周期、预算和关闭安全验证**。真实 DEX 的独立 A1/A3 正式比较完成 32 块、128 次任务，功能与环境完整，但性能判定为 **INCONCLUSIVE**；描述性点估计 1.08%，探索性区间 −0.02%～2.33%，前后半程趋势触发预注册警告。两组输入 backing 峰值相同。因此本批 **不接入 BIL**，不宣称稳定加速或内存节省。设备已协调释放。

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

下一步依据是当前缺失证据，而非扩大 allocator 功能或反复 pilot：当前真实 DEX 输入已经是单 dense backing，没有已验证的池化/placement 净收益；需先取得 BIL 真实调用热点、寿命与桥接成本证据，再考虑新的预注册产品实验。16 KB/MTE、独立时段下的热/频率资格、RSS/能量各自仍需验证。Compute/Media 已有真实 SDK，CPU scratch import/owner 合同尚未确认，不能把 content lease 当 allocator 注入。JNI/Rust/slab/TLB placement 均没有当前必要消费者，保持未选择；本轮唯一一次工程重试已结束，不再追加样本。

BIL main 核查为 `00572940c354fbec7b6f311d4ecd6f02e66ea7c5`；现有 scratch `e630ad56` 和 holder 打包 `061360f5` 不计为 Memory 增量。外部工作树和并行会话保留，没有把本批函数测试当其设备或发布验收。
