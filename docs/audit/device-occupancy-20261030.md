# arm64 真机占用审计（2026-10-30）

初次审计结论：**UNRESOLVED_OCCUPANCY / 尚不能确认空闲**。以下初次审计在约 11:21–11:24（Asia/Shanghai）只读核对可访问的其他聊天、协调文件和主机进程；当时未向设备发送 ADB 命令，也没有安装、启动、停止、解冻或重启任何设备任务。目标设备以 `arm64-PMA110/API37` 标识，完整序列号只留在原本地协调记录中。

后续已解决：用户明确授权跨聊天协调后，原 owner 回复“设备已释放、可供 Memory 使用”，保留 GPU 轮次为 INTERRUPTED_INCOMPLETE，只停止其隔离诊断进程并保全应用数据，没有排队设备任务。根代理实时复核原 PID 与 instrumentation 已不存在，再运行 Memory arm64 核心/C/C++ 三组安全程序，全部通过。释放证据及哈希见 `artifacts/arm64-20261030/device-admission.json`，实际准入/前后检查与结果见 `artifacts/arm64-20261030/safety/receipt.json`。原 GPU 未完成状态没有被改写为通过；本次设备时段仍采用会话协调与观察门禁，不声称有全局强制设备锁。

## 明确的未完结证据

`MEGUVIZ_PACK_WORKSPACE/coordination/verification/gpu-inflight-gate-1793256395431998300/DEVICE-RUN.md:5`（文件修改时间 2026-10-30 09:51:46）明确记载：`gpu_gate_v2_1793324951` 启动后 ADB 中断；重连后原测试进程仍存在，cgroup 冻结，因此该轮未完成，不能重启或记为通过。这是另一会话的实际设备任务，不能因为没有持续主机 ADB 进程就当作它已经结束。

对应本地收据目录：

`MEGUVIZ_PACK_WORKSPACE/coordination/validation/gpu-inflight-gpu_gate_v2_1793324951/reconnect-inspection-1793325100/`

- `device-summary.json:4–5`：run_id 为上述轮次，state 为 `RUNNING`。
- `pid.txt:1`：最后观测的原进程 PID 为 `21672`。
- `cgroup-events.txt:1–2`：`populated 1`、`frozen 1`。
- 原采集目录 `summary.json` 与 instrumentation stdout/stderr 记录了中断/未完整导出，不构成任务完成或资源释放证明。

这些收据是约 09:51 的历史观测；本代理没有把它们冒充 11:24 的实时设备状态。它们构成需要实时排除的未结束占用，而不是足够的“当前空闲”证明。

## 聊天与主机侧交叉核对

只读读取 Codex 聊天 `01a1814c-ad83-7681-833e-fdf32a513e45`（MeguvizPack）。该聊天仍为 active/inProgress。可访问的最近内容集中于 ShaderTools 覆盖目录、schema/解析器、主机测试和 BIL 接管锁原型，并未看见上述 GPU 轮次完成、取消或释放设备的明确回执。该 turn 内最后可见的设备命令仍在检查 PID `21672` 的 cgroup，后续大量消息转入主机工作。

11:21 的 Win32_Process 快照未发现匹配的 ADB/Python；11:23:48 的收尾采样出现 ADB server PID `5320`（11:21:53 启动，命令为 `adb -L tcp:5037 fork-server server`），以及 ShaderTools 的主机 `verify_host.py` PID `33992`，未发现指向原 GPU 轮次的采集器。ADB server 的存在不代表某项设备测试正在执行，缺少长期客户端也不表示设备测试已经结束。设备上的 instrumentation 可以在主机连接结束后继续存在或被 OS 冻结，因此这些快照不能否定前述任务 owner。

在可访问的 `coordination/tools` 和非源码快照验证脚本中，没有找到各项目共同使用、可证明已持有/释放的设备 lease/lock 协议。部分历史 offline 验证目录拒绝访问，搜索并非整个磁盘的排他性证明。构建 `DEVELOPMENT_LOCK.json` 是源码/SDK 身份锁，不是设备租约。

## 根代理后续准入条件

开始 Memory 真机任务之前，需只读实时核查原 GPU 测试进程、instrumentation、cgroup 和 `files/gpu-inflight/gpu_gate_v2_1793324951/summary.json`。原进程或 RUNNING/冻结任务仍在时，不应启动会影响独占测量的 Memory 任务；不得自行 force-stop、解冻、清数据或重启来制造空闲。没有原 owner 的完成/释放回执时，应把本轮占用资格保留为未确认。

即使实时进程已不存在，也应记录相应时间、进程/仪器列表与其他 active 会话当前活动，明确这是短期观察性准入，并在正式配对采样前后再次检查。尚未发现共享租约协议，不能声称已取得能阻止其他会话随后使用设备的排他锁。

该审计本身没有提供 arm64 Memory 安全或性能结果，也没有改变任何外部项目、设备状态或任务状态。
