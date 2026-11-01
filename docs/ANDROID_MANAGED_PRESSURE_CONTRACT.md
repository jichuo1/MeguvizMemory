# 托管域的可选压力接入：合同草案

状态：**设计草案，未实现、未接线、未进行设备或性能验证**。本文件依据当前工作树核对已有接口，供后续有真实需求时选择；它不改变 Memory、BIL、Lumen 或其他库的依赖，也不要求新生产依赖。托管域当前只有显式 `setBudget`，并没有同域自动压力控制。[托管域合同](ANDROID_MANAGED_DOMAIN.md)和[现有压力适配](ANDROID_PRESSURE.md)分别描述已实现范围。

## 当前代码与实际需求

- `workspace_jni.cpp` 中的托管关闭 worker 先对核心域调用 `begin_close`，随后等待自己的 in-flight、owner 和 lease pin 归零，最后 collect/destroy。它没有持有或关闭 pressure controller 的步骤。
- `mgmem_pressure_create_v1` 会 retain 原 domain owner；controller 的 worker 做 quiet-only trim，只有 controller 关闭、drain、join/destroy 以后才归还 owner。`mgmem_pressure_notify_v1` 在 controller 锁内同步调用 core `set_budget`，并不是单纯排队；它不在通知线程调用 trim/free/drain，但也没有严格的无锁或墙钟上界。
- 现有 `NativePressureBackend.fromNativeRegistration` 包装的外部注册只负责关闭 controller，不拥有或关闭外部 domain。`MemoryPressureAdapter` 接到系统事件后会调用 backend；其 `forwardedEvents` 是 backend 返回成功的计数，不证明系统真实低内存或内存已回收。
- 已有的 `consumers/image/pressure_stability.cpp` 是使用真实原生压力控制器的图像工作区；它不验证新 Java 托管域。当前 Java 图像 CLI 在工作前进入域关闭，并非持续接收 Android 压力的产品场景。因此本草案的直接价值是补齐可复用能力，**不是已经证实存在产品净收益**。

本次所审关键源码 SHA-256：`workspace_jni.cpp` `c700332b5a8e1140939e808f0b93eb83acb6690748314819ac08f7a017c5f6f8`；`ManagedMemoryDomain.java` `bb24edad0ea8637c66586c04d47d5117d9c12c76521241a99956eaf314175441`；`pressure.cpp` `428f3bdeb9ef335e7c8dcafa171f571d310c04325b1c6a65c950ff7a5d5c23a5`；`pressure_jni.cpp` `7c845a730607b1e2df1b5d82b8e435d134e65def8ff530a51a662acdaeca470e`。这是当时观察到的源码字节，不单独证明构建或运行来源。

## 所有权与关闭次序

1. 只允许显式 opt-in、只在同一已加载共享 Memory runtime 的自有 System/Arena 域上附着一个 controller；不返回 raw domain 指针，不接管外部 owner。attach 可能分配或启动线程，安排在工作线程；在调用 controller create 前预留稳定的 no-throw owner pin，直至调用完整返回。创建、绑定或 Java 认领失败须有明确的取消或未知隔离路径。
2. 为 controller 引入独立的通知/观测入场计数与唯一 detach claim。关闭 pressure binding 与关闭 domain 竞争时，只有一个清理执行者负责 begin-close、drain、join/destroy；其余调用只观察状态，不重复销毁。已入场调用完整返回前不得释放 controller。
3. `ManagedMemoryDomain.beginClose` 先关闭新 owner、lease 和压力事件的准入，再由清理工作线程**先请求并完成 pressure controller 的关闭与销毁**，然后才让域进入 core `begin_close`，等待其他 owner、lease pin、guard 和相关 core 调用返回，再 collect/drain/snapshot/destroy。不能先等待全部 domain pin、再等待 controller 自行归还 pin，否则形成循环；也不能让占满配额的 domain 清理线程等待一个尚未获得线程配额的 pressure 清理任务。
4. 如果 controller worker 或 provider 回调未完成，域保持 CLOSING；超时只影响等待者，不强制释放。创建、线程准入、bind、detach 或 destroy 返回未知/可能已消费的错误时保留必要记录和 pin，报告 QUARANTINED/UNKNOWN，不盲目重试、不宣称 backing 归零。`CLOSED` 仅在相关已确认调用完成后发布，仍不证明所有 Java/JNI 栈、系统线程或 GPU retirement 已结束，不能据此卸载 DSO。
5. `MemoryPressureAdapter.close()` 的既有含义是注销自己的 framework callback 并关闭 backend；不能悄悄将其改成关闭整个 managed domain。域关闭时必须能封闭并请求本 attachment 清理；framework callback 的注销由明确的组合 owner 或调用方负责。忘记注销时须可观测，不能假定 Android Context 已丢弃引用。

