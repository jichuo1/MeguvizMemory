# NativePressureBackend 真机短诊断源码

独立测试包为 `io.github.jichuo1.meguviz.memory.nativepressureprobe`，组件为 `io.github.jichuo1.meguviz.memory.android.NativePressureCloseInstrumentation`。测试使用已有公开的 NativePressureBackend 和 MemoryPressureAdapter；不修改生产库、系统策略、BIL 或其他应用。

每次独立创建 arena 并取得 retiring probe，直接调用 `onTrimMemory(15)` 提交**合成**压力，并观察预算降为零且现有 backing 仍保活。两个等待同一放行信号的线程分别调用 backend 与 adapter 关闭；只将明确的 `cleanup admission in progress; retry` 作为竞争拒绝，在两线程均返回后重试。随后确认关闭拒绝新事件、probe 跨关闭可读、持 probe 等待超时、释放后最终 controller/domain CLOSED 与 backing 等账目归零。共四轮；测试线程离开主线程。并发放行并不能证明命中指定竞争时序，尤其不能证明线程创建或 detach 的故障分支；这些分支须依靠已有宿主受控故障回归。

通过时仅输出一条 `report_json`，字段：`suite=android-native-pressure-close-v1`、`status=PASS`、`checks=53`、`case_count=4`、`final_backing_bytes=0`、`callback_origin=synthetic`、`race_window_guaranteed=false`、`performance_qualified=false`，然后 `finish(0)`。检查数是源码断言次数，不等同独立场景或故障注入次数。采集器还须核对正常 transport、输出唯一性、已安装包字节、设备前后状态和完整后验，才能将本次运行记为通过。

线程未返回、JNI 结果未知、压力迟迟未被观测到或域可能仍活跃时，诊断只发送 `FAIL` 或 `INCOMPLETE` 的中间状态（包含固定 stage，不包含地址或异常原文），不调用 finish、强制关闭或释放未知资源；外部超时或中间状态均不是 PASS。外部运行器不得自动卸载、清数据、force-stop、重试或更改系统策略；只终止本地 transport 不证明设备端进程退出。失败回执应保留远端状态未知，并由后续授权处理。

宿主测试将真实 core、pressure 和 JNI 编译进单个测试共享库，Android framework 类型使用桩：

```text
cmake -S tests/android_native_pressure_device/host_stub -B build/native-pressure-probe-host
cmake --build build/native-pressure-probe-host --config Release
ctest --test-dir build/native-pressure-probe-host -C Release --output-on-failure
```

宿主测试和 Android SDK 编译不是 ART、实际设备、真实系统低内存、长稳、生产产品或性能验证。实际运行前还须单独冻结候选 AAR、APK、签名和工具，确认设备及其他会话空闲。
