# 独立托管域和压力诊断

本目录只提供测试源码，不进入生产 AAR。`ManagedInstrumentation` 使用同一 `ManagedMemoryDomain` 进行 System 和 Arena 的受控字节租约、手动预算与合成压力预算仲裁、预算拒绝、owner 先释放、域异步关闭、压力 worker 完成后跨关闭读写、租约释放及最终域账目归零检查；另以 `MemoryPressureAdapter` 注册应用回调，手工触发回调并检查注销、重复及并发关闭。诊断在 instrumentation worker 运行，不通过强制 GC 或消耗设备内存制造压力。

压力提交得到正票据只说明入队；诊断等待相应结果为 APPLIED 并核对有效预算。合成 Android 回调自身没有票据，因此仅记录接纳，再另行提交并确认独立事件。BUSY 仅在票据尚未受理时限时重试。测试会等待压力控制器关闭后才释放跨关闭租约，域的最终快照必须显示 backing、reserved、live、retiring、allocation 和 lease 均为零。Arena 的已分配 chunk 在持有租约时可能保留；不以请求 trim 代替最终零账目。

设备组件为 `io.github.jichuo1.meguviz.memory.managedprobe/io.github.jichuo1.meguviz.memory.android.ManagedInstrumentation`。使用前需按现有合同核对构建输入、实际 APK 签名和目标设备空闲状态；不得为解决签名冲突清除已有应用数据。该源码无专用前台 Activity，也不提供预启动进程的 PID/resumed 持续确认和长稳运行，因此**不能当作长稳证据**。手工调用回调不证明真实系统低内存或压力信号，未声明设备、R8 运行时或性能资格。

`host_stub/CMakeLists.txt` 是独立宿主入口：用现有 Android 最小桩、真实宿主 JVM 和单个测试 DLL 运行同一诊断；`FaultProbe` 用受控宿主桩覆盖注册失败、注销及清理接纳失败后的可重试关闭、事件拒绝、尚未确认完成以及并发关闭。该测试 DLL 将 core 和 JNI 合并，只用于宿主回归，不代表 Android AAR 的动态依赖闭包。建议在独立 `build/` 子目录中配置和运行 CTest；它不操作设备，宿主结果不证明 ART 或系统回调运行。测试失败时以固定状态和阶段输出，不输出原始异常、用户字节或地址；不对未知状态强制回收。

`ManagedShrinkWitness.java` 是故意不被引用的测试输入；R8 构建时须将它加入输入，并检查它从产物中消失，才可作为发生收缩的 build-only 证据。该见证类不检查 JNI 名称或设备运行。

instrumentation 的 `report_json` 成功字段固定为 `suite`（`android-managed-pressure-v1`）、`status`（`PASS`）、`checks`（整数）、`case_count`（3）、`final_backing_bytes`（0）、`callback_origin`（`synthetic`）、`queued_implies_applied`（false）、`performance_qualified`（false）。失败字段固定为 `suite`、`status`（`FAIL`）、`checks`、`stage`；`stage` 仅使用源码中的固定标识。成功检查次数至少为 302：每次查询未完成的压力票据和合成回调遇到 BUSY 的重试也计一次检查，所以它不是固定值。回调仅在可观察到 BUSY 且未被受理时有界重试，并核对拒绝和转发计数。宿主故障入口另输出 `suite=android-managed-fault-host` 和 `status=PASS/FAIL`，与设备诊断报告分开。

示例宿主命令（生成器和工具链按本机环境选择）：

```powershell
cmake -S tests/android_managed/host_stub -B build/android-managed-host
cmake --build build/android-managed-host --config Release
ctest --test-dir build/android-managed-host -C Release --output-on-failure
```
