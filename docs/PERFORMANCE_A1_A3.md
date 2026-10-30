# arm64 A1/A3 真实 CPU 任务实验

日期：2026-10-30。**结论：功能与完整采集已通过，性能资格为 INCONCLUSIVE，本批不据此接入 BIL。** 第二轮完成全部 32 个时间块、128 个独立原生任务；探索性耗时下降点估计为 **1.0794%**，探索性 95% 区间为 **[-0.0234%, 2.3316%]**。点估计未达到预注册 3% 门槛，区间含零，且触发预注册时间趋势警告，因此不能称为已确认净收益。原始精确值、失败和状态以各轮报告为准。

## 工作量、版本与计时合同

选定场景是独立的真实 DexMeguviz 任务：读取用户本地既有 Bilibili 9.17.0（9170100）APK 的 `classes.dex`，建立 borrowed snapshot，执行方法查询并完整消费每条方法及文本字段。每个独立进程完成一次全量解析/枚举；这不是 malloc 循环，也不是现有 BIL DexKit 或播放器路径的替换。

| 固定项 | 本批值 |
|---|---|
| 输入 | DEX037，10,540,184 B |
| 输入 SHA256 | `83ac99bc338e8bfc1980e382a4c318871cac0364dc449ae7b0316f601c136ab0` |
| 完整方法数 / 输出 checksum | 54,147 / `12701547145945416225` |
| 真机范围 | PMA110，API 37，arm64-v8a，实际基础页 4,096 B；固定本进程 CPU2 affinity |
| Memory/原生 input backing 预算 | 两版均 32 MiB |
| Dex 自有 parser 预算 | 两版均 256 MiB；parser 内部 allocation 保持原 owner |
| benchmark 源码提交 | `58765309752321c52dfeabb2f09b33dc998ca3a8` |
| arm64 executable SHA256 | `c282433806a60937d15c98222f0f37005495b2fb35ba7596839cb2b9442b1391` |
| 安装后 Memory SO SHA256 | `9906104f022f18d9946deed60a5002bf4a8eadda71f8b051f4467d01d26dd919` |

输入 APK/DEX 不向公共仓库分发，仅发布来源/hash 和观测记录。工作量合同见 [real-dex-workload.json](../bench/real-dex-workload.json)，安全前置证据见 [arm64 safety receipt](../artifacts/arm64-20261030/safety/receipt.json)。

A1 用 System 分配的一份密集、64 B 对齐 input backing，显式预算和 refcount 寿命，直接调用既有 borrowed-source SDK，不调用 Memory API。A3 用安装后的 Memory arena，采用相同 input 长度、对齐、初始化、borrowed SDK、输出寿命和工作量；arena chunk 等于这份输入长度。两版都只为选定的新 input-owner 合同建立一份 backing，没有故意让 A1 做逐记录/逐帧分配。

这个 A1 不是所有可能实现中的最优 zero-copy 基线：两版都已有计时前预读 vector，再复制到独立 input owner。若调用方可以直接保活预读 vector 并交给原 SDK 借用，另一次 backing/复制可能不需要。本实验只回答所选新 input-owner 合同下的 A1/A3 差异，不能用来证明 Memory 优于最佳现成输入路径。A2 纯接口成本与 A4 placement 均未单独选择或主张。

CPU 主计时从 owner setup/必要 memcpy 开始，包含 engine/snapshot 创建、解析、完整查询与消费、SDK/context 释放以及 A3 owner/lease/collect/drain/destroy。文件预读、进程启动和 loader 不在主 elapsed 中，两版边界相同；预读耗时另记。不能将该结果写成 APK 打开到结果、BIL 启动或播放的端到端收益。详细 phase 诊断与正式计时分轮，采集器墙钟也不是 native 主指标。

在任何 pilot/formal 之前，64 MiB parser 预算预检对完整查询显式失败，原始错误为 `query_methods_dex_4`。该失败未当作快速成功或部分结果；保留在 [host-A1-parser-budget64.json](../artifacts/arm64-20261030/failures/host-A1-parser-budget64.json)，随后双方统一固定为 256 MiB parser 预算，Memory input 预算仍是 32 MiB。正式采样过程中没有放宽预算或减少工作量。

## 两轮分别保留，不混池

| 轮次 | pilot 与冻结样本量 | formal 完整性 | 结论 |
|---|---|---|---|
| v1：`paired` | 独立 6 块；规划未截断 N=179，按既定上限冻结 96 块，`precision_unmet=true` | 观察到 36 块、144 次 native 成功；第 35 块 position 3 的 after-environment 因宿主 Windows 原子替换失败缺失，仅 35 个完整有效块 | INCONCLUSIVE：未完成 96 块、规划精度不足，且存在时间趋势警告 |
| v2：`paired-retry1` | 仅一次新实验/新 pilot；6 块，规划未截断 N=13，按最小值冻结 32 块，`precision_unmet=false` | **32/32 块、128/128 task** 完成，采集 receipt PASS；全部输入/结果、affinity、最终 backing/lease 合同通过 | **INCONCLUSIVE**：趋势警告阻止确认性资格；点估计与区间也不满足预注册时间收益门槛 |

