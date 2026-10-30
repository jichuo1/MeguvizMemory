# 研究材料复核与本轮实验边界

复核日期：2026-10-30（Asia/Shanghai）。资料原件保留在 `research/2026-10-28/`。本轮把附件作为研究输入，没有把其中的角色、操作指令或历史状态当作当前仓库事实。

## 已确认的研究合同

`QUANTITATIVE_ASSESSMENT.md` 是 A0…A4 和增量分母的主口径。A0 为固定历史实现；A1 为常规消费者优化后的 System 路径；A2 只有在相同 native backing、初始化、所有权、布局和工作量下才隔离纯接口成本；A3 包含 arena 与生命周期及接入成本；A4 需要另行取得 placement 资格。Java direct 迁入 native 同时改变存储语义时，A2−A1 是迁移与接入总差。未实现或未测量的版本保持缺失，不构造虚假对照。

3%–5% 是候选验证目标；3.55%、0.20%、11.50%、−0.35% 是分别给定 allocator、暴露翻译成本和新增成本的条件模型。它们不是设备数据、概率、区间或芯片结论。`QUANTIFICATION_INPUTS.json` 中的成本份额、8% 既有优化、场景权重、64 MiB/1 GiB 例子和功率变化全部是假设。`MEASUREMENT_TEMPLATE.json` 的未知实测字段应继续为 null，不能用模型补齐。

收益分母分别为 `(A0−A4)/A0`、`(A1−A4)/A1`、`(A1−A3)/A1` 和 `(A3−A4)/A3`。接口开销只扣一次。累计 requested、allocation 次数、唯一 backing、retained、retiring、RSS/PSS、GPU 容量各自记录；前两项下降不能换算为同百分比的时间或 RSS 下降。多线程 CPU time 之和也不能替代 elapsed 关键路径。

研究的 BIL 历史 SHA 为 `74d78e19095baa006b1315001190241952f1b84a`。准备路径计数公式、23/29/35 次 process、旧 `medianMs` 取五次第二快以及 warm 包含分配，都只是这个固定源码的描述，应用于当前 HEAD 前必须重新审计。P1-07/scratch 与 packed reader 的既有收益归 A0→A1；没有实际使用安装后 Memory API 的 Java 优化不能充当 Memory 消费者。

## TLB 主张的证据状态

已读取 `TLB_CLAIMS.json` 的分类、作者模型 C01…C14 和缺失项 U01…U05，以及 `TLB_IMPLEMENTATION_PLAN.md` 的实验与资格合同。台账把视频公开图示、作者实验、推断和不可用信息分别记录。其“1024 组/4 路”“16 KiB 子区/32 KiB 双槽”“bit 索引”和条件覆盖量属于作者模型或推断，不是本项目实测，也不是已取得的厂商硬件 ABI。

资料包没有附带台账引用的 `video-evidence/` metadata、sprite、裁图、音轨、字幕、作者微基准或原始 PMU 样本。本轮能复核台账结构与限定语，不能独立目视那些历史裁图，也没有重新确认视频具体数字。保留原资料的历史取证状态，同时明确当前可访问证据边界；不把台账阅读升级成硬件复现。

基础页、PTE contiguous hint、CPU 自主聚合、TLB 几何、VA 相邻、PA 相容性与 cache 局部性必须分别处理。没有 PFN/PMU 事件与权限时机制为 UNKNOWN/UNAVAILABLE。VA/ELF 对齐、单次大分配或 hint 返回成功都不能证明 PA 连续或翻译聚合。通用 DTLB refill/miss 不能自动称 page walk。

本轮默认 System provider 与 dense，特殊 placement、page hint、别名实验均 NOT_SELECTED。没有候选净收益时保持简单路径是合格研究结果。禁止 MAP_FIXED、修改系统 THP/sysfs/ASLR/MTE、接管 ART/direct buffer 或触及外部 PFN/PTE。任何后续策略只作用于自有区域，不能搬迁已交出稳定地址的活 allocation。

## 已冻结的可执行子协议

`bench/protocol.json` 的 `MM-003-host-functional-diagnostic-v1` 在采样前定义宿主功能与诊断范围，输出 `MM-003.protocol_ready=READY`。这使 MM-021 的安全 harness 可继续开发，无需等待整个真机 baseline 或 MM-020 的所有视频主张完成。此输出不代表 MM-003 真机 baseline 已完成。

