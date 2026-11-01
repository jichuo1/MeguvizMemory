# DEX reader V2 流式记录器

这是独立诊断构建，不更改 V1 目标、轨迹格式或既有回执。`prepare_stream_paired.py` 只接受固定字节身份的 `consumers/dex/paired_v2.cpp`，在新建的私有 `build/` 目录生成单独源码；`verify_prepared` 会重新生成并核对源码、输入组件和成员集合。Dex 源码仍须由已有 `prepare_consumer.py` 从固定锁生成 baseline/traced 两份隔离副本；其专用 LEB 命名空间不改变其他翻译单元。

V2 的 traced 程序仅在 `dmg_snapshot_open_borrowed` 的调用线程记录已选元数据 reader 字节读取，不覆盖 SHA、查询、decoder、其他线程、机器指令或硬件访问。`corrupt-input` 的 header 摘要对应首字节置零后的实际输入；未受理的 lease/预算路径可有零个事件。诊断插桩、写入和预分配都会改变工作量，不可用其耗时或普通复制/借用差异宣称 Memory 净收益。

## 严格字节协议

仅 traced 模式在 stdout 写 V2 二进制；Windows 在任何输出前将 stdout 设为 binary。所有帧、尾部模块行和消费者 JSON 均由同一个 `std::cout` 顺序写入，避免两个缓冲流重排。baseline 保持原有的小文本结果。所有整数以小端编码，不依赖 C++ 结构体布局：

1. 88 字节 header：ASCII `MDXTRC2\n`（8）、实际输入长度 `u64`（8）、事件上限 `u64`（8）、实际输入 SHA-256 的 64 字节小写 ASCII。
2. 每次记录：字节 `0x01`、位点 `u16`、输入相对偏移 `u32`，共 7 字节。输入最多 32 MiB，位点只能为 1–31 或 1001；偏移必须小于 header 中的输入长度。
3. 成功结束帧：字节 `0x02`、已记录次数 `u64`，共 9 字节。之后恰好一行 `MGMEM_CORE_MODULE_HEX <小写十六进制>\n` 和一行消费者 JSON、然后 EOF。模块行是诊断进程自报的私有数据，不证明已映射字节。

单进程使用固定 64 KiB 缓冲，不按事件上限预留向量；默认最多 250000 条，调用方可显式设置 `MGMEM_DEX_TRACE_MAX_EVENTS` 为 1–64000000，不会自动因剩余磁盘空间调高。达到上限、非法 span/位点/偏移、作用域错误或写入失败会保守记录首个固定失败类别，不生成合格结果；尝试数在上限加一处饱和，仅可解释为下界。stderr 的固定诊断是子进程自报，丢失或异常时须视为通用 INCOMPLETE。

与 V1 一样，插桩 helper 在原始字节读取成功后才调用观察函数；记录器的偏移检查只能校验到达回调的事件，不能阻止已发生的越界读取，也不能替代解析器的边界检查或地址检测工具。

结束帧写入前会先刷新事件；若结束帧本身或其后的最终报告、flush、close 失败，已写出的部分不能撤回。因此**结束帧本身不证明成功**：受控采集器必须等待进程退出码 0，完整验证 header、事件、终帧计数、两条尾行、EOF、输入/制品身份及相关原样对照，才能签发限定回执。父进程还需并发排空 stdout/stderr，按固定内存与磁盘预算写入仅供本机的独占文件；超限、截断、超时或任何不确定输出均为 INCOMPLETE。终止本地进程不证明其后代已退出或所有资源正常回收。

## 本机准备与构建

```text
python consumers/dex_trace/stream_v2/capture/prepare_stream_paired.py --paired-source consumers/dex/paired_v2.cpp --out-dir build/<新paired阶段目录>
cmake -S consumers/dex_trace/stream_v2/capture/paired -B build/<新构建目录> -DCMAKE_PREFIX_PATH=<安装SDK前缀> -DDEX_TRACE_STAGE=<已验证Dex隔离目录绝对路径> -DPAIRED_TRACE_STAGE=<已验证V2阶段目录绝对路径> -DDEX_TRACE_MODE=traced -DCMAKE_BUILD_TYPE=Release
cmake --build build/<新构建目录> --config Release --target mgmem_dex_reader_stream_v2
```

baseline 应使用不同构建目录和 `-DDEX_TRACE_MODE=baseline`；调用方须分别校验两个构建输入、实际 SDK 库与输出身份。单独执行该程序得到的字节流不是完整受控采集或性能资格。原始输入、二进制流、模块路径和日志只应留在被忽略的本机目录。
