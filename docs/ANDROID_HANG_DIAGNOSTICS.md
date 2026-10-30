# Android 自有进程停滞诊断（revision 4）

此版本只修改独立 `PressureR8Instrumentation` 诊断应用，不改变生产 JNI、压力适配器或公共 ABI。它用于区分已观察到的 `cycle.idempotent-close` 边界；此文件本身不代表在设备上复现或修复了旧挂起。

原阶段标记同时覆盖 `MemoryPressureAdapter.close()` 与其后的 `awaitClosed(0)`。现在分别记录 `cycle.idempotent-close.enter`、`.returned`、`cycle.idempotent-poll.enter`、`.returned`。阶段名和起始时间在请求进度锁或写文件之前先发布为 volatile 值；它们在 JVM 内存中的值与磁盘上最近一次完整记录不一定一致。

## 可选停滞监视

- `stall_timeout_ms` 默认 0（关闭）；开启时范围 1000–60000 ms。同一阶段未再进入新阶段达到该阈值时，本进程进入 `STALL_PHASE_TIMEOUT`。正常扫描建议使用大于任何合法阶段等待上限的阈值，例如 10000 ms；它不替代完整运行的 wall 上限。
- `capture_stack_on_halt=true` 请求 `Thread.getAllStackTraces()`；线程按 main、Instrumentation、诊断线程、常见已附着线程和其他线程的顺序截断到前 20 个，每线程最多 16 帧、每帧 240 字符。只观察本应用 Java 线程，不包含未附着的 native worker，也不提供 native mutex 所有者信息。
- 故障记录使用独立 `cache/mgmem-r8-stall.json`，先写 `STACK_CAPTURE_REQUESTED`，采集成功再写 `CAPTURED_JAVA_THREADS_ONLY` 和线程数组；显式失败记 `UNAVAILABLE_类型`。独立报告线程最多被等待 2 秒，监视者不等待进度锁或同步文件 I/O 结束。记录还可能完全缺失：如报告线程无法启动、文件系统卡住、ART/JVM 全局停顿或进程先被杀。外部控制器必须保持自身有界超时，并可读取上一次完整的 `cache/mgmem-r8-progress.json`。
- 所有 unknown 停止均保留 `unsafe_cleanup=true`、`native_resource_state=UNKNOWN_NOT_FORCED_FREE` 并仅隔离本诊断进程；不是正常域归零。`mgmem-r8-result.json` 与进度文件只有在 `progressLock.tryLock()` 成功且写入完成时才会更新，不能因旧文件存在就当作本轮完成。以 `run_id` 关联当前记录；匹配不了、文件损坏或未生成均记未知，不能沿用旧轮次。

## 保护注入与真实复现

`inject_progress_lock_timeout=true` 要求 `stall_timeout_ms` 非零；仅首轮使用独立线程持有进度锁并等待，主测试线程尝试进入 `diagnostic.inject-progress-lock.blocked`。预期监视者不依赖该锁，记录 `STALL_PHASE_TIMEOUT`、`UNKNOWN_RUNNING_WORK`，随后只终止自身进程；宿主在有限时间内读取 stall 文件和线程栈，并验证没有 normal `CLOSED_ZERO` 报告。持锁注入属于独立预期失败，不能当成正常生命周期通过。

正常复现关闭注入，使用与生产相同的 AAR/JNI、冻结设备/制品/时限，并先验证设备空闲。`capture_stack_on_halt` 和监视期限需写入新计划：采栈与文件写入本身有扰动，不能与旧性能样本混合。栈捕获可以定位当时 Java 状态，单次栈或一个阶段标签不能自动证明根因；应结合实际调用边界、native 状态与反例或修复对照。

## 可选前台对照与自动关闭

