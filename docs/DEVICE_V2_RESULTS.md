# v2 新设备结果与接入决定

2026-10-30。**功能通过；DEX正式采集因环境门槛中止，图像正式采集因pilot精度不足未开始；BIL保持不接入。** 没有取得稳定性能、RSS/能量或产品资格。

设备为用户新接入并明确授权的2210132C/nuwa，API33、arm64、4096B页、CPU2（允许0–7）。原PMA110的另一个会话GPU测试未被终止。新设备core3（11组/145,033检查、C backing归零、C++5项）以及消费者196项（DEX32、图像workspace156、图像perf8）均实际通过。

## 采集结果

| 阶段 | 实际结果 | 判定 |
|---|---|---|
| 初始pilot | 首窗口28次native全部成功，但触发110秒session上限；未完成128次pilot | 单独保留，不混池 |
| 唯一工程重试pilot | 4窗口/128任务全部通过，每family64次 | DEX准入formal；图像停止 |
| 图像formal | Memory净收益规划log半宽0.010362410262851413 > 0.01 | 0次formal，不追加样本 |
| DEX formal | 4完整窗口、64次native成功；第5窗口首任务在执行前被拒绝，共65条记录/计划192 | INCOMPLETE，未取得资格 |

DEX拒绝原因是CPU2的5次预准入背景忙碌率分别为100%、100%、84.444%、61.364%、100%，均超过冻结的50%门槛。最后一条 `native=null`、`command_result=null`，没有启动D-C1原生任务；它不是Memory/解析器失败，也不是成功耗时样本。没有删除失败行、补齐窗口或再次重试。其他设备环境原因未归因给某个应用或用户操作。

首轮工程中止后、未查看任何对比效果前，已公开登记唯一一次调度修正：窗口间隔120→240秒、session110→210秒、总体2400→4500秒；native、完整工作、环境/资源门槛及统计方法均不变。原28行、两套冻结计划和源码精确归档分别保留，修正不算库性能收益。

## DEX描述性点估计

下表仅来自4个完整正式窗口、64次native。**正式计划要求12个窗口，因此不提供该计划的区间或达成精度，不把点估计当稳定收益。** 正数代表候选耗时下降；负数代表候选耗时增加。

| 对比 | 归因 | 耗时下降点估计 | source＋input唯一请求容量比 |
|---|---|---:|---:|
| D-Z0 / D-C0 | 普通直接借用相对普通复制 | +1.197% | 0.5 |
| D-C1 / D-C0 | 同复制合同的Memory接口增量 | −0.166% | 1.0 |
| D-C2 / D-C1 | arena相对Memory System | +0.081% | 1.0 |
| D-C2 / D-Z0 | Memory复制方案相对普通借用 | −1.298% | 2.0 |
| D-C2 / D-C0 | 辅助：arena相对普通复制 | −0.085% | 1.0 |

直接借用取消10,540,184B的额外input backing及一遍复制；这属于普通消费者优化。该容量口径不含parser内部、metadata或RSS/PSS。普通借用的额外backing为0，故Memory/普通借用的“额外backing比”分母为0，应为null；上表使用共同source加input容量，避免无效除法。

Memory整体方案相对普通借用没有建立净收益，且该任务的source加input容量为2倍，超过预声明1.05门槛。百分比均直接从相应配对计算，未相加。所选任务仅为冻结DexMeguviz的真实DEX解析及完整54,147方法/文本消费，不能外推为BIL的DexKit/播放器热点。

## 图像资源结果

图像完成64次pilot，但Memory净差异精度未达预设目标，formal未运行，不给正式时间收益。1080p直接System的full workspace请求10,368,000B，ring为8,300,160B；ring arena backing为8,359,936B。逻辑payload减少2,067,840B，arena backing减少2,008,064B，归普通布局优化。普通持久workspace的稳态provider申请已为0，不能再计为Memory新增收益。该对照仍保留共同source→RGB复制，不声称是所有图像API的最优普通实现。

## 采后分析展示纠正

冻结分析器对不完整formal曾错误输出df11区间和“精度达标”字段。原输出为 `formal-dex.json` / `formal-dex-frozen-analysis.json`，其相关区间**已撤销，不应使用**。原分析器和测试精确字节保存在 `frozen-suite-analyzer-initial.txt` / `frozen-suite-tests-initial.txt`。

权威更正视图为 `formal-dex-corrected.json`：点估计、资源、任务、pilot决定和INCOMPLETE结论完全不变；不完整或无效formal的区间、半宽及达成精度置null并给出原因。新43项离线回归验证pilot和有效12窗口的完整输出与旧版逐值相同。纠正发生在设备采集结束后，未新增任何样本，也不提升资格。见 `analysis-correction.json`。

## BIL与剩余资格

本批未改BIL，核查main仍为00572940c354fbec7b6f311d4ecd6f02e66ea7c5，原有未提交工作保留。P1-06/P1-07/P2-05、持久staging、ncnn/GPU复用均排除在Memory增量外。没有稳定净收益或产品桥接证明，继续不引入Memory依赖或默认开关。

新设备窗口已结束：无本批native残留、无活跃instrumentation，boot与5份冻结制品哈希回读一致；独立目录保留，没有停止别人的进程、清应用数据或修改全局策略。没有后续排队任务。

实际16KB、MTE/HWASan进程模式及故障试验、RSS/PSS、能量、PMU/PFN、长期驻留域和BIL产品回归分别仍未取得资格。本轮不会为凑齐计划继续采样；将来若开展新研究，需独立说明新问题、设备环境与协议，不能补入本轮。

统计可用公开的protocol/plan/task hash-chain复算；真实APK/DEX与准入授权正文未发布。工程包提供原SDK、消费者及脱敏证据，实际重跑仍需要合法输入和新的设备准入。
