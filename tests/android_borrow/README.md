# 独立 Android NativeMemoryLease 诊断

这是后续设备验证使用的**独立测试 APK**，不进入生产 AAR。测试用 JNI fixture 同时动态链接现有 `libmgmem.so` 和 `libmgmem_pressure_jni.so`，不嵌入第二份 Memory core。Java 测试类实际调用输入 AAR 提供的 `NativeMemoryLease`。

v2 fixture 建立自己的 SYSTEM 域和已初始化 allocation，检查缺少初始化声明会被拒绝；随后分别以 READ、WRITE 借用执行单次认领、初值和范围、owner 释放及域 CLOSING 后继续访问、持 lease 时 collect 返回 BUSY、重复 close、owner pin 归还以及正常 domain 最终零计数和 destroy。受限回调门禁另用两个 Java 线程观察 `OPEN → RELEASING → RELEASED` 和重复 close 可提前返回；门禁本身有超时，无法观察到预期状态必须失败。

测试还使用 fixture 专用 JNI pending Java 异常检查 `FAILED` / native 状态未知，以及 checked external release 返回错误码检查 `FAILED` / 9、注册不重试和 domain/owner 保守隔离。前者是有意注入 Java 异常的诊断场景，不表示正常 no-throw 回调合同允许抛异常；它没有让 C++ 异常跨越 ABI。后者故意保留 32 字节及 2 个 owner pin 至测试进程结束，不能与正常 domain 的 backing 归零混记。它不模拟实际低内存，不操作 BIL，也不证明性能、R8 运行时、持续稳定或其他设备资格。

`tools/build_android_borrow_probe.py --help` 描述构建参数。需要已有 JDK、Android SDK/NDK、CMake/Ninja、装有新 borrow 头的 Android shared Memory SDK 和对应 ABI 的 pressure AAR。构建器要求 AAR 中 core 与 SDK 的 core 逐字节相同，验证目标 ELF 架构、动态依赖与所需导出，才生成自签名测试 APK 和构建回执。它只写全新的 ignored `build/` 子目录，不调用 ADB；诊断密钥和原始日志留在该目录，不应公开分发。回执 `device_executed=false` 仅证明构建和静态闭包检查。

可选 `--r8` 使用输入 AAR 内原封不动的 `proguard.txt`，另加的诊断规则只保留 manifest 所需的 instrumentation 类，不额外保留 `NativeMemoryLease` 或其 native 方法。构建器检查测试输入中无引用的 witness 确实被移除，并用 DEX 实际定义核对 lease 类及 JNI 方法的名称和签名；另外以相同输入、去掉 AAR 规则执行独立负对照，记录 lease 名称是否改变。`PASS_R8_BUILD_ONLY_NOT_DEVICE_EXECUTION` 只表示这些构建检查通过，不能证明 ART 上的 JNI 调用或业务应用 R8 集成通过。

`tests/android_borrow/host_stub` 是可独立重建的宿主回归入口：使用真实宿主 JVM、fixture、core 和 JNI，并以最小测试桩代替 Android 类；测试库将 core 与 adapter 编入同一个 test DLL，另一个同名占位库只供 Java 加载，不代表安装版或 Android 的运行时依赖闭包。构建后运行 CTest，它调用当前 `android_borrow_round.py` 的严格 v2 解析器核对 62 项和隔离账目；失败 fixture 故意保留资源至测试进程结束，不能写成正常归零。该入口不调用 ADB，不证明 ART、设备或性能。

未来设备执行前，按项目协议先核对目标序列、无其他会话占用、APK 包名与签名；同名应用存在时先核对签名和数据归属，不能通过卸载或清数据解决签名冲突。用户已说明某测试手机安装了冻结模块，设备执行时应将这一环境因素单独记录。以明确的设备选择安装测试 APK 后，仪器组件为：

```text
io.github.jichuo1.meguviz.memory.borrowprobe/io.github.jichuo1.meguviz.memory.android.NativeBorrowInstrumentation
```

验证须读取 v2 仪器的 `report_json`、确认最终 `status=PASS`、62 项检查、两个 READ/WRITE case、关闭状态以及正常域 backing 为零；失败 fixture 的保留资源单独报告。旧 v1 的 29 项回执属于旧源码，不能转授新版。须分别记录实际 APK、依赖库、设备及测试终态。构建成功或仪器进程退出本身都不能替代这些证据；本目录不预设设备已经运行。
