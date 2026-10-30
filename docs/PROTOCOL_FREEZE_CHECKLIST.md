# 新配对协议 v2：离线合同与冻结门槛

当前状态：`PROPOSED_NOT_FROZEN_NOT_EXECUTED`。本批新增离线计划生成器和记录校验器，未连接设备、部署或启动 pilot。`tools/run_paired_device_v2.py --execute` 总是以状态码 2 拒绝；即使输入 JSON 自称 FROZEN 也不会执行。尚未实现 live collector 或性能资格 PASS。

研究包的采样方案是下一次设计的建议。v1 与 retry1 的 plan、JSONL、report、collector、hash 和原 `INCONCLUSIVE` 判定全部保留。不得将新 source-owner 最终释放计时与旧 total 混池。

本方案主问题是普通 DEX 消费者删除额外 source copy。主比较 `D-Z0 / D-C0`；`D-C1 / D-C2` 是复制合同下的次级控制，不能混入主估计，也不能将普通借用收益归给 Memory。新的正式结果须先通过 SDK 合同及完整功能/生命周期/输入和产物身份门槛。

冻结前必须完成：

- 为 experiment、attempt、estimand 给出唯一身份，锁定 parent protocol 的精确字节 SHA256，记录源树与编译输入、EXE/SO/SDK 闭包和功能收据 hash。
- 独立核对设备空闲与所有者释放，再由授权执行批次冻结 device/kernel/API/page size、允许 CPU、affinity、可验证传感器和环境资格。不得把上一批空闲状态当当前设备状态。
- 预排 4 个 pilot 窗口及 12 个 formal 窗口，每窗口仅 1 个 session、每 session 8 块、每块 4 任务；各 session 有 4 个 ABBA 和 4 个 BAAB。须写明日/环境、真实窗口开始时间与间隔设计，并评估窗口间近似独立的依据。12 个标签不提供独立性证据。
- 将草案顺序冻结为独立不可替换文件；冻结发生在 pilot 前，SHA 链接该协议。一次 pilot、固定 formal 384 任务、无有利早停、不追加样本、不因结果有利改聚类或阈值。
- 冻结单任务、session、总体墙钟上限、最大环境等待次数。`null` 表示缺失，不能默认为无限或零。
- 冻结 cadence：观测顺序、实际 busy 观察窗与容忍范围、thermal 观测时点、观察到 launch 的间隔、相邻 launch 间隔范围、宿主写盘与缺失/失败处理。记录单调时钟及同一时钟 epoch；不以请求 sleep 值冒充实际间隔。
- 冻结资源预算及口径：logical、unique owned backing、外部 source capacity、borrowed view、arena used/padding/retained/reserved、metadata、parser internal、RSS/PSS、energy、最终释放。未知量写 `null` 并给 unavailable reason。不能将 source 与借用 view 重复计入 unique backing，也不能把缺失 RSS/能量解释为无回归。
- 诊断目标与正式低扰动目标分开；正式只用 total 起止计时。细分 phase、fault 或时钟探测不可混入正式估计。
- 用同一 D-C0 的独立有限诊断验证观测合同完整性；比较旧 cadence 与合并读数方案时保持资格等价，不按哪种 Memory 效应有利选择策略。该诊断不进 formal，也不能证明完全没有观测扰动。

统计合同：每块 `d = mean(log(candidate total)) - mean(log(reference total))`，每窗口 `m = mean(8 个 d)`。formal 主估计用 12 个窗口均值：`mean(m) ± t(.975,11) * sd(m) / sqrt(12)`。gain 区间端点反向变换为 `[1-exp(U), 1-exp(L)]`。窗口 cluster bootstrap 只作固定的敏感性分析，不能替换不利主区间。pilot 的 4 个窗口用相同 t11 系数粗筛未来 12 窗口精度；这不是达成精度或方差保守保证。正式必须另存实际 log 区间半宽，目标 ≤0.01。

功能/身份/生命周期失败立即停止资格；环境或宿主记录缺失为 `INCOMPLETE`；pilot 可行性不满足则 `PRECISION_INSUFFICIENT_STOP_PROTOCOL`；正式趋势（后6窗口减前6窗口的 mean log-ratio 绝对值 >0.01）、达成精度失败或外层假设不足保持 `INCONCLUSIVE`。没有趋势告警、全新进程、lag1 低均不能证明独立性。保留全部原始任务，不补块、不删除快/慢点、不按频率端点归一化。

