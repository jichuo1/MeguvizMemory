# BIL 接入决策

日期：2026-10-30。**决策：本批不把 MeguvizMemory 接入 BIL，保持现有 System/Java direct/既有 owner 路径。** 这不是核心功能失败，也不声称所有 Memory 用途无效；当前真实 CPU 实验没有建立稳定净收益，且所选独立 DexMeguviz 任务不是 BIL 的生产 DexKit/播放器热点。

本批核定 BIL main 为 `00572940c354fbec7b6f311d4ecd6f02e66ea7c5`。没有在该 checkout 修改 Memory 依赖、设置默认开关或替换 parser/帧资源 owner。已有 P1-07 scratch（`e630ad56`）与 P2-05 holder 打包（`061360f5`）保留为 A1 的既有能力，不计入本库增量；准备流程的初始化、输入独立区间、成功条件和第二快统计也没有改变。

## 决策依据

| 问题 | 实际证据 | 决策含义 |
|---|---|---|
| arm64 生命周期/预算/关闭可用吗？ | 本批记录的 API 37、4 KiB arm64 真机安全测试通过，已有 lease 最后访问前不得回收、预算失败与关闭合同有回执 | 可以记录此设备/制品范围的功能通过，不能替代性能资格 |
| 选定真实 CPU 工作量可完成吗？ | 真实 9.17 APK `classes.dex`，10,540,184 B，完整消费 54,147 方法；A1/A3 的 SHA/checksum、关闭归零一致 | 独立任务功能成立，未虚构减工作量或部分成功 |
| 完整的一轮配对性能数据有吗？ | v2 32 块/128 task 完整 PASS；v1 收集器失败保留，不混池 | 采集完整性成立，原生功能错误与收集器错误分开 |
| 稳定时间净收益达标吗？ | v2 探索性点估计 1.0794%，探索性 95% 区间 [-0.0234%,2.3316%]；mean log-ratio 半段偏移 -0.0106034，超过预注册 0.01 | 点估计低于 3%、区间含零，趋势警告触发；最终 **INCONCLUSIVE**，不能启用 |
| 可控 backing 有净节省吗？ | A1/A3 同为 10,540,184 B，比值 1.0 | 无本批 input-backing 节省，不存在可用于替代时间门槛的容量优势 |
| 能直接外推 BIL 吗？ | 未测 BIL 生产热点、bridge/JNI 成本、首帧/deadline、降级率与真实业务输入分布 | 不具备产品接入或默认启用资格 |

结果和原始链路详见 [PERFORMANCE_A1_A3.md](PERFORMANCE_A1_A3.md)、[v2 report](../artifacts/arm64-20261030/paired-retry1/report.json) 及 [v1 report](../artifacts/arm64-20261030/paired/report.json)。完整 v2 的 PASS 指采集/语义/环境回执完成；分析状态仍为 `DESCRIPTIVE_ONLY_QUALIFICATION_NOT_PASSED`。不把约 1.08% 写成已确认的库收益，也不把探索性区间上界当作严格排除更大真实效应的证明。

## 对照边界

A1 是与选定新 input-owner 合同一致的单份 dense、64 B 对齐 System backing；A3 为安装后的 Memory arena，包含所有申请、初始化/复制、解析、完整查询消费和最终生命周期开销。双方相同的文件预读/进程启动/loader 不在 CPU 主计时内，parser 内部 allocation 由原 SDK 管理，不算 Memory 的节省。

双方都从共同预读 vector 复制到独立 input owner；若业务可以直接让 SDK 借用并保活原 vector，可能无需这份新 owner/复制。因此本 A1 不是最佳 zero-copy 路径的替身，真实 APK 输入也不会自动使这个任务成为 BIL 当前 DexKit 路径。没有凭输入包名或库名称建立生产热点等价。

64 MiB parser 预算在任何 pilot/formal 前拒绝完整任务，失败原样保留；随后 A1/A3 同步固定 256 MiB，input budget 仍为 32 MiB。没有在正式结果之后放宽预算、降低档位、缩短查询或变更成功定义。

## 工程失败与退出

v1 的宿主 Windows 原子 JSON 替换失败，使末 after-environment 未取得；144 个 native task 成功不等于 144 个完整资格样本。收集器有界 retry/journal 修复后，仅按事先登记的一次独立 v2 pilot/实验重试，native binary、计时边界、门槛与 600 s 限制不变。收集器吞吐/可靠性改善不计为 Memory 性能改善。

唯一一次重试已结束，不追加 pilot、不补齐 v1、不合并两轮、不根据趋势放宽阈值。设备已由主代理协调释放。当前工作在此证据下结束性能启用决策，保持 BIL 现有实现。

## 独立未满足资格

真实 16 KiB loader、MTE/HWASan、RSS/PSS/全进程峰值、同完成量能量、产品端到端与非目标回归均没有本次通过证据；PMU/PFN 与 TLB 机制也未确认。它们独立列为未验证/阻塞，不把逻辑 backing、4 KiB 功能通过或 native CPU elapsed 转换成这些结论。

未来只有出现新的、实际属于 BIL 的自有 native CPU 工作负载和对应 owner/寿命合同，且在合格 A1 上证明可测的接入价值，才有另行开展集成评审的直接依据；这不是继续本轮试到通过的授权。现有 allocation、Java direct、GPU retirement 与帧调度继续交给其原 owner。