同一个诊断 APK 支持 `foreground=false`（默认）与 `foreground=true`。后者使用显式 intent 在非主线程启动此 APK 的 `PressureR8ForegroundActivity`，等待 `onResume` 后才开始工作。该 Activity 显示简短测试提示；仅在自身窗口使用 API 27 的 `setShowWhenLocked`、`setTurnScreenOn` 和 `FLAG_KEEP_SCREEN_ON`，不更改系统设置、不解锁设备。Activity 在测试准入后若暂停，测试记录失败，不能把它当持续前台；最终通过报告分别写出 `foreground_requested`、`foreground_resumed`、检查次数、准入前 pause 基数和运行中的 pause 增量。结束时在自身主线程调用 Activity 的 `finish()`，并有界等待 `onStop` 或 `onDestroy`；两者均未观察到则记录 UNKNOWN 并隔离自身进程，不能写 `foreground_finished=true` 或成功报告。这也可能在 Activity 停止后被系统冻结，必须由外部观察者保留不完整记录。Activity 的恢复状态不代替独立的进程/cgroup 冻结观测，也不保证各厂商策略相同。[Instrumentation 启动合同](https://developer.android.com/reference/android/app/Instrumentation#startActivitySync(android.content.Intent))；[Activity 锁屏与亮屏合同](https://developer.android.com/reference/android/app/Activity#setShowWhenLocked(boolean))。

为验证启动顺序，可先由授权的 adb shell 以显式 component 启动该 APK Activity，在确认同一进程已 resumed 且 cgroup 未冻结后，以设备支持的 `am instrument --no-restart` 和 `-e foreground true -e foreground_prestarted true` 接入。此模式只接受当前进程中已创建、仍处于 resumed 状态的同一个 Activity；未找到或未恢复立即记录 UNKNOWN、隔离本诊断进程，不启动第二个 Activity。报告包含 `foreground_prestarted` 和自身 `process_pid`；宿主须独立核对启动前后 PID、冻结状态及 Activity 生命周期。`foreground_prestarted=true` 单独使用会拒绝。若设备、ClassLoader 或厂商行为不能提供上述合同，结果为未完成，不能升级为前台运行通过。

预启动入口只存在于诊断 APK：Activity 显式导出并要求调用方已获 `android.permission.DUMP`，没有 intent-filter 或其他应用组件入口。AOSP 将该权限定义为 `signature|privileged|development`；普通未获此权限的第三方不能启动，获得相应系统/特权/开发授权的调用方仍可启动，不能称作“只有 shell”。实际 shell 权限和拒绝边界由设备侧分别验证，不修改系统权限授予或全局策略。[AOSP 权限定义](https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/core/res/AndroidManifest.xml)。

每次运行另做一次独立 fire-and-forget 检查：创建空自有域并调用 `close()`，调用者仅通过反射获取注册 ID 并有界查询既有 `nativeExists`，不调用 `awaitClosed`、`snapshot` 或 `nativePollCompletion`。ID 消失才记录 `PASS_REGISTRATION_REMOVED_WITHOUT_CALLER_POLL`；底层内部完成线程可能自行调用 JNI，测试不禁止它。该检查证明受测注册自动移除的有限行为，并未独立读取最终域账目。5 秒内无法确认时记录 UNKNOWN 并只隔离自身诊断进程。它不属于 Attach 失败注入，相关可恢复性由独立 fake-VM 回归证明。

前台 Activity 和上述检查仅用于诊断 APK；builder 明确保留 manifest 的 instrumentation/Activity 入口、保持 AAR JNI keep 原规则，并把两个 Java 源文件和 manifest 写入构建身份。此前未修改生产 Java/JNI 来制造前台状态。后台和前台比较必须使用相同 APK、相同工作量和独立回执；本说明不把系统冻结现象或此次 Activity 启动升级成 Memory 性能结论。

旧 revision 3 精确源字节保存在 `artifacts/hang-diagnostic-20261030/source-baseline/PressureR8Instrumentation-v3.java.txt`，SHA256 `3f80abfeec7ea366e6ef4077f5c5f77e388d5cc99e8788ec784127eb7457f88f`。revision 4 的设备验证、APK、修复与稳定性结果由执行方另行记录；Android 标准平台 jar 编译通过不等于运行通过。
