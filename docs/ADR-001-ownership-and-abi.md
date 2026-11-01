# ADR-001：CPU 核心、版本化 C ABI 与显式 lease

日期：2026-10-30。状态：首版实现合同，ABI 1.0，产品发布资格另行验收。

本文保留首版历史范围；其中“本版未实现 JNI”等描述只适用于该快照，不代表当前源码状态。后续 JNI 受控借用、托管域和压力适配的实际合同分别见 [JNI 借用](ANDROID_NATIVE_BORROW.md)、[托管域](ANDROID_MANAGED_DOMAIN.md)和[压力适配](ANDROID_MANAGED_PRESSURE_CONTRACT.md)；当前交付与验证范围见[工程状态](../STATE.md)。

核心采用现有消费者可使用的 C++17/CMake；公开头只含 C 类型、固定宽度长度、状态码和 opaque domain。首版没有新增生产依赖。函数和 POD 均以 `_v1` 版本化，配置携带 struct_size / abi_major / abi_minor / required_flags。未知必需 flag、新 major 或不支持的 minor 明确拒绝。新增字段只能追加；旧符号合同不变。模块是 core、可选 C++ RAII、独立安装消费者及 bench，Android 构建仅是平台包装。GPU、帧调度、业务缓存不进入核心。

每个 domain 固定一个 provider，默认 System。创建时复制 vtable 并 retain context；最终 destroy 后 release。每份 backing 必须用原 provider 的相同地址、容量、对齐归还。调用方不 free 借用地址。自定义回调必须线程安全（含多 domain 共享 context）、不得抛异常、重入同一 domain 或在失败时返回半完成分配；回调在核心锁外执行，等待中的申请计入 reserved_bytes，避免并发绕过预算。核心边界捕获 C++ 分配异常并返回状态码；非法 provider 合同不是可安全恢复的任意内存管理协议。deallocate 违约会 poison 域并停止重试该地址；release 回调在 destroy 时违约抛异常，domain 仍被消耗，调用方不得重试已经销毁的指针。

domain instance ID、allocation ID、lease ID 不使用地址，计数单调且耗尽返回 OVERFLOW，绝不回绕。handle 另带 runtime_origin：它是仍装载模块内的路由 cookie，用于拒绝同时装载的另一份 core 的句柄，不是永久资源身份。运行库卸载前必须结束全部资源，卸载后的旧句柄不可复用。跨 domain、跨仍装载的 runtime 和重复 release 返回 STALE_HANDLE。generation 只用于诊断 reset，不负责保护已经泄露的指针。domain 原始指针必须传回 origin runtime，最终 destroy 由调用方与所有 API 调用串行化；句柄验证不能使已销毁的 domain 指针安全。

申请返回 owner handle；有效长度固定，不提供隐式 realloc。新申请失败保持旧 allocation 可用，增长须显式申请新 backing 再释放旧 backing，预算覆盖共存峰值。零长度申请/借用和非 2 次幂对齐拒绝；所有长度、乘法、舍入、offset 与 size_t 缩窄均检查。数据默认未初始化，消费者负责初始化并把它计入自己的工作量。

读 lease 可并存，写 lease 排斥同 allocation 的任何其他 lease。lease 在异步任务受理/入队之前取得，可跨线程交接。owner release 后禁止新 borrow，原 lease 继续有效，backing 进入 retiring；最后一个 lease 归还后才能最终释放。输出需延寿时保留 lease 或复制到独立 owner。JNI 不提供任意逃逸 DirectByteBuffer，未来只开放 opaque 批量操作或携带真实保活寿命的桥；本版未实现 JNI，不以 fixture 充数。

状态机 OPEN → CLOSING → CLOSED。begin_close 只改状态，不等待资源，也不执行可能阻塞的 provider 回调；新申请与新 borrow 被拒绝。CLOSING 允许 release/完成/collect/drain。所有 allocation/lease 与 pending 操作结束后，由清理 worker 显式 collect，同步归还 backing 并进入 CLOSED；有活跃资源返回 BUSY。drain 只等待 CLOSED，不运行任何 provider 回调，timeout=0 是轮询，超时保留资源。collect、trim 和可能触发最后 backing 回收的 release 允许同步 provider 调用，不能将这些路径放在不允许阻塞的线程。持有自身 lease 的 UI/worker 不能等待自己完成。destroy 只接受 CLOSED，BUSY 不销毁 owner/provider。

arena 是有界 monotonic chunk：同批次密集分配，所有 owner/lease 退出后 reset 才复用。trim 同样要求无活跃资源，并归还所有 backing；没有 purge 活对象或移动稳定地址。System 为每次申请独立 backing，arena 是其上的生命周期策略，未选 placement/slab/page hint 不阻塞基础版本。32 位受 size_t 与显式预算共同限制。

统计按唯一 provider backing 计算，预算是其逻辑请求容量及 pending reservation，不承诺 allocator 元数据、RSS/PSS 或物理 RAM。live_bytes 与 retiring_bytes 是不相交 payload；retained_bytes = backing - live - retiring，包含 arena padding、已释放但 reset 前不能重用的空间。backing 是三者的父项，不能再相加。peak 包含申请中旧新 backing 共存的 reservation；cumulative_requested 是成功逻辑请求之和。外部 GPU/Java/OS 元数据成本保持未知，不重复计入 CPU backing。

默认 dense/System。关闭策略只影响新 domain；旧 domain 按原 provider drain。没有特殊硬件、PFN、PMU、MTE、Android 16 KB loader 或产品性能通过声明。
