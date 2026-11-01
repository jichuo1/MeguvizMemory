# 多域 Arena 阶段交错功能诊断

`mgmem_tlb_multi_arena` 是 MM-021 的独立功能诊断。它使用本库 System provider 创建 2–8 个互不共享的 Arena 域，在持有每个 allocation 的 WRITE lease 时交错访问域内自有数据；不使用地址别名、特殊 placement、page hint 或外部地址映射。它不修改既有 `MM-003-host-functional-diagnostic-v1` 单实例冻结协议，新的输出使用 `MM-021-multi-arena-functional-v1`。

## 工作量与生命周期

- 可指定每域 1–16 个 allocation、每 allocation 1–64 个实际系统页、2–1024 遍读写。所有域总触页最多 1024，管理 backing 总预算最多 64 MiB，访问总数最多 16,777,216；尺寸、运算、地址范围和预算在使用前检查。每个域的预算和单个 arena chunk 为其全部 allocation 的总容量，额外 allocation 必须因预算被拒绝。
- 每页只在一个假设为 64 字节的 cache line 上读写一个 `uint64_t`。`fixed` 使用页首，`rotate` 根据域、allocation 和页序号选择页内 line；实际 cache 几何没有验证。所有活跃租约的虚拟地址区间都会检查不相交，页内位置不会越过 allocation。初始化和访问均作用于自有 backing，不暴露地址。
- `grouped` 与 `interleaved` 每遍分别按域优先或按 allocation、页、域的顺序访问，保持相同域数、有效载荷、访问次数、初始内容和每个元素的写入顺序。它们不是多线程并发，也不是已经配对的性能实验。
- 阶段依次为分配和借用、初始化与首次触页、一次只读 warm、前半访问、对偶数序号域 begin-close 并释放其 owner、后半访问、关闭其余域及最终释放。关闭时验证新申请被拒绝、活跃 lease 令 collect 返回 BUSY、零等待 drain 不完成；后半访问仍使用先前取得的 lease。各域最后释放 lease，确认 CLOSED、backing 与 reservation 等为零后销毁。
- 每个值由域、allocation、页序号确定；逐遍读写与最终值分别用可计算的 checksum 检查。每个已执行阶段单独计时，阶段不重叠；checksum 和预热等成本不应解释成产品关键路径。30 秒仅是诊断接受门槛，在每遍之间和完成后检查，不承诺可中断阻塞的系统或 provider 调用。调用方如采用外部进程超时，必须将该次记为未完成。

## 宿主用法

```text
mgmem_tlb_multi_arena --arenas 3 --slots 4 --pages 7 --passes 5 --order interleaved --offset rotate
python bench/test_tlb_multi_arena.py --exe <diagnostic-executable> --runtime-dir <directory-containing-shared-core>
```

成功输出一条 JSON，包含输入参数、实际 OS 基础页、访问数、warm/访问/最终 checksum、各域关闭与预算检查、各阶段的 live/retiring/retained 和 backing 统计以及分阶段耗时。`touched_payload_bytes` 只表示每页实际访问的字节，不是分配容量。参数错误或操作失败返回非零状态及一条失败 JSON；测试脚本另外使用独立 Python 算术 oracle 比较两种顺序与 offset，并检查非法输入。进程级 watchdog 的超时属于未完成，而不是成功或性能剔除。

数据只是宿主功能证据。`unique_backing_bytes` 是本诊断中各独立域的逻辑 backing 之和；live、retiring 与 retained 是其互不重叠的子项，不能再加回 backing。各域快照依次采样，并非跨域原子快照；本单线程诊断在采样期间不改变各域资源。所有这些数值不包含元数据、allocator 额外开销，也不是 RSS/PSS；没有测量物理页连续性、TLB 组路、PMU、page walk、设备迁核、能量或净收益。即使所有 checksum 一致，也不能证明硬件机制、真实设备资格或 BIL 产品收益。特殊 placement、TLB profile 和 BIL 仍由各自证据门槛决定。
