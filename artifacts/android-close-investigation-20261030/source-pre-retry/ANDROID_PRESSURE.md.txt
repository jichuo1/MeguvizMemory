# Android 内存压力与观测适配

本模块为独立适配器，没有修改 BIL、Lumen、Bitmap/HWUI 或 Compute/Media 的 owner。core ABI 1.1 的软预算入口、独立 native pressure controller、真实 `ComponentCallbacks2` 注册和 JNI composition 分别交付；不接管全局 allocator。

## 控制器合同

`mgmem_pressure_create_v1` 附着已有域时，调用方提供 domain-owner 的 thread-safe/no-throw retain/release；这不同于 provider 的 context retain。初始 normal budget 必须与域当前预算相等，附着失败不隐式修改预算。此 adapter runtime 同一域只允许一个活动控制器。

事件只调用软 `mgmem_domain_set_budget_v1` 并唤醒 cleanup worker，不运行 provider free、trim、collect 或等待 drain。normal/moderate/critical 目标由调用者明确指定，满足 `0 ≤ critical ≤ moderate ≤ normal`。预算可降到0或低于现有backing加reservation，既有owner/lease及已准入reservation继续有效，新准入按新上限拒绝。over-budget只作为观测，不授权强制释放。

cleanup worker 调用 quiet-only trim。活跃owner、retiring lease、在途操作存在时，BUSY保持待办并按显式间隔重查，直到资源安静或控制器关闭；不撤销任何已受理访问。restore必须显式通知NORMAL，不从“没有压力回调”推断内存已恢复。

关闭分三步：begin_close拒绝新事件并通知worker，不等待；drain有界等待worker，0为poll；destroy确认worker退出并join，随后在destroy调用者线程归还domain-owner引用。destroy可能触发原owner的同步最终清理，必须安排到cleanup线程。控制器本身不关闭或销毁外部域，裸C destroy仍由调用者与其他API调用串行化。

## Android 事件

`MemoryPressureAdapter` 使用 application context 注册和注销真实 `ComponentCallbacks2`。接口调用不在Java内部state锁下运行，支持回调中close、在途回调与close竞争。成功注销和cleanup admission分别记账，异常后可以显式重试，已成功步骤不重复执行；close之后始终拒绝新事件。

策略对5–14映射MODERATE、15–19映射CRITICAL、20–39忽略、40–79映射MODERATE、80及以上映射CRITICAL。UI_HIDDEN=20是界面生命周期信号，默认不把它当RAM压力，更不释放别的组件的图像/GPU对象。background级别也是可回收缓存提示，不等于已证明整机低RAM。

Android官方说明，API34起不再通知多数旧RUNNING/MODERATE/COMPLETE级别，onLowMemory也不再调用；API35将相应入口标为deprecated。因此该桥提供兼容处理、background事件和显式request/restore，**不能保证新Android一定递送低内存事件**。来源：[ComponentCallbacks2](https://developer.android.com/reference/android/content/ComponentCallbacks2)。

## JNI owner 与关闭

`NativePressureBackend.createArena` 拥有自己的域；`fromNativeRegistration` 包装 `mgmem_android_pressure_register_v1` 创建的原生注册ID。ID单调不复用，不是指针，不可强转、持久化或跨运行库使用。外部注册失败或Java绑定失败时，原生调用者仍持有已知ID，可重试绑定或调用unregister；JNI不在失败路径同步等待generic owner。

自有域的Java关闭由后台reaper完成：先停pressure worker并join/destroy控制器，再begin_close域；独立Probe强持Holder，在最后borrow_release完整返回后才减少active_probes。reaper等待该计数归零后再collect/drain/destroy域，避免只凭quiet就在别的C API返回栈上销毁域。已有Probe可在parent close后继续读取及归还，归还不依赖parent注册表；没有裸地址或可逃逸ByteBuffer。

自有域awaitClosed成功表示域与控制器都已结束，backing/reservation/live/retiring为零。外部注册的关闭只表示控制器已分离并归还原owner引用；它不主动关闭foreign domain，不能据此声称外部域已CLOSED。正timeout只允许worker调用，主线程只能poll(0)。超时不强制free；callback或provider违反合同产生错误时，不伪造成功关闭。

close需要的后台线程若未能准入，保留注册和owner并显式返回错误，可重试。VM线程附着/完成通知失败时保留可查询的已完成注册，后续Java await/snapshot使用现成JNIEnv补发通知，不把引用丢掉。该JNI模块按进程内稳定装载使用；动态classloader/DSO卸载不在本批资格内。

JNI构建只链接安装后的shared `libmgmem.so`，pressure helper为PIC静态组件并入JNI；拒绝把静态Memory SDK嵌入为第二个runtime。AAR包含窄JNI类keep规则，尚不构成BIL/R8产品资格。

Java logical snapshot数组索引依次为：controller state、domain state、budget、backing、reservation、live、retiring、retained、over-budget、events、trim attempts、trim successes、BUSY deferrals、last status、owned-domain scope(1/0)、cleanup completed。外部close后的domain值为归还owner前最后缓存，不能作为其后实时状态。null表示BUSY/不可得，超出Java signed long的unsigned计数表示-1并通过logicalUnavailableReason说明，不记为0。

## 驻留观测

`ProcessMemoryObservation.capture` 只读取自身 `/proc/self/status` 的VmRSS，以及 `Debug.getMemoryInfo` 的总PSS/private-dirty，单位为Android kB，时间来自elapsedRealtimeNanos。它们包括该进程其他组件，不归因给Memory，也不与domain backing/live/retiring相加。读取错误、权限/格式不可用或不可信零总PSS用-1加原因；诊断JSON导出为null。能源没有可靠同完成量方法，固定为unavailable，绝不记0。API口径来源：[Debug.MemoryInfo](https://developer.android.com/reference/android/os/Debug.MemoryInfo)。

## 构建与验证范围

核心工程导出 `MeguvizMemory::pressure`；独立JNI工程为 `adapters/android`。Android打包脚本 `tools/build_pressure_android.py` 只使用现有SDK/JDK、已安装shared Memory和已构建JNI，无Gradle下载或第三方生产依赖。测试签名key只留在ignored build中，复用本批诊断key，不提交也不打包key本体。

宿主 `tools/verify_android_pressure.py` 使用framework stubs验证分类、注销、重入/并发close、失败重试、main等待拒绝等，当前38项通过；可另外用实际android.jar编译。这是stub逻辑/编译资格，不是假冒Android实测。`tests/pressure_tests.cpp` 验证native BUSY延后回收、软预算、owner引用直到destroy、阻塞provider时poll/timeout不强制free。

独立诊断APK包为 `io.github.jichuo1.meguviz.memory.pressureprobe`。Instrumentation分别记录直接Application分发的合成事件和UiAutomation对自身进程执行 `am send-trim-memory` 的AMS注入递送。AMS拒绝或未递送单独BLOCKED，不合并为通过；两者都不是实际整机RAM耗尽。parent负责精确设备准入、APK身份/签名核对及实际运行回执；本适配器作者没有调用ADB。
