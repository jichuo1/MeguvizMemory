# 独立托管域长稳诊断源码

该目录不进入生产 AAR，也不更改现有测试或 runner。Android 构建须显式包含这里的 manifest、`ManagedStabilityInstrumentation.java`、`StabilityEngine.java`、`StabilityTiming.java`，以及 `adapters/android/sample/PressureR8ForegroundActivity.java` 和所选候选 AAR。Activity 只维持本进程的前台窗口，不调整系统冻结、锁屏或调度策略；真实运行前仍需单独确认设备空闲和 APK 签名。arm64 与 x86_64 的 R8 诊断 APK 已在本机构建并完成静态身份检查；仓库不包含测试 APK 或其私有签名材料，也没有声明设备、ART 或长稳资格。运行合同见[独立长稳协议](../../docs/ANDROID_MANAGED_STABILITY_PROTOCOL.md)。

设备执行方案：由有 `android.permission.DUMP` 权限的可信 shell 显式启动 `io.github.jichuo1.meguviz.memory.stabilityprobe/io.github.jichuo1.meguviz.memory.android.PressureR8ForegroundActivity`，确认它已 resumed 并取当时进程 PID；随后用 `am instrument --no-restart` 向 `io.github.jichuo1.meguviz.memory.stabilityprobe/io.github.jichuo1.meguviz.memory.android.ManagedStabilityInstrumentation` 传入 `expected_pid`、唯一小写 16 位十六进制 `run_id`。diagnostic Activity 与现有 R8 诊断相同，声明 exported 并要求 DUMP，实际目标设备上 shell 能否启动仍须验证。诊断核对当前 PID、预启动 Activity 的同进程实例、resumed 状态和开始后的 pause 次数；无法确认则 INCOMPLETE。外部执行器还须观察自身的 PID 和 Activity 证据；这些采样不能证明两次采样之间没有变化。

正常结束且不存在未知状态或可能存活的域时，诊断在主线程向已确认的自身 Activity 定向调用 `finish()`，最多等待 3 秒确认调用返回；失败、超时或身份变化时报告 INCOMPLETE。等待超时会撤销尚未执行的排队请求；已开始执行的 `finish()` 不能中断，返回结果未知。调用返回仅证明已请求结束，不证明 Activity 已停止或进程已退出，外部仍需独立核对前台和进程；不得发送全局返回键或强停。未知状态或可能有存活域时不请求结束 Activity，也不强制释放资源。异常且已知没有活动域时，诊断会尽力尝试同样的定向请求，但不以此推断资源或界面已清理。

可选参数：`duration_ms` 默认 900000，上限 3600000；`max_rounds` 默认 10000，上限 100000；`interval_ms` 默认 250，上限 60000；`phase_timeout_ms` 默认 30000、范围 1000–60000，且须大于 interval。每轮各执行一次 System/Arena 的真实托管域，逐一确认合成压力事件已应用、手动零预算和拒绝、owner 释放后跨关闭租约、关闭后最终零账目。计划时长从前台身份确认并完成看门线程就绪握手之后开始计算，准备阶段不计入；准备已超时或就绪握手超时单列 `preparation_timeout`。少于 900 秒、达到轮数上限却尚未到计划时长、或身份不成立，均不能输出 PASS。

进度存入目标应用 cache 的 `managed-stability-<run_id>-progress.json`，采用 AtomicFile，内容包含固定 suite、状态、run_id、PID、单调 sequence、阶段、轮数、总耗时、工作负载耗时和前台确认。各字段独立采样，报告标记 `snapshot_consistency=non_atomic`，不可视为同一时刻的原子快照。每个阶段的进度在耗时调用之前发布；外部可只读观察该文件。独立 daemon 看门线程在阶段超过上限后写单独的 `managed-stability-<run_id>-stall.json`，记录 INCOMPLETE / `phase_timeout_unknown`；同一 run ID 若有 stall，外部必须以 stall 为准，即便 progress 较晚显示其他状态。看门线程不 join、结束进程或释放可能仍被占用的资源。每个受控内存操作之前和返回后都会检查 unknown，但无法中断已在途调用，检查与实际调用之间也不是原子操作。若一次域操作状态未知，诊断不调用 `finish`、不主动释放域，保留外部观察；此时不能只等待 instrumentation 的正常退出。准备初期或整个进程被冻结时，看门线程可能尚未就绪或无法运行；外部必须将无终态视为未完成，不能将无 stall 文件视为通过。日志不写 native 地址、测试字节或原始异常。

宿主检查使用单 DLL 链接真实 core/JNI 和有限 Android 桩，不证明 Android 动态依赖闭包：

```powershell
cmake -S tests/android_managed_stability/host_stub -B build/android-managed-stability-host
cmake --build build/android-managed-stability-host --config Release
ctest --test-dir build/android-managed-stability-host -C Release --output-on-failure
```

八项宿主入口覆盖 System/Arena 正常两轮、持租约阶段未知状态保留、owner/lease 在途调用返回时模拟超时、前台身份拒绝和准备超时、可控时钟下看门线程未获调度时阶段到期不能被刷新掩盖、源码 manifest 权限合同，以及自身 Activity 定向结束、身份变化、未知状态和超时撤销。工作线程在同一状态锁内使用同一次单调时钟采样比较旧阶段期限并更新时间，率先发现过期的线程统一写 stall；这仍无法中断在途调用，检查与调用之间也不具备原子性。合成压力不等于系统低内存；长稳、性能、真实系统压力和 R8 行为均须按实际证据另行验证。