每块是 ABBA 或 BAAB 的四个独立进程/task session；两种顺序平衡并在正式采集前固定。统计单位是块，不是 128 个相互独立的帧/记录。pilot 不进入正式估计；v1 与 v2 不合并，未用第二轮补齐第一轮。

v1 的停止原因是 Windows `formal-receipt.json.pending → formal-receipt.json` 原子替换 `PermissionError`，不是 native 算法、Memory 预算或关闭失败。144 个 native 结果均成功，但最后一条采集 row 的 `success/valid_environment=false`、`environment_after=null`，不能算完整资格通过；缺失数据没有补造。见 [v1 report](../artifacts/arm64-20261030/paired/report.json)、[v1 formal receipt](../artifacts/arm64-20261030/paired/formal-receipt.json) 和同目录 `formal.jsonl`。

修复仅涉及宿主采集器：对 `PermissionError` 做最多 0.9 s 的有界 retry，超过仍显式失败；changed public command 以 revision journal 追加并 fsync，完整 receipt 每 2 s 限频，状态改变/结束强制保存。真实 Windows 无删除共享 reader 测试中，本批归档的短 hold 于 0.172 s 后恢复，持续 hold 时 0.906 s 明确返回 PermissionError，旧/新 JSON 完整且 journal 可恢复。见 [capture-journal-regression.json](../artifacts/arm64-20261030/capture-journal-regression.json)。这是收集可靠性修复，不能称为 Memory 或算法性能改善。

v2 在新 pilot 前登记了唯一一次工程重试：native executable、计时边界、环境阈值和 600 s 上限均不变；若新 pilot 要求 N>96 则停止，不重复 pilot 寻找有利结果。本次新 pilot 允许冻结 32 块。协议见 [v2 pilot protocol](../artifacts/arm64-20261030/paired-retry1/pilot-protocol.json)、[formal plan](../artifacts/arm64-20261030/paired-retry1/formal-plan.json) 和 [formal protocol](../artifacts/arm64-20261030/paired-retry1/formal-protocol.json)。

## 第二轮结果及其限度

主估计器为 `g = 1 - exp(mean(d_b))`，其中 `d_b = mean(log(T_A3)) - mean(log(T_A1))`。分母是 A1；正值表示所选完整 CPU 任务的耗时下降。整块 bootstrap 20,000 次、seed 20261030；不是把 phase 相加或把 CPU time 当 elapsed。

| v2 观测 | 数值 / 资格 |
|---|---|
| 配对时间比 | 0.9892061008 |
| 探索性耗时下降点估计 | 1.0793899% |
| 探索性双侧 95% 收益区间 | [-0.0234357%, 2.3315899%] |
| 前后半块的 mean log-ratio 偏移 | -0.0106034351；绝对值大于冻结阈值 0.01，触发警告 |
| lag-1 相关系数 | 0.1446657；未触发该项 0.5 阈值，不代表已证明独立 |
| A1 / A3 input backing 峰值 | **10,540,184 B / 10,540,184 B**，比值 1.0 |
| 性能分析状态 | `DESCRIPTIVE_ONLY_QUALIFICATION_NOT_PASSED` / `INCONCLUSIVE` |

精确结果见 [v2 report.json](../artifacts/arm64-20261030/paired-retry1/report.json) 和 [formal receipt](../artifacts/arm64-20261030/paired-retry1/formal-receipt.json)。完整采集 PASS 与性能资格不是同一状态。趋势规则触发后，上述区间只能描述本批数据，不能当作确认性区间、稳定 1.08% 获益或对真实效应的严格上界。没有根据接近阈值的结果改门槛、加样本或更换估计器。

backing 仅覆盖这份自有 immutable input，不含共同预读 vector、parser/引擎内部容器、allocator 元数据或 OS 驻留。比值 1.0 说明本批没有该口径的容量收益；不能推导 RSS/PSS、全进程峰值或能量没有变化。

CPU2 只对本 benchmark 设置 affinity。pre 门槛是电池温度≤400 deci°C、OS Thermal Status=0、选定 CPU 观测 busy≤0.5，有限 cooldown；post 继续核温度/thermal。电池温度不是 CPU 温度，DVFS 只观察未控制，begin/end CPU 也不是完整迁核计数。频繁环境探测与变动频率仍限制稳定性能推断。

## 退出状态

本批唯一一次工程重试已用完，不再新增 pilot 或 formal，也不混池。设备任务已结束并由主代理协调释放；保留本批目录与证据，不停止或清除其他应用。

真实 16 KiB loader、MTE/HWASan 模式、产品能量/RSS/PSS、BIL 实际热点与接入成本仍缺证据。它们分别记录，不由本次 4 KiB arm64 功能或探索性时间数据代为通过。BIL 决策见 [BIL_ADOPTION_DECISION.md](BIL_ADOPTION_DECISION.md)。
