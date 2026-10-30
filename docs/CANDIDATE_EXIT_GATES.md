# 候选与退出门槛

当前决定仍为 **不接入 BIL**。本批只为两条有实际消费者的研究路径补足功能与资源依据，没有添加生产后端。

| 候选 | 本批交付 | 后续进入条件 | 退出 / 降级 |
|---|---|---|---|
| DEX source borrow | 冻结 SDK 合同和 D-C0/Z0/C1/C2 功能 harness | 全输出与最后 release 正确，源 capacity/唯一 backing 分栏，另冻结完整 owner task 协议 | 合同失败即停；只有资源收益则 RESOURCE_BENEFIT_ONLY；普通借用足够时降低 Memory 优先级 |
| 图像三行环 | full/ring × direct/System/arena、持久 workspace、独立 oracle | 每个像素只转换一次、逐字节相同、各 provider 同布局、有限批次与最终归零 | 输出/生命周期失败即停；时间资格未测不宣布加速；普通布局节省不归库 |
| 多对象 arena/reset | 现有 API 的同寿命 workspace 对照 | 与普通持久 buffer 比较，真实变尺寸/延寿模式和预算冻结 | 普通基线稳态零申请且无净益、pinning/retained 超预算则不接入 |
| BIL 生产候选 | 固定源码与既有能力去重清单 | 真实热点/成功定义/owner/注入点成立，迁移成本可测，强 A1 后还有改进空间 | 无热点、无合法注入点或普通优化足够，保持现有实现 |
| 特殊 placement / slab | 未选择 | 新的 profile 或机制/净收益证据 | 无证据不实现；无 PFN/PMU 不声称 PA/TLB 资格 |

功能、性能、机制与发布分别过门：功能 PASS 仅证明既定用例；宿主资源模型不替代 RSS；设备性能需要新制品和冻结协议；BIL 需要产品路径与回归；16 KB、MTE/HWASan、能量和最终产品依赖闭包独立记录。

新版统计以预排采集窗口为主单位。后续冻结版本已完成4个pilot窗口；DEX formal只完成4/12窗口后因环境停止，图像因pilot精度不足未进入formal，见[最终结果](DEVICE_V2_RESULTS.md)。缺失记录保留为INCOMPLETE，趋势/实际精度/外层独立性不支持时为INCONCLUSIVE。不能把连续采集切成多个名字取得资格，不能追加pilot直到有利；旧轮已结束，新问题必须冻结独立协议。见[冻结清单](PROTOCOL_FREEZE_CHECKLIST.md)。
