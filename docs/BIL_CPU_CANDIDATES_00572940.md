# BIL CPU 候选去重清单

2026-10-30 再核 main 为 `00572940c354fbec7b6f311d4ecd6f02e66ea7c5`。本批未改 BIL、未安装新依赖、未采产品 trace。下表是固定版本源码候选，不是已测热点或接入资格。

| 调用点 | 已有能力，不能重复计功 | owner / 最后释放 / 注入点 | 新增成本与反驳条件 |
|---|---|---|---|
| FrameScratchArena / EngineHolder preparation | P1-07 `e630ad56` 五槽复用、初始化和关闭已存在 | Java preparation owner；当前无 native Memory provider | JNI、存储迁移和延寿可能增加成本；无 allocation/GC/阶段热点证据则不迁移 |
| FrameBufferLedger | P2-05 `061360f5` holder 打包与 idle buffer 复用 | 帧 backing 直到所有 holder 退出；duplicate view 不自动 retain | 慢消费者/pinning 必须测；不能因 idle 项被删就提前回收 |
| frame_guard_geometry / CPU guard | P1-06 `046d87a3` 几何缓存、无 patch 跳过 motion；stack/std::array | guard 没有已证 heap 申请点 | 只可另研究计算/访问；没有 owned-native allocator 热点则退出 Memory 候选 |
| frame_engine staging / ncnn | 持久 staging、ncnn allocator、GPU cache 和提交后退役已存在 | Engine/ncnn/GPU 原 owner；Options 未暴露 Memory provider | staging commit 保持失败前 output/alias；不删除保护复制，不接管 GPU retirement |
| frame_engine keepAlive/downs/fresh 等局部容器 | 这些局部容器尚无热点测量；先比较普通 reserve/固定容量 | 必须活过 submit_and_wait；不能按 POD 随意 reset | profile 份额不足、普通优化已足够或退役复杂则退出 |
| Danmaku native flat → fromFlattened | Rust elem_body 复用、已存在 flat 与 !changed 快路均扣除 | Rust 输出用 dw_free_*；JNI 交给 Java 数组；ABI 无 Memory provider | packed reader 是普通消费者优化；保留 presence/null、range、拒绝/回退、复检和 host parseFrom，语义不等价即退出 |
| VersionAdapter → DexAssistSession → DexKitAssistEngine | lazy batched queries、每 archive 一次 create(path).use 已存在 | DexKit bridge owner，最多8 archive/32 match；无外部 Memory owner 参数 | 独立 DexMeguviz 全方法枚举不是此生产路径；未证 bridge/热点等价则不外推 |

来源采用固定版本 [BIL 源码树](https://github.com/jichuo1/Bilibili_Innocent_Lab/tree/00572940c354fbec7b6f311d4ecd6f02e66ea7c5/Bilibili_Innocent_Lab/app/src/main)。研究包包含14份文件的 blob 核对；本会话先前只读索引另记录了实际调用点与文件指纹，本批没有把其历史 benchmark 的 `device_run=false` 当成当前性能记录。

每个候选还缺频率/尺寸分布、实际关键路径份额、真实 owner/并发/失败轨迹、迁移成本和强 A1 对照。没有足够证据时，结果允许 NO_CANDIDATE，不为展示本库而新增缓存或改 ABI。P1-06/P1-07/P2-05 及持久 staging 的收益始终排除在 Memory 增量之外。
