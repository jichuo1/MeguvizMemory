# 2026-10-30 研究回包与实施证据

本批把研究建议转为独立消费者、离线审计和新协议草案。旧 arm64 实验仍为 **INCONCLUSIVE**，BIL 保持不接入。研究包内的提示词、任务和限制作为待审查材料，实际工作范围来自用户的“阅读内容并继续推进”及本会话已授权范围。

研究 ZIP：116,600 B，SHA256 `de1e57232d287ee86d2cca5287b0471b94838ba7bfce126e73ae0f32b8e05631`。原包私有保留，不把提示词、原始 APK/DEX 或机器路径上传公开仓库。`tools/audit_paired_archive.py` 只读取 JSON/ZIP，不执行包中代码；19 个清单成员逐文件验真。

## 原证据审计

复算 32 块/128 次任务：gain `0.010793899186631095`，探索性区间 `[-0.00023435730555698697, 0.02331589922167354]`，前后半 log-ratio 偏移 `-0.010603435100858594`。原始 stdout、输入、完整输出、顺序与关闭字段一致；旧数据、计划、报告、采集器及原分析器未改写。见 [复算收据](../artifacts/research-20261030/independent_recompute.json) 和 [身份清单](../artifacts/research-20261030/evidence_manifest.json)。

回包的 `formal.jsonl` 与 `formal-plan.json` 和本地原件逐字节相同。回包 `report.json` 被改成一空格缩进：14,256 B / SHA256 `c88ef1ba38311f5e5ffcd05dfbb3ad5e84c9b7576db64588b57f27d3c2e33f6f`；仓库原件为 20,413 B / SHA256 `40fe90483845bc74d31f4175dbb39ef2e129d687a562ee9d732832d9c51ad729`。两者 JSON 值完全相同；收据分别记录字节身份与语义一致，不宣称报告字节相同。

`precision_unmet=false` 只表示 pilot 所需块数未超 cap。按旧 bootstrap 重算的实际 log 区间半宽为 `0.011913172510177405`，高于 `0.01` 目标；本轮补记 planned/achieved 两栏，不修改旧算法或旧判定。32 块/128 任务也不证明有 128 个独立热环境窗口。

## 身份与资料可用性

| 项目 | 本批依据与边界 |
|---|---|
| 研究审阅基线 | Memory `764bfee5143b938e0e1b7f6329df56e9bbe3888c` |
| 历史 native 编译 / collector | 分别为 `5876530` / `601550c`；不改写为报告提交 |
| 历史 source lock | 从上述审阅基线保存原始 blob 到本批目录，保留根 HEAD `68fac01`、`dirty:true`；逐文件 hash 才是其输入身份 |
| 新 source lock | 根 `SOURCE_LOCK.json` 单独更新，表示本批 scoped 文件集合；不是新二进制的 clean commit 证明 |
| 冻结 Dex SDK | ChatGPT 通道未取得；本地冻结快照可用，按历史 lock 核对，不使用外部新 HEAD 推断 |
| 原 APK/DEX | 本地保有；新 host 功能资格重新完整消费。离线历史审计本身没有重新解析输入 |
| 设备 | 本批没有使用 ADB 或启动新设备采样；旧 4 KB arm64 功能证据继续有效于原制品和原用例 |
| BIL | main 只读核查仍为 `00572940c354fbec7b6f311d4ecd6f02e66ea7c5`，未修改外部工作树 |

## 本批六项交付入口

- R1：本文件、历史独立复算和范围化状态修正。
- R2：[`DEX_BORROW_CONTRACT.md`](DEX_BORROW_CONTRACT.md)，冻结 SDK 合同与文件身份。
- R3：`consumers/dex/paired_v2.cpp`，D-C0 / D-Z0 / D-C1 / D-C2，完整 owner 计时边界；普通去复制收益与 Memory 增量分开。
- R4：`tools/paired_analysis_v2.py` / `run_paired_device_v2.py`，离线层级计划与验证；见 [`PROTOCOL_FREEZE_CHECKLIST.md`](PROTOCOL_FREEZE_CHECKLIST.md)。
- R5：`consumers/image/paired.cpp`，full/ring × direct/System/arena，持久工作区强基线和逐字节 oracle。
- R6：[`CANDIDATE_EXIT_GATES.md`](CANDIDATE_EXIT_GATES.md)、[`BIL_CPU_CANDIDATES_00572940.md`](BIL_CPU_CANDIDATES_00572940.md)、[`MECHANISM_CLAIMS_20261030.md`](MECHANISM_CLAIMS_20261030.md)。

本批宿主收据证明功能、逻辑资源和清理范围；宿主 elapsed 仅作运行诊断，不证明真机速度、RSS、能量或机制收益。16 KB loader、MTE/HWASan、PMU/PFN、产品热点和正式发布分别保留资格状态。

最终本地检查为 DEX 32/32、图像 156/156、离线协议36/36，另有历史审计4个负fixture。协议检查包含33项合成用例和3项真实host native payload的只读资源映射检查；合成数据不作为测量。新 DEX 正式/诊断与图像程序的 arm64 交叉构建均通过，见 [构建收据](../artifacts/research-20261030/arm64_cross_build.json)，没有连接设备。公开 CI 增加离线审计、33项协议检查和156项图像 oracle；私有真实DEX/冻结SDK不自动下载或进入CI。

对象身份的 canonical JSON SHA256 与精确文件字节 SHA256 分开命名：新协议内部用 `protocol_canonical_sha256` / `plan_canonical_sha256`，制品清单和文件锁只使用文件字节哈希。
