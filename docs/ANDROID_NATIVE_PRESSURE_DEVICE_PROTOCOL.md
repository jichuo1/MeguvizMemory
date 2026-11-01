# NativePressure 关闭与竞争短诊断协议

本协议针对独立包 `io.github.jichuo1.meguviz.memory.nativepressureprobe`，与托管域诊断使用不同包及独立 runner。它在后台 instrumentation 内运行 4 次拥有 arena 的 NativePressureBackend，检查合成压力、并发请求关闭、持有 lease 的关闭等待和最终账目；4 次尝试不能保证命中竞态窗口，报告固定 `race_window_guaranteed=false`。它不构成长稳、真实系统低内存、性能或 BIL 集成资格。

## 准备与冻结

先运行 `python tools/test_android_native_pressure_round.py`；此测试只使用假 ADB。以独立构建器生成 NativePressure 诊断 APK、回执、匹配 AAR 与 SDK core，使用 `python tools/android_native_pressure_round.py freeze --help` 显式指定它们、设备 serial、API、页大小、ADB、aapt2、Java、apksigner 和超时。该步骤不调用 ADB；它核查 APK 包名、唯一 instrumentation、SDK 27/33、ABI、签名和 AAR/SDK 两个 native 库字节，复验独立构建的源文件、37 个 JNI 方法签名、callback 的 R8 mapping/DEX 日志及 shrink witness；这些不是构建来源的签名证明。

冻结计划默认在 `build/native-pressure-device-plans/` 下新建，输出原始 `plan_sha256`。计划含本机路径及设备哈希，属于私有数据，不提交或上传。随后使用 `run --help` 指定同一输入和冻结时输出的哈希，新建 `build/native-pressure-device-receipts/` 内回执目录及 `build/` 中互不重叠的私有日志目录。运行前对计划、源码、构建回执、日志、制品和工具再次核对。不要更改计划后重新计算哈希冒充原冻结内容。

## 设备准入与失败

每次测试前必须确认设备空闲，核对相关会话；进程、活动 instrumentation 和用户清单只是时点观察，不是共享设备锁。runner 使用显式 serial，并在开始、安装前、启动 instrumentation 前和结束后分别检查；API、ABI、页大小、启动身份和当前用户必须匹配。同包的现有 APK 须回读并确认相同签名；其他可见用户有同包时，除本用户已存在完全相同字节、无需更新外禁止安装。安装请求前持久化结果未知状态，超时或回读失败后不自动重试、卸载或清理数据。

运行器仅启动本包 instrumentation，不做前台重启或改变系统冻结策略。采样只针对本包的 PID、UID、启动身份与 cgroup；冻结、超时或进程身份变化标记 INCOMPLETE。`NOT_OBSERVED` 且无样本不是未冻结证明；有样本也只是离散观察。单条 ADB 命令并发读取 stdout/stderr，分别限制 16 MiB 和 256 KiB；instrumentation 输出重定向到本地文件，以 1 MiB 为门槛轮询、超出则中止且有界读取，轮询间文件可能超过该门槛，**不构成磁盘硬上界**。超限或超时时仅终止本机 ADB transport，设备进程可能仍在运行、结果保持未知；不 force-stop、不清数据、不终止其他会话。中间 `sendStatus` 可能表示资源仍活跃，不是完整终态；不得将它当作 PASS 或完成的 FAIL。

## 报告与后验

PASS 要求唯一终态 JSON 和 `INSTRUMENTATION_CODE: 0`，transport 成功且 stderr 为空；suite 为 `android-native-pressure-close-v1`，`checks=53`、`case_count=4`、`final_backing_bytes=0`、`callback_origin=synthetic`、`race_window_guaranteed=false`、`performance_qualified=false`。终态 FAIL 与 INCOMPLETE 必须使用固定 stage 和 `INSTRUMENTATION_CODE: 1`；截断、额外报告、重复字段和非终态一律不能通过。报告 PASS 后还须确认结束时无其他测试及本包 instrumentation、启动身份和当前用户未变化、已安装 APK 字节与冻结值一致；后验失败保留报告但整轮为 INCOMPLETE。

私有回执和原始进程清单、日志、拉回 APK 保留在 `build/`，包括设备相关身份哈希亦不得直接公开；公开记录须单独脱敏，仅包含必要范围、计数及制品摘要。此前诊断结果不自动升级为本轮结果。
