# arm64 真机功能验证（2026-10-30）

结论：**本批指定二进制在 PMA110、API 37、4096 B 基础页的真实 arm64 设备上完成生命周期、预算和关闭安全验证。** 核心 11 组、145,033 项检查通过；纯 C 消费者关闭 backing 归零，C++ RAII 五项用例通过。该结论是功能证据，不是性能、16 KB、MTE 或 BIL 产品资格。

主证据为 [设备准入](../artifacts/arm64-20261030/device-admission.json) 和 [功能收据](../artifacts/arm64-20261030/safety/receipt.json)。后者记录 24 条实际设备命令，全部退出码为 0、无超时，并保留测试原始 stdout/stderr。

## 准入、时间与实际环境

此前审计发现另一会话的 GPU 测试未完结，因此没有据主机无长期 ADB 客户端而直接认定空闲。用户授权协调后，MeguvizPack 原 owner 明确回复设备已释放、没有排队的设备任务，后续如需使用先协调。准入状态为 `RELEASED_FOR_MEMORY`，原轮次 `gpu_gate_v2_1793324951` 的终态仍为 `INTERRUPTED_INCOMPLETE`；释放设备没有把原 GPU 轮次变成通过。

原 owner 释放回执的 SHA256 为 `da1c9b57d6d4d6ca58f5b5cc1f0ff0bc85cace7b13d48b945aa50dd01dc21b2f`，路径记录在准入文件中。Memory 执行前复查原诊断 PID 已不存在，没有匹配诊断进程或活跃 instrumentation。空闲进程豁免列表为空，本轮没有依靠豁免绕过其他测试 owner。

| 项目 | 本轮记录 |
|---|---|
| 型号 / ABI | `PMA110` / `arm64-v8a` |
| Android API | `37` |
| 内核 | `6.12.69-android16-6-g242e1a07f878-ab15865446-4k` |
| 实际基础页 | `4096 B`，来自运行时 `getconf PAGESIZE` |
| 功能采集时间（Asia/Shanghai） | `2026-10-30 11:36:41.993570` 至 `11:36:43.838053` |
| 前置占用复查 | `11:36:42.264193`；匹配测试/诊断进程与活跃仪器均为空 |
| 后置占用复查 | `11:36:43.782788`；同上 |
| boot identity | 前后 SHA256 一致：`b7a76efcea39263996369f09e672fe4a21e824b7e69a596d569266e9b6636c9d` |
| 独立设备目录 | `/data/local/tmp/meguvizmemory-arm64-20261030T033641Z-460563ec1ce9` |
| 共享设备锁 | 没有取得通用机器锁；本次是原 owner 协调释放加前后观察性复查 |

设备序列号正文没有进入公开回执，设备身份以 SHA256 关联。采集器没有 kill、force-stop、解冻旧进程、重启、清应用数据，或修改 governor、settings、sysfs 等全局策略。只在本批新建的独立目录上传四个 ELF、设置本批测试文件执行位并运行它们；目录保留，不自动删除。该目录和前后观察不是阻止其他会话未来使用设备的排他租约。

## 实际运行的二进制

本地文件与设备上传后 `sha256sum` 均一致，适用范围固定为下列四份制品。其他版本需要重新核验，不能仅凭同名文件沿用本次通过。

| 二进制 | 字节数 | 本地及设备 SHA256 |
|---|---:|---|
| `libmgmem.so` | 1,665,960 | `9906104f022f18d9946deed60a5002bf4a8eadda71f8b051f4467d01d26dd919` |
| `mgmem_core_tests` | 509,816 | `3c069fefed51fddc36077f4a51655092080b2078b72d05529431428ed8ccff4a` |
| `mgmem_c_consumer` | 13,400 | `91e28ba320bd3df5c9929c68752c8648f5a36939fd25fdc79c82d2ebf52c0704` |
| `mgmem_cpp_consumer` | 194,712 | `52e21437ce336cc6c6e1c2ade9e4b67f0cf0856f6793929709f4f6a35b00f12b` |

测试使用仅指向本批目录的 `LD_LIBRARY_PATH`，实际动态装载上述 `libmgmem.so`，没有替换 BIL 或任何家族运行库。完整命令、上传哈希和执行位操作位于功能收据；收据 SHA256 为 `88889fd3571e6e4e33c3711b5db430594f53e5718f6d026e9cd9e06b93dca9dc`。

## 功能结果与覆盖

核心输出为 `suite=mgmem-core-v1, tests=11, checks=145033, status=PASS`，原始输出为 [019-test-mgmem_core_tests.stdout.txt](../artifacts/arm64-20261030/safety/019-test-mgmem_core_tests.stdout.txt)。通过的命名用例为：

1. `abi_and_checked_arithmetic`：ABI、页/长度算术及溢出。
2. `system_budget_layout_and_rollback`：System 对齐、预算拒绝和旧数据保持。
3. `provider_oom_and_exact_pairing`：OOM、异常边界与原 provider 精确配对回收。
4. `allocation_reservation_close_race`：在途申请预算 reservation 与关闭竞争。
5. `borrow_access_and_handle_identity`：读写互斥、范围、跨域/错误/重复句柄。
6. `late_cross_thread_lease_after_close`：owner 关闭后的晚完成跨线程 lease。
7. `arena_retained_reset_and_trim`：活跃资源时拒绝 reset/trim，安全复用与 retained。
8. `growth_coexistence_peak`：新旧 backing 共存峰值与增长失败原子性。
9. `drain_completion_and_timeout`：完成、超时与资源保留。
10. `drain_poll_during_blocked_collection`：回收回调阻塞时，drain 仍按时返回且不强制释放。
11. `concurrent_system_stress`：已有六线程并发申请、借用与释放压力。

