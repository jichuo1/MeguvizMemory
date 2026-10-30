# 基线与验证协议

本轮完成 CPU 功能实现与安装消费者验证。宿主 Release/ASan 均通过四项 CTest；Android API 34 x86_64 模拟器的真实 4 KiB loader 已运行核心、C 和 C++ 用例。两个不同消费者通过安装后的 Memory API 完成实际工作。正式性能、16 KiB loader、MTE、家族桥和发布资格仍未通过。

协议编号 `MM-003-host-functional-diagnostic-v1`，冻结日期 2026-10-30（Asia/Shanghai），机器可读入口为 `bench/protocol.json`。`MM-003.protocol_ready=READY` 只说明本轮宿主功能/诊断协议可执行；完整产品 baseline 仍 BLOCKED，不能把这个输出提升为 MM-003 整项 DONE。

## 固定输入与对照

研究历史 BIL 基线是 `74d78e19095baa006b1315001190241952f1b84a`。本轮只读审计的 BIL main 是 `00572940c354fbec7b6f311d4ecd6f02e66ea7c5`，原用户修改和其他 worktree 全部保留。当前 scratch 已存在于 P1-07 的历史提交 `e630ad56`，五槽 direct buffer、有效范围/初始化及第二快统计不能重新记作 Memory 收益。packed reader 在本轮所读 main 尚未实现，本批聚焦 CPU 库与两个安装消费者，没有修改 BIL。

| 版本 | 实验合同 | 本轮状态 |
| --- | --- | --- |
| A0 | 固定历史消费者源码、制品、工作量与成功资格 | 仅历史来源已识别，无本轮产品采样 |
| A1 | 常规消费者优化后的现有 System 路径 | BIL scratch 源码已确认，无本轮配对性能样本 |
| A2 | 相同 native provider/backing、初始化、owner、布局和工作量，经 Memory System API | 当前两个消费者的 System 模式只提供功能对照，尚非匹配 A1 的纯接口性能实验 |
| A3 | A1 加 Memory region/arena，包含接入成本 | 两个消费者 arena 模式功能通过，无产品净增量资格 |
| A4 | A3 加取得设备和消费者资格的 placement | NOT_SELECTED |

Java direct 迁入 native 若同时改变清零、复制或存储所有者，A2−A1 只能称迁移与接入总差。全方案、Memory、核心和 placement 的分母分别为 `(A0−A4)/A0`、`(A1−A4)/A1`、`(A1−A3)/A1`、`(A3−A4)/A3`。不存在的版本不填结果；不同分母的百分比不相加，接口成本不重复扣除。

研究的 3%–5% 是候选目标，3.55%、0.20%、11.50%、−0.35% 是条件模型。本轮没有把它们或原 calculator 输出填入 LOCAL_MEASURED。三份研究算术重算与原结果相等，仅证明算术可重现，见研究复核文档。

## 本轮可重现的功能场景

| 场景 | 固定工作与 oracle | 已执行证据 |
| --- | --- | --- |
| 核心 owner/lease/预算/arena | 11 组用例，145033 次检查；包括失败原子性、原 provider 配对、增长共存峰值、关闭竞争、迟到 lease、reset/trim 和并发压力 | 宿主 Release 与 ASan；API 34 x86_64 4 KiB 模拟器 |
| C ABI | 纯 C 程序真实申请/使用/释放，关闭 backing 为零 | 宿主、同一 Android 模拟器 |
| C++ 窄适配 | `include/mgmem/memory.hpp`；五个 RAII/fallible 生命周期用例 | 宿主 Release/ASan、同一 Android 模拟器 |
| 双 runtime 来源 | 同时装载两份核心，拒绝交叉 handle | 宿主功能通过；不是最终家族单 DSO 闭包资格 |
| PPM 图像工作区 | 固定 RGB P6 输入，整数 luma 加边缘夹取 3×3 box blur，完整输出与独立 Python oracle 逐字节比较 | 五种尺寸×两模式十个正例；十个非法输入、两个不可写文件目标均按合同失败 |
| DexMeguviz 解析器 | 真实 `minimal.dex`，`dmg_snapshot_open_borrowed`/查询；独立 copy-in 语义 oracle；结果跨 snapshot 保活 | System/Arena 两模式均 PASS，source SHA、方法数和结果 checksum 一致 |
| 安全 owned-region 诊断 | 实际页、固定节点/访问次数、dependent/independent、页内 rotate/fixed、stride/window/seed，checksum oracle | 宿主 48/48；模拟器单例通过。只构成功能/诊断证据 |

