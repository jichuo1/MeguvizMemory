# 独立 Android NativeMemoryLease 诊断

这是后续设备验证使用的**独立测试 APK**，不进入生产 AAR。测试用 JNI fixture 同时动态链接现有 `libmgmem.so` 和 `libmgmem_pressure_jni.so`，不嵌入第二份 Memory core。Java 测试类实际调用输入 AAR 提供的 `NativeMemoryLease`。

fixture 建立自己的 SYSTEM 域和已初始化 allocation，检查缺少初始化声明会被拒绝；随后分别以 READ、WRITE 借用执行单次认领、初值和范围、owner 释放及域 CLOSING 后继续访问、持 lease 时 collect 返回 BUSY、重复 close、owner pin 归还以及 domain 最终零计数和 destroy。它不模拟实际低内存，不操作 BIL，也不证明性能、R8 运行时、持续稳定或其他设备资格。

`tools/build_android_borrow_probe.py --help` 描述构建参数。需要已有 JDK、Android SDK/NDK、CMake/Ninja、装有新 borrow 头的 Android shared Memory SDK 和对应 ABI 的 pressure AAR。构建器要求 AAR 中 core 与 SDK 的 core 逐字节相同，验证目标 ELF 架构、动态依赖与所需导出，才生成自签名测试 APK 和构建回执。它只写全新的 ignored `build/` 子目录，不调用 ADB；诊断密钥和原始日志留在该目录，不应公开分发。回执 `device_executed=false` 仅证明构建和静态闭包检查。

可选 `--r8` 使用输入 AAR 内原封不动的 `proguard.txt`，另加的诊断规则只保留 manifest 所需的 instrumentation 类，不额外保留 `NativeMemoryLease` 或其 native 方法。构建器检查测试输入中无引用的 witness 确实被移除，并用 DEX 实际定义核对 lease 类及 JNI 方法的名称和签名；另外以相同输入、去掉 AAR 规则执行独立负对照，记录 lease 名称是否改变。`PASS_R8_BUILD_ONLY_NOT_DEVICE_EXECUTION` 只表示这些构建检查通过，不能证明 ART 上的 JNI 调用或业务应用 R8 集成通过。

未来设备执行前，按项目协议先核对目标序列、无其他会话占用、APK 包名与签名；同名应用存在时先核对签名和数据归属，不能通过卸载或清数据解决签名冲突。用户已说明某测试手机安装了冻结模块，设备执行时应将这一环境因素单独记录。以明确的设备选择安装测试 APK 后，仪器组件为：

```text
io.github.jichuo1.meguviz.memory.borrowprobe/io.github.jichuo1.meguviz.memory.android.NativeBorrowInstrumentation
```

验证须读取仪器的 `report_json`、确认最终 `status=PASS`、两个 READ/WRITE case 和最终域 backing 为零，并分别记录实际 APK、依赖库、设备及测试终态。构建成功或仪器进程退出本身都不能替代这些证据；本目录不预设设备已经运行。