任何涉及 controller 的 C 调用都应先取得必要的寿命/in-flight pin，释放 managed domain、registry 和 Java monitor 后再进入 controller；不能持 managed domain 锁再取 controller 锁。现有 controller 在自身锁内进入 core 预算/快照路径，违反这一顺序可能形成锁环。生命周期回调不抛异常、不在 UI 路径执行 trim/free/join；短锁也不意味着硬实时。

## 手动预算、事件与观测

建议托管域与 pressure controller 使用**同一预算仲裁者**，不允许两个独立写入者相互覆盖。附着成功时记录当前手动目标 `M`；`M` 可由既有 `setBudget` 显式修改，允许为零。压力等级为 NORMAL 时有效目标为 `M`；MODERATE/CRITICAL 分别为 `min(M, moderateTarget)` / `min(M, criticalTarget)`。恢复 NORMAL 必须是明确请求，不能从一段时间没有回调推断；降低预算不撤销已有 owner、lease 或已持有 reservation。每次变更只对 core 施加一次已计算的有效目标，不能先暂时抬高预算再 clamp，否则可能放行超预算的新申请。

现有 controller 使用固定 normal/moderate/critical 写预算，**没有动态手动目标的现成接口**。现有 `mgmem_pressure_create_v1` 还要求 `normal_budget_bytes > 0`，而托管域的软预算可以合法为零：零预算域不能直接按现有合同附着，不能为了通过创建校验而临时抬高预算或绕过手动上限。后续需要设计兼容此情况的新接口或安全拒绝附着，不默认改变旧接口语义。因此简单把现有 backend 和 `ManagedMemoryDomain.setBudget` 同时连到域是不完整的；需要扩展或封装 controller，使手动变更与压力应用按同一序列和锁序线性化，同时保持未附着时旧 API 的同步语义。不能在 Java 里只做“通知后再次降低”的补丁来掩盖竞争。

若要求 framework 回调不等待 native controller，则应使用有界异步投递：回调只做有限准入/投递，不调用 trim/free/drain；入队成功是 **ACCEPTED/QUEUED**，不是预算已应用。队列满或争用显式记录 BUSY/拒绝，不静默丢弃 critical；记录已接收、已应用、失败、合并和各自序号。仅当等级、手动预算代次和策略代次相同且先前应用已成功时，才可合并重复事件；失败、重试、明确 NORMAL restore 或预算变更不能被误去重。若应用要求压力事件受理后立即禁止超新预算申请，必须另行设计与 core reservation 原子衔接的准入机制，不能把异步排队结果假称即时预算生效。

domain 逻辑预算/backing 账目、pressure 入队与 trim 结果、进程 RSS/PSS 分栏记录；不相加为物理内存，也不从 trim 回执推断系统低内存或收益。worker、队列和 registration 有独立上限及失败原因，不计入 domain backing。Android 自 API 34 起不再向应用递送多种旧的 RUNNING/MODERATE/COMPLETE 级别，`onLowMemory` 也不再调用；显式请求与恢复仍需保留，不能假设一定收到自动事件。[Android ComponentCallbacks2](https://developer.android.com/reference/android/content/ComponentCallbacks2)

## 实施与验收前置

- 先实现可独立关闭的同域绑定和预算仲裁，并保持默认未启用；不要修改 BIL 或接管 Lumen/Bitmap/HWUI/GPU owner。明确 API 版本、现有 C/Java 行为和系统库加载闭包。
- 宿主受控测试应覆盖手动预算为零、压力/restore 与新申请的并发、重复事件与队列拒绝、attach/关闭竞争、worker 配额耗尽、已持 lease 的 BUSY trim、controller 已消费错误和 owner pin 完整释放；故障隔离不计正常归零。
- 真实 Android 仅在设备明确空闲并冻结同版本制品/协议后验证 callback 注册与注销、同域预算、活动 lease、压力、关闭和异步清理的组合；模拟分发/AMS 注入与真实系统低内存分开记录。R8、API 版本、16 KB、MTE、长期稳定与性能分别资格，不把宿主或构建结果转授设备。
