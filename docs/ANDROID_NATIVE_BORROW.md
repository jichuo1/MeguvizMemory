# Android 受控原生字节借用

这是可选 JNI 适配层。可信原生代码通过 `mgmem_android_borrow_register_v1` 把已有 Memory allocation 的一段 READ 或 WRITE lease 注册为进程内 token；Java 用 `NativeMemoryLease.fromNativeRegistration` 单次认领，然后通过 `read` / `write` 在已有 `byte[]` 与受控范围之间复制。该接口不返回原生指针、`DirectByteBuffer` 或其他可绕过关闭检查的引用。它没有提供 Java 侧原生 allocation 工厂，也不接管进程 allocator。

## 原生依赖与归属

声明位于安装后的 `include/mgmem/android_borrow_jni.h`，实现由 Android JNI 组合库 `libmgmem_pressure_jni.so` 提供，而非 `libmgmem.so`。同一进程的原生消费者须链接同一个已安装的共享 Memory core 和此 JNI 库，不能把 Memory 静态嵌入成第二套 runtime。该头文件依赖现有 `mgmem/pressure.h` 中的 `mgmem_domain_lifetime_v1` 合同；没有新增第三方生产依赖。

注册要求有效 domain、仍有 owner 的 allocation handle、范围和原 domain owner 的线程安全保活回调。调用方须一直保持这些参数有效直到注册调用返回；已取得的 lease 之后可在 domain 进入 CLOSING、allocation owner 已释放的情况下继续访问。domain owner 的 retain/release 负责保证 domain 在最后一次 `mgmem_borrow_release_v1` 完整返回前不会被销毁。适配器不关闭或销毁外部 domain。

**必须先初始化整个可借用范围**，并传 `MGMEM_ANDROID_BORROW_INITIALIZED`。Memory 的原生 allocation 默认未初始化；此标志是 origin owner 的明确保证，不是适配器能验证的事实。WRITE lease 同时允许读取，也必须先初始化。Memory 管理范围之外的别名、DMA、线程同步和外部 owner 行为仍由原 owner 负责；不能把 Memory lease 当作对其他别名的硬件访问保护。

token 是只在当前已加载 JNI runtime 内有效、单调且不复用的标识，不是地址，也不是访问控制或可持久化的凭据。注册成功后只能被一个 Java wrapper 认领；认领前若绑定失败或不再需要，由原生调用方调用 `mgmem_android_borrow_cancel_v1`。Java 已认领的 token 不能再由 cancel 关闭。未认领 token 和忘记关闭的 Java lease 会保留 native lease 与 owner；不依赖 GC/Cleaner 自动恢复，也不把元数据计入 domain backing 预算。

## 访问和关闭

`length` 是注册范围长度，可大于单个 Java 数组。每次读写都分别检查 lease 范围、Java 数组范围、非负值及权限；native 侧也会重新检查。READ lease 不支持写入，WRITE lease 是 core 的独占写借用。Java 方法同步串行化同一 wrapper 的访问和 close；native 层也会等待已入场访问完成后再释放 lease。数组传输期间不保存 Java 数组或数组指针，JNI 抛出的异常保留给调用方。读入调用方 `byte[]` 时，如果 JNI 发生异常，目标数组可能已被部分修改，不能假定其保持原值。

每次 `write` 上限为 1 MiB；该 JNI runtime 内所有注册共享最多 16 MiB 的并发写入暂存 payload 配额。如果多个类加载器或重复装载的 DSO 导致独立的 JNI runtime 副本，各副本不共享这项配额，因此它不是强制的进程总上限。申请配额、分配暂存或 JNI 从 Java 数组复制失败时，native backing 不变；只有复制成功才提交。暂存 payload 不计入 domain 预算，分配器开销和其他进程内内存也不包含在该配额中；额外复制、分配及并发额度拒绝是使用成本。更大写入由调用方显式拆为多次独立提交：前一笔成功后，后一笔失败不会回滚前一笔。该保证针对可恢复错误，不保证崩溃原子，也不保证其他未经同步的原生别名看不到提交过程。

`close` 幂等，但可能在其调用线程同步执行原 provider 和 domain-owner release 回调，应由应用选择合适的清理线程，不能默认在 UI 线程调用。关闭后不再接受访问；现有 Java byte[] 中的副本不受影响。关闭错误时将该注册隔离，不能假定 lease 或回调未被消费，也不能重试或声称资源归零。若 foreign 回调违背 no-throw 合同，则其引用状态可能未知；适配器不能修复违约或保证强制回收。JNI DSO 在注册、调用或隔离项仍存在时不支持卸载。

## 当前验证边界

独立宿主测试 `tests/borrow_host` 以真实 core 加 fake JNI 覆盖初始化标志、重复认领、权限/边界、owner 先释放、domain 关闭后访问、访问与关闭竞争、pending JNI 异常及失败回调隔离；另一项使用真实宿主 JVM、真实 JNI 和 core 进行字节读写、关闭与域清理。该测试组合的宿主 JVM fixture 将 core 编入测试 DLL，仅用于单进程测试，不是 Android 发布库链接方式。

尚需统一验证 Android NDK 构建、最终 AAR/R8、设备 ART、压力与生命周期组合、长期稳定性和真实消费者接入；宿主结果不替代这些设备和交付资格。BIL 未接入，也无性能或零拷贝收益声明。
