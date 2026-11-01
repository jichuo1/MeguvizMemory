# 安装版 Memory + DexMeguviz 元数据 reader 配对诊断

本诊断以锁定的真实 `paired_v2.cpp` 和 DexMeguviz 构建源码为输入，在 `build/` 生成独立原样和插桩副本，分别静态链接 Dex 核心并消费所选安装版 Memory SDK。它不修改共享 DexMeguviz 或普通消费者。对于同一个 DEX，分别运行 `D-C0`（普通复制）、`D-Z0`（原向量借用）、`D-C1`（Memory System）和 `D-C2`（Memory Arena），在同一变体内比较原样与插桩结果；前两项没有 Memory domain，不把它们的最终状态称为 Memory 域归零。普通复制、借用和工作区优化不记为 Memory 净收益。

## 插桩与范围

`prepare_consumer.py` 根据提供的源码锁逐项验证并复制 DexMeguviz 的锁定构建源码，拒绝额外构建文件、链接、源码漂移及补丁数量不符。原始 `checked_leb128.hpp` 不变；仅插桩 reader 使用专用命名空间的诊断副本，并精确重定向三处 helper 调用，避免与其他编译单元的原始 inline 定义发生 ODR 冲突。其他编译单元仍使用原始 helper。`verify_prepared` 重新生成并比较源码、位点和清单；准备进程必须退出成功，不能只因存在清单就认定成功。

`prepare_paired.py` 对固定哈希的 `paired_v2.cpp` 按固定锚点生成单独诊断源码，不修改原文件。插桩 Recorder 只在调用线程进入 `dmg_snapshot_open_borrowed` 时启用，并在该调用返回后退出；冻结 Dex 版本在这次调用内同步执行 `read_metadata`。记录的只是本次窗口中选定的 31 个 reader 字节位点和一个 LEB 位点，按执行顺序输出相对 DEX 偏移，不输出指针或字节值。实际读取超过预先分配的事件容量、span 与选定输入不符或输出失败均不能产生成功回执。调用窗口开始时另计算实际所用输入字节摘要；`corrupt-input` 会将首字节改为零，因此其摘要与原文件分别核验。

本诊断不记录 SHA-256 内部读取、普通复制、SDK 查询、非 reader 的 decoder/debug/CFG/dataflow、allocator 或跨线程释放中的访问；结果的跨线程释放由已有消费者合同检验，不被伪装为事件轨迹。线程局部 Recorder 的事件顺序是插桩源码语义，不是未插桩机器指令、缓存、TLB 或 PMU 访问。该工具不计时、不比较或宣称性能：原消费者 JSON 中的时间字段可能因插桩改变，自动对照显式忽略它们。

## 构建与受控对照

先准备官方候选归档和解包后的完整 SDK（保留候选中的 Linux 相对符号链接）；以下尖括号由调用者替换。SDK 与候选必须属于同一份来源，Memory core 须是其安装目录中的共享库。

```text
python consumers/dex_trace/prepare_consumer.py --dex-source build/<冻结Dex源码> --source-lock SOURCE_LOCK.json --out-dir build/<新Dex准备目录>
python consumers/dex_trace/prepare_paired.py --paired-source consumers/dex/paired_v2.cpp --out-dir build/<新配对源码目录>
cmake -S consumers/dex_trace/paired -B build/<新baseline构建目录> -DCMAKE_PREFIX_PATH=<SDK前缀> -DDEX_TRACE_STAGE=<Dex准备目录绝对路径> -DPAIRED_TRACE_STAGE=<配对源码目录绝对路径> -DDEX_TRACE_MODE=baseline -DCMAKE_BUILD_TYPE=Release
cmake --build build/<新baseline构建目录> --config Release --target mgmem_dex_reader_trace
cmake -S consumers/dex_trace/paired -B build/<新traced构建目录> -DCMAKE_PREFIX_PATH=<SDK前缀> -DDEX_TRACE_STAGE=<Dex准备目录绝对路径> -DPAIRED_TRACE_STAGE=<配对源码目录绝对路径> -DDEX_TRACE_MODE=traced -DCMAKE_BUILD_TYPE=Release
cmake --build build/<新traced构建目录> --config Release --target mgmem_dex_reader_trace
python consumers/dex_trace/paired_compare.py --input <工作区内原始DEX> --baseline <baseline程序> --traced <traced程序> --core <安装SDK共享核心> --candidate <候选ZIP或tar.gz> --candidate-evidence <对应候选摘要> --source-lock SOURCE_LOCK.json --dex-stage <Dex准备目录> --paired-stage <配对源码目录> --sdk-prefix <SDK前缀> --out-dir build/<新对照目录>
```

