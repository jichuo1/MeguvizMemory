# MeguvizMemory 工程状态

更新：2026-10-30。当前交付 **0.1.0 CPU 功能工程预览**，公开仓库 [jichuo1/MeguvizMemory](https://github.com/jichuo1/MeguvizMemory)。正式 Release、BIL 默认切换与性能资格未完成。

已交付版本化 C ABI、System/custom provider、唯一 backing 记账、有界 arena、allocation owner/读写 lease、reservation 预算、非阻塞资源准入关闭、collect/drain 分工、reset/trim、C++ RAII、CMake install/export，以及真实 DexMeguviz 和文件图像工作区两个安装后消费者。Windows Release 与 ASan 的 4 项 CTest 全过，双消费者验证 24/24 通过；Android 三 ABI 构建安装，API 34 4 KB x86_64 模拟器核心/C/C++ 实际运行通过。

静态 Windows SDK 已实际安装并通过独立图像 22/22 验证；远端代码提交 `58ed07f` 的 Windows/Linux 构建均通过 4 项 CTest 和安装后的图像 22/22，见 [CI 回执](https://github.com/jichuo1/MeguvizMemory/actions/runs/40995571678)。本地 `dist/` 提供 Windows shared/static 与 Android 三 ABI 共五个 SDK ZIP，逐条校验内容等于安装目录，哈希见 ARTIFACTS。

具体资源结果保留 arena 的额外 backing，见 [BENCHMARK_RESULTS](docs/BENCHMARK_RESULTS.md)。没有从申请次数推导性能/RSS，不填研究模型为实测。

证据入口：

- [VERIFICATION](docs/VERIFICATION.md)：命令、环境、通过范围与失败经验。
- [ADR](docs/ADR-001-ownership-and-abi.md)、[ALLOCATION_OWNERSHIP](docs/ALLOCATION_OWNERSHIP.md)、[INTEGRATION](docs/INTEGRATION.md)：API、寿命、关闭和回退合同。
- `SOURCE_LOCK.json` / `ARTIFACTS.json`：源码、外部 parser、工具链及构建/安装制品哈希。
- `artifacts/verification-20261030/consumers/receipt.json`：24 个完整命令、原始输出与独立 oracle。
- [CAPABILITY_MATRIX](CAPABILITY_MATRIX.json)、[TASKS](TASKS.json)、`bench/protocol.json`：能力和 MM 任务的分维度状态。
- [消费者审计](docs/audit/consumers.md)：BIL 当前 HEAD 与已有 P1-07/P2-05、packed、真实家族 SDK 对齐。

下一步依据是当前缺失证据，而非扩大 allocator 功能：先固定目标产品工作负载与 A1 原生 backing 合同；取得不争用其他会话的设备时段和真实 16 KB/MTE 环境；再冻结配对性能协议并测 CPU 端到端/能量/RSS，判断是否值得进一步接入。Compute/Media 已有真实 SDK，下一步需要固定适用 profile 与 CPU scratch import/owner 合同，不能把 content lease 当 allocator 注入。JNI/Rust/slab/TLB placement 均没有当前必要消费者，保持未选择。

BIL main 核查为 `00572940c354fbec7b6f311d4ecd6f02e66ea7c5`；现有 scratch `e630ad56` 和 holder 打包 `061360f5` 不计为 Memory 增量。外部工作树和并行会话保留，没有把本批函数测试当其设备或发布验收。
