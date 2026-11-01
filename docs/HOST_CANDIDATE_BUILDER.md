# 宿主工程候选构建器

`tools/build_host_candidate.py` 在本机 Windows x86_64 或 Linux x86_64 从隔离源码快照构建可安装的共享核心、压力静态库、宿主 JNI 和纯 JVM 受控 API JAR。Windows 输出 ZIP，Linux 输出保留库相对符号链接的 tar.gz；两个平台必须分别在相应系统实际构建，不能从一端结果推定另一端通过。工具不访问设备、不签名或发布。候选不是正式 Release，项目许可证仍须确定。

JAR 只含 `NativeMemoryLease`、`ManagedMemoryDomain` 及其嵌套类，不包含 Android framework 回调类。构建器对这两个随 JAR 交付的类生成 JNI 头并参与原生编译；JNI 中未随 JAR 交付的 Android `NativePressureBackend` 方法仅检查导出名，不宣称其 Java/JNI 签名已由此宿主流程验证。宿主 JNI 与 Android AAR JNI 是分别构建的二进制，不得互相当作 Android 或 ART 运行证据。JNI 和消费者必须链接并加载同一个共享 Memory core，不能另外静态嵌入第二份 runtime。

## 使用

导出包含本工具及其依赖工具的目标源码到独立目录，并在该目录外创建源码锁：

```text
python tools/source_lock.py --root build/frozen-source --output build/frozen-source-lock.json
```

预先准备本机 C/C++ 编译器、CMake、Ninja、JDK 和 LLVM 的 `llvm-strip`、`llvm-readobj`、`llvm-nm`、`llvm-ar`、`llvm-readelf`。Windows 须先在当前进程初始化 Visual Studio 的 C++ 开发环境。私有标识文件仅留在 `build/`，每行一个非空标识：

```text
python tools/build_host_candidate.py --source-root build/frozen-source --source-lock build/frozen-source-lock.json --jdk "<JDK目录>" --cmake "<cmake可执行文件>" --ninja "<ninja可执行文件>" --llvm-bin "<LLVM工具目录>" --private-identifiers-file build/private-identifiers.txt --work-dir build/host-candidate-work --output "<dist下不存在的新候选文件>"
```

输出名在 Windows 必须以 `.zip` 结尾，在 Linux 必须以 `.tar.gz` 结尾。`build/` 与 `dist/` 解析后须位于工作区；源码和工作目录不能互相包含。源码、锁、构建工具与编译器应保持静止；结果包含来源与工具哈希，而非签名的完整构建来源证明。

工具只移除压力静态库中的调试段；Windows 比较处理前后的非调试 COFF 段、重定位和外部定义，Linux 比较已分配 ELF 段、非调试重定位和外部定义，不声称其他非分配段均被对比。core 的冻结头文件声明与压力静态库公开声明均逐一检查定义；Windows PE 函数还检查非零、非转发 RVA 位于有效可执行节。Linux ELF 只检查符号名称、类型、可见性和非 UND，不证明每个符号指向有效可执行节。共享核心、JNI 和导入库不改写，若仍含本机路径或私有标识则失败，保留工作目录供诊断。Linux 归档只允许 `sdk/lib/` 中指向包内文件的相对库链接，并拒绝逃逸和环；解包时须保留它们。Windows 需要兼容的 Microsoft C++ 运行时，Linux 需要兼容的系统 C/C++ 运行时；这些运行时均不捆绑。

候选共享核心和 JNI 的动态依赖必须属于构建器中的有限系统/C++ 运行时白名单；JNI 只能依赖一份匹配的 core SONAME。Linux 候选拒绝所有 RPATH/RUNPATH、FILTER/AUXILIARY 和 AUDIT/DEPAUDIT 标签；调用方负责运行时的合法加载路径。包载入和进程内实际动态库搜索仍取决于调用环境，不承诺防止调用方后来替换文件或加载另一 runtime。

白名单的 Windows 部分为 `KERNEL32.dll`、`MSVCP140.dll`、`VCRUNTIME140.dll`、`VCRUNTIME140_1.dll`、`ucrtbase.dll` 及 `api-ms-win-crt-*-l1-1-0.dll` 中明确列出的 heap、string、runtime、math、stdio、locale、convert、time、filesystem、utility、environment、conio 组件。Linux 部分为 `libc.so.6`、`libstdc++.so.6`、`libgcc_s.so.1`、`libm.so.6`、`libpthread.so.0`、`libdl.so.2`、`librt.so.1`、`libatomic.so.1` 与 `ld-linux-x86-64.so.2`；实际出现的子集记录在 manifest。JNI 另允许且要求一份 `mgmem.dll` 或 `libmgmem.so.1`。不在列表中的依赖会被拒绝；这些是明确的工程候选边界，不表示每种操作系统版本都验证过。

输出前会从候选归档回读 SDK 与 JAR，独立构建并执行真实宿主图像消费者，检查像素、合成压力、失败路径和实际加载的模块文件身份；另从回读 SDK 的脱敏压力静态库链接运行 v1/v2 契约，逐进程确认实际 core 模块身份和 checked-release 能力。压力测试只改名和变更 `main` 返回类型以加入身份检查，源码和改写版本的哈希分别记录；故障隔离不算正常回收。它只证明该宿主、该工作量和对应的文件；源码、路径、磁盘文件哈希并不证明已映射内存逐字节相同，也不说明性能、RSS、能耗、Android/R8/设备或 BIL 资格。失败、超时和原始构建日志保留在私有工作目录，不应分享整个目录。
