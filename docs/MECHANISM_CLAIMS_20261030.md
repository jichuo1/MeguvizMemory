# 机制主张与可反驳条件

本批先减少可观察工作，再判断 Memory 的净增量。标签 SOURCE_FACT、OFFICIAL_MECHANISM、MODEL、HYPOTHESIS、LOCAL_MEASURED、UNKNOWN 分别使用；机制解释和产品资格分开。

| 主张 | 证据等级 | 可实施的干预 / 反驳条件 |
|---|---|---|
| 当前 DEX 复制对照有两份输入存储 | SOURCE_FACT：旧 `paired.cpp` 预读后再复制 | 新 D-Z0 直接借用同一稳定 source；必须完整消费、保活到最后 result 和 callback。若合同或输出不等价则失败 |
| 去复制少逻辑读写 21,080,368 B | MODEL：2 × 10,540,184 B | 不是实测 DRAM 流量、fault、RSS、能量或 elapsed；普通消费者收益不能归给 Memory |
| 三行环缩小内部 luma | SOURCE_FACT + MODEL：`5WH` → `4WH+min(H,3)W` | 先 direct System 比布局，再固定布局比较 provider；边界和两级整数舍入任一字节不同即失败 |
| arena 可减少多对象批次申请 | HYPOTHESIS | 需要真实批次与静默 reset；强 A1 含持久 workspace。若 A1 稳态已零申请而 Memory 多出管理成本，则退出 |
| mutex/map/list 是热点 | UNKNOWN；存在这些结构是 SOURCE_FACT | 需要实际 profile 份额、争用和端到端消融。暂不做 slab、锁重写或新生产后端 |
| VA 密集等于 PA 连续或 TLB 聚合 | 不成立的推断 | 用户 allocator 不控制内核页表；无对应证据时 PA/聚合 UNKNOWN，不按芯片名选择策略 |
| 少 minor fault 等于少 TLB miss | 不成立的推断 | 缺页、翻译缓存和数据缓存是不同观测。诊断 phase 也只对 API 完成边界归因 |
| 端点频率可归一化整个任务 | 不成立的推断 | `scaling_cur_freq` 常是最近请求值；保留端点元数据，不据此除 elapsed |

Linux 6.12 说明 mTHP 可以使用多个 base page 的块且仍以 PTE 映射；部分架构在 VA/PA 连续且正确对齐时能压缩翻译。这是正式机制说明，不能证明本设备或当前 backing 满足条件。THP 也会带来清零和 fault 延迟取舍。[Linux THP/mTHP](https://docs.kernel.org/6.12/admin-guide/mm/transhuge.html)

PFN 暴露有权限限制，缺少权限时字段可归零；没有 PFN 不能将零值或 VA 邻接解释成物理地址证据。[Linux pagemap](https://docs.kernel.org/6.12/admin-guide/mm/pagemap.html)

频率属性受驱动和硬件影响，端点不描述任务内的 DVFS 轨迹。电池温度和 thermal status 也不替代核心温度。[Linux CPUFreq](https://docs.kernel.org/6.12/admin-guide/pm/cpufreq.html)

AOSP 的 Scudo 默认安排有设备例外；LLVM Scudo 自有缓存和管理层。不能假定每次 malloc 都触发内核映射，也不能仅凭 API 版本确定目标进程配置。[AOSP Scudo](https://source.android.com/docs/security/test/scudo)、[LLVM Scudo](https://llvm.org/docs/ScudoHardenedAllocator.html)

以上网页于 2026-10-30 重新读取。研究包引用的 Arm 101811/0106 translation-granule 与 contiguous-bit 页面本次工具访问失败，保留为包内来源，未声称本次重新取得。8EE6 原视频/厂商参数仍未核实，不自动把 PMA110 或 CPU2 识别成相关微架构。

旧 arm64 的 1.0794% 为 LOCAL_MEASURED 记录上的描述估计，趋势触发且实际探索性 log 半宽超过目标；新机制、新计时边界、新构建均另立身份，不补样、不混池、不按有利方向选统计方法。
