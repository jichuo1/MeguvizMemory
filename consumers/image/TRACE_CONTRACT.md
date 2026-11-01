# P6 图像消费者的源码级访问诊断合同

该可选诊断在 `consumers/image/paired.cpp` 的实际 P6→灰度→clamp 3×3 模糊路径编译期插桩，独立于原有不插桩 target。普通目标的预处理后不含采集调用或热循环条件；两者运行所得时间不可混用。采集操作限定在现有自有 `direct` System、Memory `system` 和 `arena` 三个 workspace，既有 lease、owner、close 和 worker 同步合同不变。

诊断记录的是经插桩版本的**源码语义操作**，不是实际 CPU 指令、cache/TLB/PMU、物理页或 BIL 产品热点。RGB 三通道读在插桩版本明确顺序化；编译器可用不同机器指令实现未插桩表达式，不能将此事件顺序称为未插桩机器访问顺序。`memcpy` 只报告一次 `COPY_IN` 目标范围，`OUTPUT_IO_READ` 只报告提交给文件输出的范围；源 vector、libc 内部、文件系统、allocator、元数据和 stack 等未逐字节采集。仅一个 worker 访问图像，主线程通过 admission/future 与 join 协调，不能外推为一般并发访问模型。

## 采集与限额

诊断 target 需显式提供 `--trace-dir <尚不存在的目录>`；目录必须能由当前进程创建，输出 `events.txt` 与诊断 PGM 均独占创建、不覆盖已有文件或跟随已存在链接。原有非插桩消费者的输出行为不变。`--trace-max-events` 默认 250000、允许 1 到 64000000；输出文件上限为 2 GiB，内部 I/O 缓冲固定 64 KiB，其他 workspace 与平台缓冲开销另计。两个上限和磁盘容量均不会降低原图像大小、帧数或算法工作量：超限、失败、写入或关闭错误都使采集进程非零并报告 `trace_status=INCOMPLETE`。遇 I/O 错误时部分文件可能没有结束行，不能把存在文件当成成功。1080p 的逐像素访问可能产生超过 3000 万条事件，调用方应显式提高事件限额并预留大约 GiB 级磁盘空间及足够的外部超时；默认限额不是 1080p 完整轨迹资格。

原始 `events.txt` 不包含图像像素值、原始指针、输入/输出路径或密钥；其中的图像尺寸、RGB payload 摘要、内存相对访问顺序及累计输出 checksum 仍可能是敏感信息，应按调用方数据处理。普通 PGM 图像输出包含像素，不属于脱敏 trace，不应随证据公开。采集器对实际读入并由 worker 使用的 RGB vector 计算 SHA-256，供 runner 与其独立解析结果比对；它绑定已解码 payload 和尺寸，不单独认证原 P6 header 的原始编码。文件不能单独证明采集进程成功：即使结尾写有 `COMPLETE`，footer/close 后续错误仍可能发生。受控 runner 必须在采集前复制或冻结合法 P6 输入，分别记录完整输入、RGB payload、trace 和输出的 SHA-256，验证采集进程退出码为 0、JSON `success=true` 与 `trace_status=COMPLETE`，并在执行前后重验身份；回放入口必须校验该受控记录、实际消费字节与文件哈希。裸 trace 只能解析，不能授予输入身份或采集成功资格。受控工具对 P6 文件总字节设置 `3 × 8 Mi + 1 MiB` 上限；原消费者允许更长注释，超出该安全限额的输入会被工具明确拒绝。

`MGMEM_IMAGE_TRACE_TESTING` 仅在测试 executable 可显式注入输出失败；生产诊断 executable 不提供测试参数。故障注入是宿主受控反例，不代表已复现真实磁盘故障。

runner 把用户指定的采集可执行文件和 Memory core 按所请求的磁盘路径在运行前后哈希；没有读取实际执行映像、loader 复用模块的身份或已映射字节。Python 回放脚本也只记录请求路径的前后哈希，未认证解释器或实际加载的字节码。receipt 只是对所选进程输出和文件身份的本地记录，不是签名的来源证明；采集 binary 与本仓库源码的对应关系需要另外的构建证据。direct 模式的回放只使用普通自有缓冲，不加载或调用 Memory core；它是单线程逻辑重建，并非原采集过程系统调用的真实再现。system/arena 回放请求使用给定共享核心，但实际载入模块身份仍标记为未验证。回放失败时区分已确认清理、direct 引用清除和未知/隔离；不重试可能已消费的对象或把未知状态记为归零。