首批验证包括：安装制品后的真实离线解析、不同算法与数据流的 CPU 图像工作区、以及所有权/预算/关闭/迟到完成的功能合同。具体格式、输入 hash、算法、尺寸、源码清单和安装制品 hash 必须由实际 case manifest 补齐。两个只包装同一示例函数的程序不满足独立消费者资格。

MM-021 首个可执行 profile 为独立进程的 owned-region、无别名、无 hint 模式。每例必须检查 span/offset/stride/对齐溢出、节点不跨页不重叠、VA 与实际触页预算；先写入私有 backing，再分开记录初始化、first-touch、warm、steady 和释放。至少控制页内 cache-line 偏移并记录依赖链/独立流，避免把同 offset 的 cache 冲突解释为 TLB。profile 全量 case 矩阵未实现时只能报告已执行子集，不能宣称 MM-021 全部通过。

本轮为 FUNCTIONAL_AND_PILOT_ONLY。计时与诊断轮分开，保留全部失败与回退。未来正式性能比较先完成独立 pilot，再冻结样本量、配对均衡顺序、主指标、回归预算、温度/迁核排除阈值。独立 session/时间块是统计单位，循环中的数百万 load 不是独立样本；样本不足不发布 p95/p99 或精确置信区间。真实设备、SDK、OS/页配置、MTE、能量和产品性能各自验收。

## 原研究算术复现

已审阅资料包中的三个 Python calculator；它们仅使用标准库进行算术和 JSON 读写，无安装依赖或联网动作。PATH 中没有 python/py/python3。`uv python find --no-python-downloads` 先遇到默认 cache 创建问题，使用工作区 cache 后其 managed Python 安装目录读取被拒；本轮没有下载 Python 或请求扩大权限。

已找到并使用现成的 `D:\stable-diffusion-webui\venv\Scripts\python.exe`，版本 Python 3.10.6。执行时设置 `PYTHONUTF8=1`，避免 Windows 默认编码影响包含中文的输入。以 `runpy.run_path` 加载脚本并调用其 `calculate()`，把输出写入 `build/research-check/`，用 Python JSON 对象相等比较原结果：

| 原脚本 | 对应原 JSON | 复现结果 |
| --- | --- | --- |
| `calculate_models.py` | `MODEL_RESULTS.json` | 语义完全相同 |
| `calculate_quantification.py` | `QUANTIFICATION_RESULTS.json` | 语义完全相同 |
| `calculate_tlb_models.py` | `TLB_MODEL_RESULTS.json` | 语义完全相同 |

实际执行包括 `& 'D:\stable-diffusion-webui\venv\Scripts\python.exe' --version` 及经标准输入执行的比较脚本，后者逐个运行三份 `calculate()`、保存 JSON、执行 `assert all(row['semantic_equal'] for row in receipt['results'])`。命令退出码为 0。收据 `build/research-check/receipt.json` 保存 Python 版本、三份脚本 hash、三份原 JSON hash 和逐项 `semantic_equal=true`。生成 JSON 的空白和换行不要求与附件字节相等；比较的是完整数据结构及数值。

研究算术重现不构成 CPU、Android、PMU、allocator、RSS/PSS、能量或产品性能实测。

## 当前状态与直接后续依据

| 任务或证据 | 当前结论 |
| --- | --- |
| MM-003 protocol_ready | READY，仅本轮宿主功能/诊断子协议 |
| MM-003 真机 baseline | 未完成，等待实际场景、制品和设备采集 |
| MM-020 台账复核 | 已读原台账并确认限定语；引用的图像原件缺失，硬件机制未复现 |
| MM-021 harness 功能 | 本文件定义需求；以实际实现/执行收据更新，不由文档声明 PASS |
| MM-021/024 设备与机制 | 本轮无合格证据，UNKNOWN/UNAVAILABLE |
| MM-023 特殊 placement | NOT_SELECTED，当前没有启动实现的候选证据 |
| 性能与发布资格 | NOT_STARTED，不从模型、静态审计或宿主功能结果推导 |

下一步依据是核心 API 的真实构建/安装与消费者输出。待消费者审计固定当前源码和既有优化状态后，再确定 MM-012/022 是否存在值得回放的热点；没有热点时保留简单 provider。