离线工具使用新 schema，不自动转换旧 JSONL。标准库命令示例：

```powershell
python tools/run_paired_device_v2.py --protocol bench/paired_protocol_v2_draft.json --draft-plan build/v2-draft-plan.json
python tools/run_paired_device_v2.py --protocol bench/paired_protocol_v2_draft.json --check-plan build/v2-draft-plan.json
python tools/test_paired_analysis_v2.py --output build/v2-offline-test-receipt.json
python tools/paired_analysis_v2.py --protocol bench/paired_protocol_v2_draft.json --plan build/v2-draft-plan.json --journal <new-v2-journal.jsonl> --phase formal --output build/v2-report.json
```

内部对象身份统一使用 `protocol_canonical_sha256` / `plan_canonical_sha256`，取 sorted keys、compact separators、UTF-8、无 NaN 的 canonical JSON SHA256。plan 绑定 protocol 的 canonical 身份，journal record 绑定 plan 的 canonical 身份，analysis report 同时记录两者。精确文件字节使用 `protocol_file_sha256` / `plan_file_sha256` / `journal_file_sha256`；最终 offline receipt 的协议文件绑定是 `protocol_file_sha256`。`parent_protocol_sha256` 明确表示选定父协议的精确文件字节 SHA256，仍待冻结。这两种身份不可互换，格式变化可以改变文件 hash 而保持对象 canonical hash。

新 journal 每物理行有 `sequence`、`previous_sha256`、`record_sha256`、`record`、`event_sha256`，使用上述 canonical JSON 编码 SHA256 链；首 previous 为 64 个零。顺序、payload 或尾部损坏显式失败，禁止按 task ID 排序修复。文件以 `x` 模式创建，工具不覆盖既存 plan、journal 或 report。hash chain 防止未察觉损坏，不是第三方签名或设备执行证明。

`record` 规范化合同包含 phase/window/session/block/position/variant、experiment/attempt/estimand、plan_canonical_sha256、day/environment/host clock epoch、success、native、resources、environment、clocks。`native` 保留实际 v2 输出的 `source_sha256`、`input_bytes`、`method_count`、`result_checksum`、`total_ns`、`case=normal` 和最终 `closed_backing_bytes=0`、`source_final_released=true`、`lease_final_correct=true`、`final_resource_state=CLOSED_ZERO`。每行必须带 `command_result.stdout` 的精确原始文本与 `exit_code=0`；校验器严格解析 stdout 并以 canonical JSON 和 native 比较，不接受重复 key、非有限值、缺输出或差异。原生 variant 须与外层及 plan 一致；workload/schema/input_bytes 也须匹配；pilot/formal 都只接受 `diagnostic=false` 和 `phases=null` 的低扰动构建。禁止在缺失输出时合成成功值。

`resources` 各列使用字节数（energy 为 joules），未知量为 `null` 且 `unavailable_reasons` 有对应原因。`paired_analysis_v2.native_to_resources(native)` 是直接消费真实 native 输出的明确 adapter，校验器要求行资源对象与 adapter 完全一致，不能把可测值静默变成 null。映射包括 unique_owned_backing_bytes→unique_owned_backing_peak_bytes、borrowed_view_bytes→borrowed_view_peak_bytes、padding_bytes→input_backing_padding_bytes、metadata_bytes→memory_metadata_bytes、retained_bytes→retained_before_collect_bytes、reserved_bytes→reserved_final_bytes、parser_internal_bytes→parser_peak_owned_bytes、energy_joules→energy；对象的 scopes 明确区分 peak、before collect 与 final，不将其相加冒充共同峰值。不能把借用 view 与 source/backing 重复计数。`environment` 至少有 complete/valid、battery_deci_c、thermal_status、background_cpu_busy 及实际 busy 起止/thermal 采样单调时钟；所有 ns 字段必须为非 bool 正整数。`clocks` 有 observation begin/end、launch/completion。允许 unavailable 的非资格传感器必须另记，不能替代必需资格字段。采集器未完成、时段独立性和资源资格尚未冻结，因此当前校验器无任何性能 PASS 路径。

离线 receipt 中所有 synthetic 时间、环境和资源均为构造测试数据，只证明缺行、重复、乱序、缺环境、错误 checksum、关闭失败、hash 损坏、趋势与统计实现的处理行为。它们不新增真机、机制或性能证据。
