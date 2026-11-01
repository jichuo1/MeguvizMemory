# Android 托管域消费示例

本目录是独立的 Android Java **源码级消费者**：在应用提供的后台工作线程上创建 `ManagedMemoryDomain`、显式绑定同域压力控制器，并通过 `MemoryPressureAdapter` 注册系统回调。示例用受控 lease 写入和读取工作区，最后注销回调、归还 owner 和 lease，等待域关闭并核对最终账目。它不依赖本目录以外的应用代码，也不添加第三方生产依赖。

调用方应持有 `ManagedPressureSample`，把 `openOnWorker()`、`submitSyntheticCriticalOnWorker()` 和 `closeOnWorker()` **串行安排到后台线程**，不要在主线程等待。`openOnWorker()` 中途失败时仍要保留 session 并尝试关闭；`closeOnWorker()` 返回 `false` 表示有界等待尚未确认完成，应保留 session 并安排后续串行重试。关闭抛异常或域隔离时不能当成成功，也不能因为超时强制释放资源。`closeOnWorker()` 返回 `true` 才表示该示例确认自身域完成清理并忘记观察令牌；这不证明其他线程或动态库可以卸载。

`submitSyntheticCriticalOnWorker()` 直接向压力控制器提交**合成事件**，返回的正 ticket 只表示已排队；返回对象分别记录 ticket 的实际状态、native 状态与应用序号。仅对明确未受理的 BUSY 作有界重试，接受后不重复提交；超时或查询不可用返回 `EVENT_UNAVAILABLE`，不能当作应用成功。两个 framework 计数字段是另行采样的累计转发和拒绝数，不能证明发生过真实系统低内存。应用真正收到何种系统回调取决于设备和 Android 版本，必须自行观测，不能由本示例推断。

例如，应用可把下列顺序放在由应用持有的串行后台执行器中；session 和执行器应由应用的生命周期所有者持有，直到关闭确认完成：

```java
ManagedPressureSample session = new ManagedPressureSample(applicationContext);
// 保存 session 后再执行，以便 open 抛错时仍可安排清理。
session.openOnWorker();
ManagedPressureSample.EventObservation event = session.submitSyntheticCriticalOnWorker();
// 根据 event.state/status 处理；合成事件不能作为系统低内存证据。
boolean closed = session.closeOnWorker();
// false 或异常时保留 session、报告原因并按合同处理；不要丢弃或强制释放。
```

上面展示调用顺序；实际调用方应在 `openOnWorker` 或事件处理抛出异常时也安排清理，并处理 `InterruptedException`。示例的字段没有为多个调用线程提供同步；系统回调本身由库处理，不应从回调并发调用这些 session 方法。一次释放若结果未知，库会隔离对应域；应用不能把它视为正常归零。

## 编译检查

准备与当前源码匹配、包含 core/JNI 的 Android AAR、对应的 `android.jar` 和 JDK，使用一个**尚不存在**的输出目录：

```sh
python consumers/android_managed/verify_compile.py \
  --aar "<待检查的 AAR>" --android-jar "<Android SDK 的 android.jar>" \
  --out-dir build/android-managed-sample-check
```

检查器只读取 AAR 中唯一的 `classes.jar`，核对所需 Java 类及每个已包含 ABI 的 core/JNI 文件存在，再编译示例。它记录输入与输出 SHA-256；AAR 内两个文件的存在不证明二进制能加载或互相兼容。输出目录中的 `javac.stderr` 和 `diagnostic.txt` 可能包含本机路径，应留在本地，公开时只使用脱敏收据。`--classes-jar` 可替代 `--aar` 作开发期源码编译检查；这不检查 AAR 或其中的 JNI，收据会明确标为 `DEVELOPMENT_CLASSES_JAR`。

`python consumers/android_managed/tests/test_verify_compile.py` 单独检查验证器的归档结构规则；测试使用内存中的合成条目，不执行其中的 native 字节，也不是 AAR 运行证据。

宿主检查可复用已构建的 `tests/managed_host` 夹具：

```sh
cmake -S tests/managed_host -B build/managed-host -DCMAKE_BUILD_TYPE=Release
cmake --build build/managed-host --config Release
python consumers/android_managed/verify_host.py \
  --fixture-classes build/managed-host/classes \
  --fixture-jni "<managed-host 的 plain JNI 库所在目录>" \
  --out-dir build/android-managed-sample-host
```

单配置构建的 JNI 目录通常是 `build/managed-host/plain`；多配置生成器可能使用 `build/managed-host/plain/Release`。检查器使用夹具中的宿主 JNI 和 Android 类型 stub，验证单次合成事件、正常和重复关闭、注册未完成即抛异常后的清理及回调注册/注销；它记录夹具文件哈希，但不能单凭哈希证明夹具由当前源码构建，也不覆盖系统注册后抛异常的未知行为。stub 不代表 Android framework，原始 stderr/stdout 可能含路径，应保留本地。

AAR 编译和宿主测试均不启动 Android、安装 APK 或使用设备，不证明 ART、R8 运行、真实系统压力、设备稳定性或性能收益。完整压力与预算合同见 [托管域压力接入](../../docs/ANDROID_MANAGED_PRESSURE_CONTRACT.md)。
