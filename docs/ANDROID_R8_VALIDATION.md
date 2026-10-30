# Android R8消费与有界稳定性验证

本轮新增独立消费APK，不修改BIL、原有`PressureInstrumentation`或生产owner合同。输入为已交付的0.2.0压力AAR，直接使用其中的`classes.jar`、`proguard.txt`和shared core/JNI。修正见证后的arm64构建收据为`artifacts/continuation-20261030/android-build/r8-arm64-build-v2.json`；设备执行结果由主代理另行记录，**构建PASS不代表设备PASS**。

本消费构建直接调用已有SDK/R8工具，实际使用AAR的Java与keep/native闭包；没有运行Gradle/AGP产品构建或BIL依赖图，不能据此宣布这些产品组合通过。

## 构建与真实R8证据

`tools/build_pressure_r8.py`使用已有SDK/JDK和SDK内R8 9.2.4-dev，不增加生产依赖、不下载、不调用ADB。编译平台android-37.0，min API27、target API35，独立包`io.github.jichuo1.meguviz.memory.pressureprobe.r8`。沿用ignored build内诊断签名；不打包或提交key。

脚本启用R8 release默认的缩减、优化与混淆。额外规则仅保留manifest instrumentation入口，以及可重命名的见证类；JNI类和`onNativeClosed(int)`只由AAR原有规则保留。mapping确认普通见证类重命名，usage和最终DEX确认未使用类移除；不是仅用D8生成DEX，也没有对整个适配器包添加keep。AAR既有规则较保守地保留NativePressureBackend全部成员，本轮不改动其生产规则。

arm64构建验证：见证类`R8ConsumerMarker`变为`io.github.jichuo1.meguviz.memory.android.a`；`R8UnusedMarker`被移除；native类名及完成回调名保留。APK内两份DSO逐字节等于输入AAR，zipalign 16KB packaging检查和签名校验通过。这里的16KB包装检查不是实际16KB loader运行资格。

最初设备smoke在第2个检查处拒绝，因为instrumentation用原类名字符串与运行时类名比较，而R8可一起改写该字符串。这是测试见证逻辑错误，不能把该次失败称为JNI成功或库失败。v2仅要求类名非空并报告，由外部runner按该APK构建收据的`r8_evidence.marker_after`精确核对；旧APK、旧构建和失败输出保留，v2单独构建、重新执行，不合并为同一次结果。

```text
python tools/build_pressure_r8.py --sdk <existing-sdk> --java-home <existing-jdk17>
  --out-dir build/continuation-r8-arm64-v2
  --receipt artifacts/continuation-20261030/android-build/r8-arm64-build-v2.json
```

输出目录必须是build下的新目录。可选`--abi x86_64 --memory-sdk install/continuation-x86_64 --native-dir build/continuation-pressure-jni-x86_64`从固定AAR保留Java/classes/keep/notice、替换真实x86_64 shared DSO，先生成独立派生AAR后消费。检查ELF机器类型，收据分别锁定base AAR、派生AAR和override来源；不把原arm64 AAR声称为x86产物。

## 运行合同

组件为`io.github.jichuo1.meguviz.memory.pressureprobe.r8/io.github.jichuo1.meguviz.memory.android.PressureR8Instrumentation`。安装、准入、设备身份与同证书更新由主代理负责。本脚本不启动或占用设备。

支持instrumentation字符串参数：

| 参数 | 默认/范围 | 含义 |
|---|---|---|
| iterations | 100；1–100000 | 最低完成轮数 |
| min_duration_ms | 0；0–1800000 | 不足该时间仍持续执行实际验证轮次 |
| max_duration_ms | 180000；1000–3600000 | 整轮检查期限；各join/await仍各自有界，不强杀线程 |
| sample_every | 100；1–100000 | 自身RSS/PSS观测间隔；中间最多256份 |

15分钟profile可用`iterations=1000,min_duration_ms=900000,max_duration_ms=1200000,sample_every=200`。没有空睡凑时间；短暂sleep只用于native worker BUSY、snapshot或系统递送的有界轮询。若超时或达到100000循环仍未满足时间要求，则FAIL，不能降级为完成。短smoke与15分钟profile必须分开记录。