受控 runner 对每种变体执行 `normal`、`cross-thread`、`invalid-lease`、`borrow-budget`、`corrupt-input`，共 20 对独立进程。它验证固定状态和整数类型、输入身份、方法数与结果 checksum、引用 retain/release、关闭及账目字段；仅无效 lease 和借用预算拒绝允许零事件。空轨迹、截断、超限、进程失败、事件偏移越界、载入核心身份不匹配、输入/二进制/源码漂移均为 INCOMPLETE。它核对候选归档及整个安装 SDK、CMake 缓存、私有源码两套副本，并在运行前后重复确认。ZIP/tar 每个 SDK 成员上限 64 MiB、累计上限 256 MiB；Windows reparse/junction 目录拒绝，Linux 仅接受候选清单中明确记录的相对文件链接。

每个诊断子进程报告其 OS 模块路径，runner 对照所选安装 core 路径及磁盘文件摘要；这是**子进程自报的模块路径和磁盘文件匹配**，不独立证明进程映射字节、可执行文件确由本次源码构建，也不是签名来源证明。用户业务输入、原始 events、路径和子进程输出都只保存在 ignored `build/`，公开时应另做脱敏摘要。

每个子进程有独立超时及 stdout/stderr 实时内存限额；另有默认 300 秒、最高 3600 秒的总 deadline 和私有 `progress.json`，每完成 5 对打印计数。总 deadline 是协作式边界：单次文件/解压系统调用及最终持久化可能跨过截止，不是硬实时保证；超时只终止本工具自己启动的子进程，不声称其后代已退出。只有整个进程退出码为 0 且 `receipt.json` 为 PASS，才是限定对照通过；单独存在中间输出或 progress 不是成功证据。该诊断不执行 Android 设备、不验证真实产品工作负载或 Memory 净性能收益。

## 采集失败的限定分类

插桩 Recorder 保存**首次观察到**的固定类别：`EVENT_LIMIT`（达到事件容量）、`SPAN_MISMATCH`（传入 span 与预定输入不一致）、`OFFSET_OUT_OF_RANGE`（偏移越界）、`SITE_INVALID`（未识别位点）、`SCOPE_INVALID`（可观察的作用域进入/退出错误）或 `OUTPUT_FAILURE`（trace 输出失败）。它只写固定类别、事件上限、已记录数和尝试数下界，不写输入字节、偏移、地址或路径。尝试数最多保留 `event_limit + 1`，`attempted_capped=true` 表示实际尝试数至少为此值，不是精确总数。若先后发生多种异常，类别只反映首个异常；不可据此推断其他异常没有发生。未处于本线程活跃采集窗口的其他线程读取不会被统计，不能据此声称跨线程访问已经覆盖。

仅当 traced 程序以约定的退出码 1 返回，且 stderr 恰好是唯一、字段与计数均严格有效的固定诊断时，配对 runner 才写私有 `trace-incomplete.json` 并输出脱敏 `INCOMPLETE`。它标记 `evidence_scope=CHILD_REPORTED_NOT_INDEPENDENTLY_VERIFIED`：类别是诊断子进程自报，不是对实际执行映像或根因的独立认证。崩溃、其他退出码、构造/分配失败、stderr 缺失或含额外文本、状态不一致、采集超时及自身输出失败均保留通用 INCOMPLETE；特别地，stderr 自身不可写时不保证可获得分类。分类失败永远不生成采集 PASS、绑定 PASS 或回放 PASS。`paired_replay.py` 只在采集器退出码 2 且脱敏 sidecar、末条报告及已完成对数相互一致时传播这些字段，其余情况不回显原始子进程日志。

本次修改了诊断 Recorder 和源码生成器。此前已准备的 paired 源码、已编译程序、冻结计划与私有签名/身份不自动适用于新版；使用前应从锁定源码重新准备、构建并另行冻结输入身份。历史结果保持原样，不能混入新版结论。
