# 冻结 Dex SDK 借用合同与 DEX v2

2026-10-30。该结论只适用于原实验冻结在 `build/locked-inputs/DexMeguviz` 的 SDK 闭包。研究报告无法从公开渠道取得它；本工作区已保留真实副本，因此这项资料阻塞在本地解除。没有读取外部 DexMeguviz 新 HEAD 替代旧实现，没有修改 SDK。

逐文件身份、原 `SOURCE_LOCK.json` 的 SHA256 和 65 个 SDK 文件的核对结果见 [borrow_contract_gate.json](../artifacts/research-20261030/dex/borrow_contract_gate.json)。原源锁中的根提交、dirty 标记与逐文件身份各自保留；报告所在提交不是所有二进制的编译提交。新 v2 二进制、安装 Memory 动态库和消费者源码分别记录哈希，不能把这个主机收据当作旧 arm64 制品重新构建证明。

## 已核实的 SDK 代码合同

| 合同 | 冻结文件与实现依据 | v2 处理 |
|---|---|---|
| immutable 与回调 | `include/dexmeguviz/dexmeguviz.h:105` 起的 `dmg_buffer_lease` 要求回调 noexcept、thread-safe，并维持 bytes immutable；`src/c_api/source_input.cpp:115` 起验证两个回调、immutable=1 和 reserved=0 | 发布前完成构造；发布后不修改 data/size/capacity，不移动 context；回调不抛出，引用计数原子化 |
| 源基地址对齐 | `src/format/reader.cpp:103–118` 按范围检查后从 uint8_t 元素拼接 u16/u32；`src/source/sha256.hpp:26` 起逐字节组装 word；`src/format/instructions/decoder.cpp` 的 read16/read32 同样逐字节取值 | raw DEX 基地址没有 64B 对齐前提；DEX 内 offset 的格式对齐仍严格检查。D-Z0 直接使用稳定 vector<uint8_t>；复制组保持原 64B 分配，差异归完整输入策略 |
| 范围与大小 | `source_input.cpp` 的 validate 检查 SDK/max_source/size_t 上限；`Source::view()` 只暴露 size 字节；reader 的 range 检查所有容器边界 | size 取真实文件长度；capacity 独立记账，未初始化容量不作为输入 |
| retain 时点 | borrowed open 先验证参数/预算，再 reserve_borrowed，调用 retain 后把 lease 和源指针安装进 Source，随后执行 digest/parse | 有效输入成功时 retain=1；无效 lease 和 borrowed budget 拒绝前 retain=0；parse 拒绝时已经 retain，Source 析构必须配对释放 |
| snapshot/result 延寿 | `src/c_api/runtime.hpp` 中 Source 被 Dataset 的 shared_ptr<const Source> 保持；`dmg_snapshot` 与 `dmg_result` 都保有 Dataset；`query_execution.cpp:31` 起 result 取得同一 Dataset | caller 提前放弃 owner；snapshot 在结果完整消费前释放；全部方法、数值和 UTF-16 文本最后访问完成后才 release result |
| 最后释放 | `Source::~Source()` 先归还 SDK borrowed account 再调用 lease.release；Source 的析构线程由最后 shared_ptr 所在线程决定；`api.cpp` 的 snapshot/result release 使用原子 handle 引用计数 | callback 可在另一个消费者线程执行；context 一直存活，join 后才销毁 InputOwner。Memory read lease 归还后才 collect/drain/destroy |
| 同步 API 与取消 | 同步 `dmg_query_methods` 没有取消参数；现有 `dmg_job_cancel` 才是取消入口，`dmg_job_release` 会取消并 join worker；JobState 与 queued batch 保持 Dataset | 不新增取消 API；测试已有 Job。batch_size=1/max_queue=1、真实输入多于一方法且不消费队列，阻止 worker 在取消前提交全部结果；取消后另一线程 final release/join |
| closed/error | snapshot close 关闭新查询准入，不强制销毁仍被持有的 Source；失败不发布 snapshot/result；guard 将错误转为明确状态 | 验证 closed 查询返回 CLOSED、result=null，以及参数拒绝、parse 拒绝、budget 拒绝，各自保留 callback 计数 |

这是所选 raw DEX/完整同步 method query 的源码审计和主机验证。没有把任意 API 并发调用、任意 SDK 版本或 Android 运行时提升为已验证合同。用户在原始共享对象上进行未同步容器修改仍违反 immutable 条件。

## 四组可比较的边界

- **D-C0**：直接 System 对齐分配、复制，不调用 Memory 分配/lease。
- **D-Z0**：直接借用已预读、地址稳定且 immutable 的普通 vector；这是强普通消费者基线。
- **D-C1**：Memory SYSTEM 加相同复制、writer/read lease 与生命周期核验。
- **D-C2**：Memory ARENA 加相同复制、相同 lease，一个 chunk/一个 allocation；没有批量 reset 或 parser 内部分配注入。

Memory ABI 没有已验证的外部 owner import/adopt 接口，因此 D-Z1/D-Z2 明确 N/A。没有为了补齐表格增加 provider 或生产后端。D-C2 对 D-Z0 是整套策略比较，不能叫 allocator/TLB 单因素收益；各组百分比不能相加。

原 `paired.cpp`、旧计划、原始 JSONL、report 与 arm64 制品完全保留。新协议的 `total_ns` 明确包含所有组的预读 vector 最终释放，不能与旧 v1 计时混池。

## 主机资格及资源结论

[functional_receipt.json](../artifacts/research-20261030/dex/functional_receipt.json) 保存两个构建的独立 binary SHA256、安装 Memory 库 SHA256、逐用例输出与检查。正式入口在编译期消除阶段时钟；诊断入口记录互不重叠外部阶段和残差。正式入口仍包含生命周期正确性和资源检查开销，没有声称这是 SDK 纯算法时间。

32 项均通过：四组 × 七个正式构建功能用例，以及四组诊断完整查询。完整查询得到相同输入 SHA256、54,147 方法与 checksum `12701547145945416225`；结果继续持有输入，callback 平衡、跨线程最后释放、取消后的 join、Memory CLOSED_ZERO 和全部组源最终释放通过。额外检查不支持的 D-Z1 被明确拒绝。所有 elapsed 数值是功能执行记录，未进入性能采样，未执行设备实验。

在本次 Windows 实测 vector capacity=10,540,184 B。复制组的预读源加独立输入唯一容量为 21,080,368 B；直接借用组为 10,540,184 B，省下一份目标 backing 和复制。SDK borrowed_view 是同一存储视图，不再叠加。这个可验证资源结果归普通消费者输入策略，不是 Memory 加速证据，也不是进程 RSS/DRAM/TLB/能量结论。parser 账户、Memory 元数据与 OS 开销另列；未观测项使用 null。

可重放主机命令：

```powershell
tools/host-build.cmd -S consumers/dex -B build/dex-borrow-v2 -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=安装目录 -DDEXMEGUVIZ_SOURCE_DIR=冻结SDK目录
tools/host-build.cmd --build build/dex-borrow-v2 --target mgmem_dex_paired_v2 mgmem_dex_paired_v2_diagnostic
python tools/verify_dex_borrow_v2.py --input 本地真实classes.dex --source-lock artifacts/research-20261030/historical-source-lock.json
```

构建需要现有 MSVC/CMake/Ninja；Ninja 不在 PATH 时沿用本项目工具链路径设置。验证不下载真实 DEX，不上传 APK/DEX，不调用 ADB。Android v2 功能与性能、16KB/MTE、RSS/energy 均未由此收据覆盖。BIL 维持不接入。
