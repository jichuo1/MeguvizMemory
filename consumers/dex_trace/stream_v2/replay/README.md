# MDXTRC2 流式解析与回放 worker

这是独立诊断组件，不修改 V1 协议或生产库。它只处理调用方已绑定的 `snapshot-open` 元数据 reader 字节事件，不能据此认定完整 DEX 查询、其他线程访问、硬件访问顺序、产品性能或 BIL 集成。

输入文件采用固定 88 字节头：`MDXTRC2\n`、小端 `u64` 输入长度、`u64` 事件上限、64 字节小写 ASCII 的实际消费输入 SHA-256；每条事件是 `01 + u16(site) + u32(offset)`，终帧是 `02 + u64(count)`，之后恰有一条模块身份行、一条消费者 JSON 行和 EOF。事件数上限为 64,000,000，输入上限为 32 MiB。坏输入场景对首字节置零后的实际消费字节绑定 SHA；invalid-lease 和 borrow-budget 只允许零事件，其余场景必须有事件。

`stream_parser.scan_file()` 使用固定缓冲流读取，不生成完整事件列表；校验头、每个 tag/site/offset、计数、终帧、有限长度尾行、固定消费者状态、文件摘要与路径身份。core 文件以不超过 64 MiB 的 64 KiB 分块方式核验。打开前、读取后检查常规文件、链接/重解析点、硬链接数和文件身份；这些检查不是对同权限进程或映射字节的签名证明。`deadline` 是可选单调时钟绝对截止，按事件块及身份分块检查，不能中断正在执行的系统调用。`consumer` 返回值是私有的子进程自报 JSON，可能含输入标识和时间字段，不应直接用于公开摘要。

`stream_worker.py` 必须由受控外层启动，单独运行的 worker 结果不能证明真实采集进程已成功退出、输入或文件来源已被可信绑定。外层应先确认候选 SDK、core、脚本与输入身份，实际启动采集进程，核对退出码及完整文件哈希，再向 worker 发送 `build/` 下独占私有 JSON 请求；每个 worker 也应由外层设置超时及输出限额。worker 本身会先完整流式验证一次，在自己的 backing 上再次流式读取并计算 `SHA256(pack('<IQB', site, offset, byte))`；两遍 trace、core 身份和消费者报告必须一致。外层应以独立 oracle 计算同一摘要并核对 worker 成功输出及 core 摘要。

请求必须且仅含以下字段：`schema_version=2`、`input_path`、`trace_path`、`variant`、`case`、`input_sha256`、`input_bytes`、`consumed_sha256`、`trace_sha256`、`trace_bytes`、`event_limit`、`event_count`、`core_path`、`max_trace_bytes`。路径须为 `build/` 内已存在的受限常规文件；整数不接受布尔值。`max_trace_bytes` 是外层预先确定的单文件上限，外层还须控制所有 trace、输入快照及其他输出的累计磁盘配额。`python -E -s -S .../stream_worker.py --request <私有请求> --timeout-seconds <秒>` 可在受控环境中调用，输入和事件、原始子进程输出与请求不得公开。

`D-C0` 是普通私有副本，`D-Z0` 借用本进程预载缓冲，两者没有 Memory domain，引用清除不证明 OS 回收。`D-C1` 和 `D-C2` 使用选定的真实 Memory System/Arena、64 字节对齐、32 MiB backing 预算，写 lease 初始化后换读 lease，依次 release owner、begin-close、归还 lease、collect/drain、核对 CLOSED/zero 并 destroy。Python 输入、副本、解释器和 trace 元数据不计入 domain backing 预算。单次调用异常或返回值未知时不重试可能已消费的句柄，不强制清理；子进程退出也不证明系统资源已回收。零事件拒绝场景的合成 worker 不重现原始拒绝逻辑或跨线程生命周期。

成功输出仅有一条 JSON，`state=PASS_STREAM_V2_READER_BYTE_WORKER`，并包含变体、场景、事件/位点数量、trace 与 core 摘要、回放摘要及清理状态；它只是子结果。外层必须确认 worker 的退出码为零、标准错误为空、所有绑定及独立 oracle 一致，且完成自身后验后才可发布最终成功回执。任何截断、超额、输入漂移、输出错误或状态未知均为 INCOMPLETE，不做性能比较。
