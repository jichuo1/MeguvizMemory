# 本批实验与资源结果

日期：2026-10-30。范围：宿主功能、资源记账与安全诊断，**不是产品性能资格**。协议见 `bench/protocol.json`，原始命令/输入/输出/hash 见 `artifacts/verification-20261030/consumers/receipt.json`。

两种消费者都从 `install/host` 导入同一 Memory DLL，分别执行 System 和 arena，工作量与输出保持一致。Dex 自身由真实外部源码构建；Memory 没有复制 parser 实现。当前输入是可再生文件格式 fixture，不能升级为 BIL 真实 APK 流量验收。

| 工作负载 | System backing 峰值 | arena backing 峰值 | System→arena provider 成功次数 | 结果 |
|---|---:|---:|---:|---|
| Dex minimal.dex，448 B，2 方法 | 448 B | 65,536 B | 1→1 | copy-in oracle 相等；关闭归零 |
| 图像 17×13，221 pixels | 1,105 B | 65,536 B | 3→1 | Python oracle 逐字节相等；关闭归零 |
| 图像 320×180，57,600 pixels | 288,000 B | 303,872 B | 3→3 | Python oracle 逐字节相等；关闭归零 |

该消费者显式选择 64 KiB chunk，用于验证通用 chunk/lease 合同；它没有对这些小输入取得 backing 优势。减少 provider 调用不等于加速。320×180 的 arena backing 比 System 多 15,872 B（约 5.51%），该回归如实保留；它不是 RSS/PSS 回归，也不是冻结产品资格预算后得出的启用结论。没有据此切换 BIL 或启用特殊 placement。

独立 owned-region harness 在 Windows 跑了 48 个 case，全部 checksum 相等；API 34 x86_64 模拟器另跑单个受限 case。每条 JSONL 保存工作量、页大小、stride、页内 offset、seed、访问模式和分段计时。诊断轮存在构建并发和后台负载，CPU/频率/迁核/温度未隔离，不对这些时间计算收益、置信区间或 p95/p99。不同访问链的依赖程度本身不同，不能把 dependent/independent 的差值归因为某种 TLB 机制。

PMU/PFN、物理连续性、TLB 几何及聚合状态均未取得对应证据；时间不等于 page-walk 计数。hint、slab 和特殊 placement 均未选择。A0…A4 产品版本未形成同 workload 配对数据，所有产品收益、能量、FPS 和场景稀释字段保持未知。

BIL 的 P1-07 scratch 与 P2-05 holder 打包已经存在于所核 main，归现有 A1，不重复计功。研究三份 calculator 可重现其原 JSON，仅证明条件算术，不填 LOCAL_MEASURED。
