# A1/A3 配对统计审阅草案

日期：2026-10-30。对应 MM-003 / MM-012 / MM-022。状态为 **DRAFT**；尚未选定真实 CPU 工作负载，也未获知真机占用/热环境，因此本文件不是正式采样冻结回执。机器可读草案是 `bench/paired_protocol_draft.json`。本代理没有操作设备或执行性能负载。

已读依据是 `bench/protocol.json`、`docs/BASELINE_PROTOCOL.md`、`consumers/image/main.cpp` 和 `include/mgmem/mgmem.h`。原协议只冻结宿主功能和安全诊断，明确正式性能尚未启动；不能沿用其 READY 字段，把新一轮真机计时直接标为正式结果。

## 先保证基线公平

现有图像 `--system` 仍通过 Memory System API，属于 A2 功能模式，不能直接充当不接 Memory 的 A1。A1 应是相同真实任务的常规优化 System 版本：已经合理可做的 session scratch 复用、密集 backing/分区和有效范围初始化均须保留。不能让 A1 每帧重新申请，而 A3 跨帧保留同样存储，再将全部差额归功于新库。

A3 必须包含 domain/申请、必要初始化与复制、lease、算法、需要的 reset/trim，以及 begin_close/collect/drain/destroy 成本。两版必须使用相同 task/batch 边界、工作完成量和输出寿命；若文件读取放在计时外，两版都这样做，并另报读入到输出的端到端指标。原生 backing、清零或复制改变时只能称迁移与接入总差，不能称纯 ABI 税。

旧 P1-07 scratch 和 P2-05 元数据能力继续归 A1。现有 64 KiB arena 在小输入上多占 backing 的结果应保留，不能根据首轮有利结果调换分母。真实图像算法可作为独立 CPU 消费任务，但若没有 BIL 实际调用和热点证据，不能仅凭它变快就接入 BIL。

## 建议采用的有界采样

每个统计单位是一个预先定义的四 session 时间块，顺序为 ABBA 或 BAAB（A=A1，B=A3），两种顺序数量平衡，用预先提交的 seed 打乱。每个 session 是新的独立任务/owner；同一时间块使用相同输入序列及完成量。帧、记录和内部循环不充当独立样本。ABBA 用来减小块内近似线性漂移影响，不能保证消除温度、频率、缓存或块间依赖。

先用 6 个 pilot block 选定单一固定工作量，目标每个 session 约 250 ms，硬上限 1 s；不按两种版本各自速度选择迭代次数。pilot 不合并进正式估计。然后根据 pilot 的配对 log-ratio 标准差 s，使用 `ceil((1.96*s/0.01)^2)` 作为约 1 个百分点 log-ratio 半宽的规划近似，至少 32 个、最多 96 个正式 block，向上取 8 的倍数。记录计算与 cap 是否触发后冻结 N，不因看到有利结果提前停止，也不在结果不理想时增加 N。小 pilot 的公式不是保证精度或正式功效证明；cap 不足则 INCONCLUSIVE。

建议每个主 profile 的正式总墙钟上限 600 s、额外 cooldown 上限 300 s；单次进程超时 15 s，自有 backing 暂以 64 MiB 上限约束。实际选定输入如果需要其他预算，须先修改并冻结，不能看到失败才放宽。设备占用未确认前不启动 pilot。只操作本 benchmark 自身允许的 CPU affinity；不改全局 governor、温控、THP、sysfs 或 MTE。

## 指标与推断

主指标为相同完成量完整任务的 monotonic elapsed，分母是 A1。对每块计算 `d=mean(log(T_A3))-mean(log(T_A1))`，主点估计 `g=1-exp(mean(d))`，明确这是配对几何时间比。预先固定 seed 做 20,000 次整块 bootstrap，给双侧 95% percentile 区间，再按 `1-exp(d)` 逆序变换端点。可以补报配对算术时间比作为次要敏感性分析，不能挑收益最高的估计器。

时间块并非天然独立。必须保留按时间排列的 d、热环境和运行顺序；若有明显持续趋势或块间相关性，bootstrap 区间只能作为探索性结果，需要更长间隔的独立时段再验证。32–96 个 block 不足以产生精确的产品 p95/p99。多个尺寸或 workload 必须指定唯一主场景，其余单独标次要/探索，不能事后择优。

## 失败与环境记录

温度、后台占用的 sensor、单位和数值门槛此时都留空，根代理必须根据设备真正可读取的观测，在正式采集前固定。不能把电池温度称 CPU 温度；begin/end CPU 相同也不能当作“迁核为零”。PMU、频率、迁核计数或能量不可用时明确标 UNAVAILABLE，仍可保留限范围的黑盒计时结果。

prelaunch 环境不合格要在启动前记下 skip/cooldown；运行后发现门槛违例则整块带标记保留，并同时报告 all-attempt 与预注册 eligible subset，不能只删掉慢的一边。输出不等、失败、超时、额外 fallback 是功能/资格失败，不是应当删掉的快样本。诊断插桩和低扰动计时分轮执行；唯一 backing、live/retained/retiring、requested、缺页、RSS/PSS 分口径，不把逻辑容量当实际驻留。

## 启用决策与独立阻塞

建议时间收益门槛为：主点估计至少 3%，且配对 95% 收益区间下界大于零；或 backing 峰值至少减少 10%，同时 95% 时间比上界不超过 1.02。两条路径都要求安全/语义零已知失败、自有 backing 不超过预注册 5% 回归预算、非目标时间不超过 2% 回归预算。不能确定区间时不判 PASS；这些数字是要在正式采样前确认的工程标准，不是必须证明的结论。

即便独立 CPU 任务达到门槛，也只有在 BIL 使用同一函数、输入分布/寿命和热点得到证实、接入成本与宿主回归通过后，才有默认接入依据。没有净收益或无法判断时保留当前 System/BIL 路径，并保存 NO_GAIN/INCONCLUSIVE 及全部样本。

真实 16 KiB loader、MTE/HWASan、能量与机制证据各自保留未验证/阻塞，不应阻止普通 arm64 4 KiB 功能测试或有效黑盒 A1/A3 实验，也不能由其结果代为通过。