每轮创建自己的arena和注册callback，以256–1536B变尺寸retiring lease反复验证：MODERATE预算、CRITICAL预算0、后续准入拒绝、worker因活跃lease延后trim、已受理lease仍可读、显式restore；主线程close和poll(0)，主线程正等待必须被拒绝；worker短await超时不得forced free；另一个线程最后读/归还lease，JNI回调结束domain关闭，backing/reservation/live/retiring全零，再次close稳定。

报告包括请求/完成轮数、最短/最长/实际时间、检查数、单域请求backing峰值、各域最终归零、自身RSS/PSS/private-dirty及不可用原因。所测域由本消费应用拥有，不把foreign domain detach当作该域关闭。Thread和JNI/classloader卸载不在该profile中。

## API34+事件边界

官方API合同说明API34起不再发送旧RUNNING*/MODERATE/COMPLETE通知，`onLowMemory`也不再调用。因此本测试依赖显式request/restore，并记录该限制；不会等待这些旧自动事件作为通过条件。真实Application注册后的BACKGROUND/UI_HIDDEN主动分发属于合成事件。另用UiAutomation仅对自身包请求AMS BACKGROUND注入，分别报告`AMS_REJECTED`、`COMMAND_ACCEPTED_NO_CALLBACK_OBSERVED`、`DELIVERED`或命令不可用；它们不相互替代，也不是实际整机低RAM。[ComponentCallbacks2官方合同](https://developer.android.com/reference/android/content/ComponentCallbacks2)

JNI静态名字与native反向查找的Java回调必须保留。R8可缩减和重命名其他可见代码，所以mapping/usage只能证明构建变换，压缩后native注册、最后lease释放与完成通知还须实际运行。[R8 keep规则官方语义](https://developer.android.com/topic/performance/app-optimization/add-keep-rules)

有界15分钟稳定性只覆盖记录设备、ABI和profile，不能证明无限时长无泄漏。自身驻留采样属于全进程，含JIT、类加载和其他组件，既不归因给某domain，也不与logical backing相加。没有强制GC、真实OOM、性能配对、能源或BIL产品资格。

## 诊断revision 3的边界保护

API33第一次Android长稳未返回正常报告，外层1260秒超时；没有取得可定位根因的线程栈，不能把它认定为已证明的生产JNI死锁。该轮已完成的独立DEX/图像结果仍保持各自范围。旧instrumentation原字节在`artifacts/continuation-20261030/frozen-first-stability/PressureR8Instrumentation.java.txt`，与原stability-plan的81a2c410…SHA匹配；bf0e…原见证版和中间构建也分别保留。

revision 3仅修改诊断instrumentation，生产JNI和NativePressureBackend逐字节未变。两个ABI另构建为`r8-arm64-build-v3-guard.json`与`r8-x86_64-build-v3-guard.json`，源码快照及保护范围见`diagnostics/r8-diagnostic-v3-build-scope.json`。它们的构建PASS不代表新设备长稳PASS。

自身cache内的`mgmem-r8-progress.json`与`mgmem-r8-result.json`原子写入小JSON，含run_id、diagnostic_revision、phase、iteration、checks、elapsed与cleanup安全状态。每个可能阻塞的native/主线程/观测边界前记录进度；同一阶段的重复轮询不重复写。主线程派发改为Handler.post和3秒CountDownLatch：PENDING任务可原子取消，保证其body不执行后正常清理并FAIL；已经RUNNING的任务超时为UNKNOWN，跳过全部普通finally清理、保留未知资源状态并只终止自身诊断进程，不与未知访问并发free。

`observe_ams`默认false，长稳完全不调用UiAutomation；AMS独立smoke证据保留。若显式true，则getUiAutomation、executeShellCommand及读取整体由独立worker覆盖3秒join，worker超时同样隔离为UNKNOWN，不提前释放其可能仍在使用的对象。整体wall watchdog也只针对本诊断进程；cache中的UNKNOWN不是正常CLOSED_ZERO，am可能仅报告进程中止，须独立读取cache证据，不能补记成功。

可选`inject_main_running_timeout=true`仅用于诊断保护自测：第一轮主任务进入RUNNING后等待不会由其他任务打开的latch，父3秒超时应记录`MAIN_TASK_RUNNING_TIMEOUT`、phase=`main.injected-running-latch`、unsafe_cleanup=true并隔离退出。该预期失败必须单独记录，普通smoke和新长稳关闭注入。真实运行结果由主代理另存收据；不把本段设计说明当作保护已运行通过。
