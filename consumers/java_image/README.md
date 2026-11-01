# Java/JNI P6 图像消费者

这是独立的宿主 JVM 消费者，读取本地 P6 RGB 文件，输出 P5 灰度文件。灰度为 `(77R + 150G + 29B) >> 8`，再进行 clamp 边缘的 3×3 均值模糊，与 `consumers/image/main.cpp` 保持相同像素语义。它实际调用安装后的共享 Memory core 和已安装的 host JNI 组合；不使用 BIL、GPU 或第三方生产依赖。它不是 Android app、ART 运行证据或性能基准。

## 所有权与成本

原生侧为完整 RGB 输入创建一个 System 或 arena 域，显式初始化全部 backing，登记 WRITE lease 并保活域 owner。Java 认领该 lease 后，原 owner 被释放、domain 进入 CLOSING；持有的 lease 仍可读写，collect 必须返回 BUSY。文件数据通过受控 `byte[]` 分块写入；worker 在任务创建前已经持有 lease，再从 native 读回并在普通 Java `byte[]` 中计算 luma/输出。worker 完整结束后才关闭 lease、collect、检查零账目并销毁域。中断时请求 worker 中断，但仍等待 worker 实际结束才释放 lease；不会以超时强行释放在用 backing。异常导致无法确认回收时保留域/owner，并报告错误而非伪称归零。

Memory 默认不初始化：本消费者为避免读取未初始化字节，先在 native 初始化整个 RGB 范围，初始化量单独计数。报告分别记录文件到 Java、Java 到 JNI 暂存、暂存到 native backing 和 native 回到 Java 的字节数，以及 Java luma/output 和固定传输缓冲区的 payload。Java 对象头、流缓冲、JNI 元数据、staging 分配器开销、RSS/PSS 未纳入这些字段；不能把这些数据相加当成全进程峰值，也不声称零复制或任何净收益。输入最多 8 Mi 像素，域预算默认 64 MiB，`--budget` 可指定更小的正整数预算；该预算不覆盖 Java 数组或 JNI 暂存。

输出使用 `CREATE_NEW`：已有文件不会被覆盖，这一点比原 C++ 演示消费者更保守。若新文件已经创建后发生 I/O 失败，可能留下不完整文件，程序不会自动删除它。错误不会输出成功 JSON；`native_sessions_remaining` 仅统计此消费者 runtime 的注册项，不代表整个进程或其他 runtime 已无资源。

## 独立构建

先准备通过 `find_package(MeguvizMemory CONFIG REQUIRED)` 可发现的**共享** Memory SDK，并用相同 SDK 单独构建、安装 host JNI：

```sh
cmake -S consumers/java_image/runtime -B build/java-image-jni \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="<Memory SDK>" \
  -DCMAKE_INSTALL_PREFIX="<host JNI 安装前缀>"
cmake --build build/java-image-jni --config Release
cmake --install build/java-image-jni --config Release
```

host JNI 由仓库中的 Android JNI 适配源码重新构建，链接安装的单份共享 core；它不是 Android AAR 里的 `.so`，两者的构建和运行结果不能互相转授。Java 类可以从与源码匹配的 AAR `classes.jar` 提供；开发期间也可用既有 JDK 编译 `adapters/android/java/.../NativeMemoryLease.java` 并打成独立 JAR。随后配置消费者：

```sh
cmake -S consumers/java_image -B build/java-image \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="<Memory SDK>" \
  -DMGMEM_JNI_PREFIX="<host JNI 安装前缀>" \
  -DMGMEM_ANDROID_CLASSES_JAR="<匹配的 classes.jar>"
cmake --build build/java-image --config Release
```

运行时让安装 core、host JNI 与消费者 JNI 所在目录均可由系统加载器找到；classpath 包含 `build/java-image/classes` 与 `classes.jar`。CLI 主类为 `io.github.jichuo1.meguviz.memory.image.JavaImage`：

```text
JavaImage input.ppm output.pgm [--system] [--budget bytes]
          [--attest-core path --attest-jni path --attest-jar path]
```

不带 `--system` 使用 arena。三个 `--attest-*` 参数必须一起提供：消费者用操作系统模块地址查询 core/JNI 的实际加载文件，并验证其与预期文件是同一文件；同时检查 Java `NativeMemoryLease` 的 CodeSource JAR 与类资源并记录各文件 SHA-256，不输出绝对路径。它采样的是运行时路径与当前磁盘文件，不是对已映射内存的哈希；不用这些参数时明确记 `identity_scope=NOT_CHECKED`。

`verify_host.py --help` 提供隔离宿主检查入口：在新的 `build/` 子目录保存原始输出，以独立像素 oracle 和安装版 C++ 消费者逐字节对照，并测试单像素、边缘、截断、尾随数据、预算、既有输出和加载 JAR 不匹配。它重新编译当前 `NativeMemoryLease.java` 并核对 class 字节，以免把旧 AAR 的类当成新源码。报告仅为限定宿主功能检查，不代替后续统一设备、性能或发布验证。