安装消费者统一入口：

```text
python tools/verify_consumers.py --image-exe <installed-package-linked-image-exe> --dex-exe <installed-package-linked-dex-exe> --dex-fixture <real-minimal.dex> --out-dir <receipt-dir>
```

图像尺寸为 1×1、1×7、7×1、17×13、320×180。RGB 生成规则与 Python oracle 固定在工具源码。每条命令有 timeout，保存 stdout/stderr、exit code、输入/输出 SHA256。完整本轮收据是 `artifacts/verification-20261030/consumers/receipt.json`，24/24 PASS；`--image-only` 分支为 22/22，明确不计 Dex 资格。运行工具的 wall time 包含启动/捕获/验证，不作为性能样本。

Dex 通过外部真实源码构建，当前没有有效 Git HEAD；Memory 使用安装包导入。外部源码不是新安装的 Dex SDK。`tools/source_lock.py` 锁定实际 include/src/third_party/CMake/cmake 输入，不能只锁公共头。最终收据必须配合源码、安装库与消费者可执行文件 hash，不能把最初的临时锁当最终制品证明。

## 设备与打包边界

Android x86_64、arm64-v8a、armeabi-v7a 均已编译/安装核心、C++ 头及本轮验证程序；仅 API 34 x86_64 模拟器实际运行。三个核心 ELF 的每个 PT_LOAD 均为 `0x4000` 对齐，DT_NEEDED 只有 `libm.so`、`libdl.so`、`libc.so`，C++ runtime 静态链接；这属于 STATIC_ELF_PASS。它不能替代真实 16 KiB loader、arm64/armv7 实机执行、MTE 或最终 APK/家族依赖闭包验证。

ASan 首轮运行因诊断 runtime DLL 未进入 PATH 而 timeout；修正运行环境后四项 CTest 全部通过。首次失败不能被删去或改写成代码 PASS；最终记录应同时保留失败原因、修正和重跑范围。ASan 与 Release 数据不混用作性能比较。

设备日志与 ELF 原始记录位于 `artifacts/verification-20261030/`。设备结果适用范围仅为记录中的模拟器 ABI/API/4 KiB 环境，不能外推到全部 Android、16 KiB、特定 SoC 或 BIL LSPosed 宿主。

## 正式性能采样的前置条件

当前 `INCOMPLETE_FOR_PERFORMANCE`。正式采集前，先固定真实主场景、版本/制品/input/output hash、画质/档位/成功资格、调用次数和初始化；完成独立 pilot 后，再冻结样本量与精度/功效依据、主指标、配对均衡顺序、时间/自有 backing 回归预算、温度/迁核/后台负载的数值排除阈值。

独立 session 或预定义时间块为统计单位；帧、循环中的 load 和同一 session 内重复不是独立样本。诊断轮与低扰动计时轮分开，保留全部慢样本、失败、超时和回退；不挑最快一次。样本不足不报告 p95/p99 或精确收益区间。准备阶段继续保留 warm 含分配、能力计时前分配/初始化、五次推理取第二快的旧统计定义；实验 session 的统计估计另行命名。

记录阶段嵌套/重叠关系后再汇总；多线程 CPU time 单独列出，不能相加作为 elapsed 关键路径。allocation 次数、累计 requested、唯一 backing、live/retained/retiring、RSS/PSS、缺页、实际触页、padding、迁核、温度与同完成量能量分别观测。未知字段为 null/UNAVAILABLE，不是零。没有能量采集方法时不宣称节电。

## TLB 与退出条件

本轮没有 placement/hint 发布 profile。PMU、PFN、物理相容性和硬件聚合均未取得资格，详见 `bench/TLB_QUALIFICATION.json`。安全 harness 通过不能证明 TLB set/way/slot、page walk、PA 连续或 BIL 性能。改变 stride、VA 对齐或 ELF alignment 不改变 OS 基础页。

需要 MM-022 的真实候选决策和相同 workload、设备配置、核心与系统页记录后才考虑 MM-023；当前 `candidate_decision=INCONCLUSIVE`，不伪造 NO_CANDIDATE 已完成研究。无稳定净收益时维持 System/dense，之后的新 domain 回到合格路径；已有对象按原 owner/provider 完成并 collect/drain，不能搬迁稳定地址或超时强制释放。

CPU 功能预览、家族集成、设备机制、性能与正式发布分别验收。当前工程预览不能被记为 MM-019 发布资格通过。