runner 对采集及回放分别启动独立诊断子进程；等待超时只终止本次子进程并记为不完整。回放子进程超时或输出超限时，资源清理状态为 `UNKNOWN_PROCESS_TERMINATED`，不能据此声称正常归零。进程结束、文件 I/O 与操作系统调度仍不承诺硬实时；runner 自身对输入哈希与独立 oracle 使用协作期限，单次系统调用不能被内部计时检查中断。

runner 的 `capture-receipt.json` 状态 `CAPTURE_PROCESS_AND_IDENTITY_CONFIRMED` 只表示所选进程自报成功、退出码及输入/输出身份等前置检查符合合同；它明确写入 `event_structure=NOT_YET_VALIDATED_REPLAY_REQUIRED`，不表示事件已经逐项验证。只有独立回放完整比对全部事件、实际消费字节和独立 oracle 后产生的 `replay-receipt.json`，才能标记限定的 `PASS_BOUND_TRACE_FUNCTIONAL_REPLAY`。本地 receipt 可被本地调用者修改，没有签名或防伪能力；对同权限进程的任意篡改、二进制实际映像以及 PPM header 内容不作不可变或认证保证。

```text
python consumers/image/trace_runner.py --capture-exe <插桩构建可执行文件> --core <匹配的共享核心> --runtime-dir <采集程序共享库目录> --input <合法P6图片> --input-origin USER_SUPPLIED --out-dir build/<新目录> --provider arena --layout ring --frames 2 --close-race --max-events <事件上限> --timeout-seconds <秒数>
```

`direct` 回放无需 `--core`；采集程序本身的动态加载闭包仍由调用方提供。输出目录包含 P6 快照、PGM、原始 trace 和本机 stdout/stderr，属于私有本机材料，不应整体上传；公开时只发布经审阅脱敏的范围、哈希和结果。

## 事件格式

文件为 ASCII、LF，固定头 `MGMEM_IMAGE_TRACE_V1`；下一行为：

```text
H <width> <height> <frames> <direct|system|arena> <full|ring> <close-race:0|1> <event-limit>
I <实际读入RGB payload的64位小写SHA-256>
```

每行事件为 `E <从1开始连续序号> <M|W> <操作码> <allocation索引或255> <相对偏移> <长度>`；`M` 是主线程，`W` 是单个 worker。`0/1/2` 分别是 RGB、luma、output workspace，`255` 表示事件不绑定 allocation。无符号十进制字段和相对区间必须检查溢出及边界；固定操作码如下：

- Memory 模式：`DOMAIN`、`ALLOC`、`ACQUIRE`、`ACQUIRE_BUSY`、`RELEASE_LEASE`、`RELEASE_OWNER`、`BEGIN_CLOSE`、`COLLECT_BUSY`、`DRAIN_TIMEOUT`、`COLLECT_OK`、`DRAIN_OK`、`DESTROY`。`ACQUIRE_BUSY` 是实际全 allocation 排斥验证，不表示成功取得 lease。
- direct System 模式：`DIRECT_ALLOC`、`DIRECT_READY`、`DIRECT_CLOSE_DEFERRED`、`DIRECT_FREE`；没有 Memory domain、lease 或伪造的 close 调用。`DIRECT_CLOSE_DEFERRED` 表示原消费者保留 owner 到 worker join，随后释放。
- 同步：`ENQUEUE`、`ADMIT`、`WORKER_START`、`WORKER_END`、`JOIN` 的 offset 表示从 0 开始的 frame 序号。worker 的访问先于其 lease release，close-race 在最后一帧 admission 前完成。
- 工作区访问：`COPY_IN` 写 RGB 全部范围；`RGB_READ`、`LUMA_WRITE`、`LUMA_READ`、`OUTPUT_WRITE`、`OUTPUT_READ` 均为一个字节；`OUTPUT_IO_READ` 是最后一帧输出的整体范围。写事件不包含实际写入值。

完整结束行为 `END COMPLETE <写入事件数> <尝试事件数> <累计checksum> <灰度转换数> <完成帧数>`，两种事件计数必须相等。未完成时为 `END INCOMPLETE ...` 或缺失结束行；任何意外操作码、乱序、重复、越界、截断、额外数据、类型不匹配和错误生命周期都应拒绝，不能作为完整采集。

`test_trace_capture.py` 用确定性 P6 生成数据验证 direct/system/arena × full/ring、双帧 close-race 与不插桩输出/独立像素 oracle 一致，另验证超限、受控写故障及既有目录；这是合成输入下真实图像算法的宿主功能测试，不是生产 trace。实际输入是否来自真实业务应另行记载并经授权，不能由文件格式或算法名称推断。
