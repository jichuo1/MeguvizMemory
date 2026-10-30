# Packed reader合同与验证

2026-10-30。MM-010交付独立纯Java reader，生产无Kotlin或其他第三方依赖；**BIL尚未接入，host parseFrom、Hook/UI和性能没有由本批取得资格**。该模块针对普通消费者的逐元素对象物化，不是Memory分配器接入或加速证据。

## 真实输入合同

只读审计BIL HEAD `00572940c354fbec7b6f311d4ecd6f02e66ea7c5`；验证前后HEAD相同、两份oracle源码逐字节SHA一致。源路径均相对该仓库：

| 来源 | SHA256 |
|---|---|
| `Bilibili_Innocent_Lab/app/src/main/java/com/Bilibili_Innocent_Lab/xposedmodule/hook/feature/danmakuwire/DanmakuWire.kt` | `ce221d29a11b1d8b435df751bcd00ba9b173a3b66f949edebcf87ae79c377ae8` |
| 同目录 `DanmakuWireFields.kt` | `00b263ea3e347add784c3eaa8e14e76dc8aa7da5a7c12af53a44da62fe8745a8` |

已核Rust `flatten`、JNI移交及真实caller：flat是 `[N,S,(weight,colorful,flags,contentStart,contentLength)×N,(kind,present)×S]`，没有额外magic/version；element flags bit0/1/2表示weight/colorful/content存在，source present为0或1。presence与值独立，0、负数、MIN/MAX都不是空值哨兵。

严格限制N/S≤65536、reply≤16MiB、精确总长度；长度算术使用long。拒绝负count、超限、截断、尾部额外项、未知presence bits、非0/1 source presence和存在内容的越界区间。Rust缺失content的原始offset为0，Kotlin producer为−1；缺失时均按原fromFlattened返回−1/0，不能因此拒绝有效输入。对于fromFlattened原先可能接受、但不属于两个真实producer的非canonical flat，本reader明确拒绝，且不触发JVM重试。

## 所有权、预算和关闭

- `copyOf`先检查payload预算，再复制flat与reply两个primitive数组，在复制品上完整校验。调用者不得在构造期间并发修改输入；构造完成后，原数组的后续变化不影响reader。
- `borrowImmutable`保留两个数组的强引用，要求调用者在close前不修改。强引用保活不能强制不可变，也不是native Memory lease；没有暴露原数组、裸指针或可逃逸元素view。
- 所有读取、批量决定和close由同一monitor串行化。close幂等、丢弃两份引用，后续读取拒绝；已返回的String与Decision位图独立有效。Java GC/RSS回收时间不作保证。
- 可见payload预算为 `4×flat.length + reply.length`；copy模式将其记为reader owned，borrow模式分栏为borrowed。该预算不含对象头、Decision位图、原调用者继续保留的输入或整个JVM/RSS。默认copy时输入和副本可能同时存在，不能把单reader预算称为总体峰值预算。
- Decision另有 `2N+S` 字节的boolean payload预算（对象头另计），拒绝后reader保持可用。语义位图由原judge/normalizer提供，调用期间不得并发修改。

## 调用语义

读取presence、signed值、content范围和UTF8时与真实fromFlattened等价。`decide`保留原权重熔断：分段没有任何正权重时放行权重过滤；已有正权重时缺失weight仍保留，存在weight按阈值比较。语义位图与weight组合；VIP改色仅作用于最终保留且colorful存在的记录，source缺失kind保留。

`selectImmutable`只在native不可用时选JVM provider。已选择的native拒绝或非法flat返回null，不重试JVM；测试用provider计数核对这一边界。Reader没有替代语义judge、TextNormalizer、wire重写、rewrite后rescan、条数/改色复检、`!changed`原reply快路或host parseFrom。Decision位图可交给原rewriter，但本批没有改BIL接线。

## 实际验证与归因

证据：[packed receipt](../artifacts/functional-next-20261030/packed/receipt.json)。使用已有JDK17和本机已有Kotlin compiler2.4.10，复制上述两份真实源码到build后独立编译，未构建外部项目。

- Java32项合同：presence/零值/全32位值、空记录/超限、畸形计数和范围、预算拒绝、快照别名隔离、关闭后拒绝、跨线程读取与close、native拒绝不回退。
- 48个真实wire格式fixture ×6种policy =288项差分；147次成功rescan及79次oracle规范性重写拒绝，输出/位图/presence和复检一致；含未知字段、重复字段、空内容和畸形UTF8。另在该harness验证N/S各65536的合法上界。
- 生产字节码不引用ElemSignal、ArrayList或Integer装箱；JAR仅生产4个class文件，oracle与测试排除。仍有reader、位图、异常及按需String分配，不声称零分配。
- `N+N′=5025`为这些fixture在原fromFlattened路径上的ElemSignal构造调用**结构模型**，N′只计需要且成功进入rescan的元素；不是实际heap profiler计数，也不转成时间、RSS或能量收益。验证工具本身会构造oracle对象，不属于生产优化统计。

Native Rust/JNI生产者没有在本批执行，host parseFrom和真实宿主回放未执行；对应源码合同已核，差分基准为真实Kotlin源码。公开CI可执行不依赖外部oracle的Java合同，`--java-only`会明确标注oracle未运行，不替代本地真实oracle证据。
