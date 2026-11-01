# Java/JNI P6 图像消费者

这是独立的宿主 JVM 消费者，读取本地 P6 RGB 文件，输出 P5 灰度文件。灰度为 `(77R + 150G + 29B) >> 8`，再进行 clamp 边缘的 3×3 均值模糊，与 `consumers/image/main.cpp` 保持相同像素语义。它实际调用安装后的共享 Memory core 和已安装的 host JNI 组合；不使用 BIL、GPU 或第三方生产依赖。它不是 Android app、ART 运行证据或性能基准。

## 所有权与成本

Java 通过 `ManagedMemoryDomain` 创建 System 或 arena 域及完整 RGB allocation；托管实现初始化 backing，再取得 WRITE lease。allocation owner 随后释放、托管域进入 CLOSING，已有 lease 仍可读写。文件数据通过受控 `byte[]` 分块写入；worker 在任务创建前已经持有 lease，再从 native 读回并在普通 Java `byte[]` 中计算 luma/输出。worker 完整结束后才关闭 lease，并等待托管清理 worker 完成：仅在自身域 CLOSED、最终快照归零并释放观察 token 后报告正常清理。中断时请求 worker 中断，但仍等待 worker 实际结束才释放 lease；不会以超时强行释放在用 backing。明确未准入的 guard/worker 失败可有限重试；异常、未知、超时或隔离状态不会重复释放，不能确认回收时保留域/owner，并报告错误而非伪称归零。命令行的有界等待不适合直接放在 UI 线程。

显式选择 `--live-domain-pressure` 时，域在图像工作期间保持 OPEN，并在该域上绑定压力控制器。消费者提交合成 CRITICAL、在后台有界等待指定 ticket 实际 APPLIED，核对零预算、拒绝新申请和租约持有期间的 busy；worker 仍用原有租约完成同样的像素处理。提交时仅对明确未受理的 BUSY 在同一应用期限内重试，统计 `pressure_submit_busy_rejections`；一旦取得 ticket 就只查询该 ticket，不重复提交。其他拒绝、事件失败、取消、过期或等待超时都不算成功。租约释放后，arena 场景等待静默 trim 成功及 backing 归零；system 场景只观察实际释放，不将其归因于 trim。随后显式恢复 NORMAL，确认 binding 关闭，再关闭域。此模式不是 Android 系统回调，也不能证明设备或性能收益。正常报告的观察数组 payload 是各次查询返回的 Java `long[]` 累计元素字节，不含对象头、不代表同时存活内存或进程峰值。

Memory 默认不初始化：本消费者为避免读取未初始化字节，先在 native 初始化整个 RGB 范围，初始化量单独计数。报告分别记录文件到 Java、Java 到 JNI 暂存、暂存到 native backing 和 native 回到 Java 的字节数，以及 Java luma/output 和固定传输缓冲区的 payload。Java 对象头、流缓冲、JNI 元数据、staging 分配器开销、RSS/PSS 未纳入这些字段；不能把这些数据相加当成全进程峰值，也不声称零复制或任何净收益。输入最多 8 Mi 像素，域预算默认 64 MiB，`--budget` 可指定更小的正整数预算；该预算不覆盖 Java 数组或 JNI 暂存。

输出使用 `CREATE_NEW`：已有文件不会被覆盖，这一点比原 C++ 演示消费者更保守。若新文件已经创建后发生 I/O 失败，可能留下不完整文件，程序不会自动删除它。错误不会输出成功 JSON；`managed_cleanup` 只报告本次消费者域的已知状态，不代表其他域、工作线程或整个进程已经静止，也不证明 JNI DSO 可以卸载。

## 独立构建

先准备通过 `find_package(MeguvizMemory CONFIG REQUIRED)` 可发现的**共享** Memory SDK，并用相同 SDK 单独构建、安装 host JNI：

```sh
cmake -S consumers/java_image/runtime -B build/java-image-jni \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="<Memory SDK>" \
  -DCMAKE_INSTALL_PREFIX="<host JNI 安装前缀>"
cmake --build build/java-image-jni --config Release
cmake --install build/java-image-jni --config Release
```

host JNI 由仓库中的 Android JNI 适配源码重新构建，包含压力、借用和托管域实现并链接安装的单份共享 core；它不是 Android AAR 里的 `.so`，两者的构建和运行结果不能互相转授。Java 类可以从与源码匹配的 AAR `classes.jar` 提供；开发期间也可用既有 JDK 编译 `NativeMemoryLease.java` 和 `ManagedMemoryDomain.java` 并打成独立 JAR。随后配置消费者：

```sh
cmake -S consumers/java_image -B build/java-image \
  -DMGMEM_ANDROID_CLASSES_JAR="<匹配的 classes.jar>"
cmake --build build/java-image --config Release
```

上述默认只编译 Java 消费者，不构建身份诊断 sidecar。若需要 `--attest-*`，配置时另加 `-DMGMEM_JAVA_IMAGE_BUILD_IDENTITY_PROBE=ON`、`-DCMAKE_PREFIX_PATH="<Memory SDK>"` 和 `-DMGMEM_JNI_PREFIX="<host JNI 安装前缀>"`；此时也需要 C++ 编译器与 JNI 头。运行时两种模式均需要已安装的 managed JNI 与同一共享 Memory core。

运行时让安装 core、host JNI 与消费者 JNI 所在目录均可由系统加载器找到；classpath 包含 `build/java-image/classes` 与 `classes.jar`。CLI 主类为 `io.github.jichuo1.meguviz.memory.image.JavaImage`：

```text
JavaImage input.ppm output.pgm [--system] [--budget bytes] [--live-domain-pressure]
          [--attest-core path --attest-jni path --attest-jar path]
```

不带 `--system` 使用 arena。普通处理不加载本消费者的 native sidecar；只有三个 `--attest-*` 参数一起提供时才显式加载 sidecar，用操作系统模块地址查询 core/JNI 的实际加载文件，并验证其与预期文件是同一文件；同时检查两个 Java 适配类的 CodeSource JAR 与类资源并记录各文件 SHA-256，不输出绝对路径。它采样的是运行时路径与当前磁盘文件，不是对已映射内存的哈希；不用这些参数时明确记 `identity_scope=NOT_CHECKED`。

`verify_host.py --help` 提供隔离宿主检查入口：在新的 `build/` 子目录保存原始输出，以独立像素 oracle 和安装版 C++ 消费者逐字节对照；默认路径和显式合成压力路径分别验证相同图像，并测试单像素、边缘、截断、尾随数据、预算、既有输出和加载 JAR 不匹配；另在不提供 sidecar 搜索路径时运行普通处理。它重新编译当前两个 Java 适配源并核对 class 字节，以免把旧 AAR 的类当成新源码。报告仅为限定宿主功能检查，不代替后续统一设备、性能或发布验证。

已有共享 Memory SDK 时，可用统一入口独立构建、安装 host JNI，从当前 Java 源生成 JAR，再构建两个消费者并执行上述检查：

```sh
python consumers/java_image/run_host_ci.py --memory-sdk "<Memory SDK>" --out-dir build/java-image-ci
```

入口可选 `--java-home`、`--cmake` 和 `--generator`；要求现有 JDK 与 CMake，不下载依赖。它要求安装 SDK 中相关 C 头与当前源码一致，并拒绝覆盖已有输出；先独立配置/编译默认 Java-only 消费者，再单独构建可选探针并验证两种运行路径。结果和原始构建日志留在所给 `build/` 目录；顶层 `receipt.json` 只含测试范围、数量与哈希。Windows 和 Linux 均有构建入口，某个平台是否通过以该平台的实际运行记录为准。