[纯 C 输出](../artifacts/arm64-20261030/safety/020-test-mgmem_c_consumer.stdout.txt) 确认 `success=true, backing_after_close=0`。[C++ 输出](../artifacts/arm64-20261030/safety/021-test-mgmem_cpp_consumer.stdout.txt) 确认 `mgmem-cpp-raii, cases=5, status=PASS`，覆盖 System/arena 借用活过 wrapper 作用域、worker 最后释放、allocation 独立保活、预算失败保持旧数据和 move/只读/空 wrapper 合同。具体实现见 [核心测试](../tests/core_tests.cpp) 与 [C++ 测试](../tests/cpp_consumer.cpp)。

功能采集只执行这一轮既有测试，没有为了增加通过数字另外运行重复压力或改变测试工作量。

## 原始库存的隐私与观察范围

[采集器](../tools/run_arm64_validation.py) 在准入通过前不运行 ADB。`ps` 全量输出在内存中解析，必须实际解析出进程；所有匹配诊断/测试的行都会进入占用判断。activity 查询采用 API 37 可用的 `dumpsys activity`，只检查 `mActiveInstrumentation` / `ActiveInstrumentation` 及其 section。命令失败、权限错误或未知错误不被当作无占用。

全量进程和活动库存、stderr 仅留在本次采集进程内存中，公开文件只保存原始字节 SHA256 以及匹配诊断行或明确仪器 section，不保存无关用户应用与页面。前后库存命令的 `capture_policy` 均为 `RAW_IN_MEMORY_ONLY_FILTERED_PUBLIC_OUTPUT`；公开文件为 `001/002-before-*` 和 `022/023-after-*`。boot ID 只保存 SHA256，序列号被替换。没有仪器标记的成功 activity 输出只构成该观察范围内的未发现，不是通用设备锁证明。

## 独立 A1/A3 源码复核结论

另一次只读复核检查了 [paired.cpp](../consumers/dex/paired.cpp)、[设备采样器](../tools/run_paired_device.py)、[统计分析](../tools/paired_analysis.py) 及 [冻结协议](../bench/paired_protocol_resolved.json)。复核没有执行设备命令或 CPU 基准，结论是**未发现阻断既定采样的实质问题**，不是性能通过。

- A1 和 A3 使用同一 System 分配原语、64 B 对齐、10,540,184 B 输入及同样的初始化复制；都调用真实 borrowed-source parser 并完整消费 54,147 个方法和全部文本。A1 没有被额外分配、减配预算或不同工作量人为弱化；A3 的域、lease、关闭、快照和最终回收成本均计入完整任务 elapsed。
- 文件预载对两组都在任务计时外单独记录。默认 timing 不运行详细阶段计时；`--diagnostic` 轮次分开，不混入正式估计。该边界是预载后 CPU 任务，不是 APK 打开到结果或 BIL 端到端延迟。
- 原 64 MiB parser 预算拒绝记录保留于 [host-A1-parser-budget64.json](../artifacts/arm64-20261030/failures/host-A1-parser-budget64.json)。在 pilot 前，两组共同固定为 256 MiB parser 预算，Memory 输入预算仍为 32 MiB；没有接受部分结果或削减枚举工作量。
- 正式序列按四个独立进程组成 ABBA/BAAB time block；失败、环境越界、缺失或阶段完整性问题保留原始记录，并使完整资格不通过，不挑选慢快样本后补足。
- 主量为 `1-exp(mean(block mean(log(A3))-mean(log(A1))))`，因此应称配对几何时间比的净增量；bootstrap 以整个四 session block 的差值为单位，不将单次运行或内层循环视作独立样本。
- [pilot 冻结计划](../artifacts/arm64-20261030/paired/formal-plan.json) 记录 6 个有效 pilot block，未截断需求为 179，正式上限为 96，因此 `precision_unmet=true`。96 是冻结计划，不能当作 96 个 block 已经成功。分析器保留硬限制：即使后续点估计有利，也不因此消除此限制或自动启用 BIL。

正式性能结果由独立采集/分析回执负责，本文不作性能判定。该真实 DEX 解析任务也不是 BIL 当前 DexKit/player 热路径；即使独立任务出现正净收益，仍需另行确认 BIL 实际热点、存储/寿命、接入成本及回归条件。

## 独立保留的缺失条件

| 条件 | 状态与边界 |
|---|---|
| 真实 16 KB loader | `UNAVAILABLE`：本机实际是 4096 B；ELF 对齐不替代真实 16 KB 运行 |
| MTE / HWASan | `UNAVAILABLE / 未验证`：没有本测试进程实际模式及受控错误资格，不宣称硬件不支持 |
| 同完成量能量 | `UNAVAILABLE`：没有合格采集方法，不由运行时间推导节电 |
| RSS/PSS、PMU/PFN | 未采集或不可用；backing 逻辑统计不等于驻留或翻译机制 |
| BIL / 家族产品集成 | 未由本次 native 功能测试验收 |
| 正式发布资格 | 本文只提供记录中的 arm64/4 KB 制品功能证据 |

这些缺失条件分别跟踪，不伪造成通过，也不反向否定已经实际完成的本机 4 KB CPU 功能验证。
