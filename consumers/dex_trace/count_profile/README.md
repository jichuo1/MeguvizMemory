# DEX reader 常量空间计数诊断

此独立 profile 通过相同的隔离 DexMeguviz 源码、安装 Memory SDK 和 `paired_v2` 工作负载，执行四种输入策略和正常、跨线程、无效 lease、借用预算、坏输入五类场景。它只在 `dmg_snapshot_open_borrowed` 调用线程内统计选定 31 个 reader 字节位点及一个 LEB 位点。计数器仅持有一个 64 位计数及 32 位位点掩码，不随读取事件数增长；Dex、输入、消费者和回调本身仍有各自内存开销。计数溢出、span/偏移/位点异常、作用域错误或输出失败均不能通过，不生成事件列表或可回放轨迹。

每个场景分别运行原样和 count-only 版本，核对既有完整消费者报告、查询 checksum、状态、retain/release、域预算和关闭账目；仅明确的无效 lease 和借用预算拒绝允许零计数。D-C0/D-Z0 没有 Memory domain，不能把其引用清理记为 Memory 域归零。统计是诊断子进程对选定源码位点的自报，不能证明完整 DEX 查询、跨线程生命周期、物理访问、TLB、产品性能或净收益；计数操作也改变工作量，不可用于性能对比。

## 构建与运行

先按 [配对诊断合同](../PAIRED_CONTRACT.md)准备并验证隔离 Dex 和 paired 源码、已回读的完整安装 SDK；以下参数由调用方替换。分别建立全新的构建目录，不能覆盖已有快照或制品。

```text
cmake -S consumers/dex_trace/count_profile -B build/<新baseline构建目录> -DCMAKE_PREFIX_PATH=<SDK前缀> -DDEX_TRACE_STAGE=<Dex准备目录绝对路径> -DPAIRED_TRACE_STAGE=<paired源码目录绝对路径> -DDEX_TRACE_MODE=baseline -DCMAKE_BUILD_TYPE=Release
cmake --build build/<新baseline构建目录> --config Release --target mgmem_dex_reader_count
cmake -S consumers/dex_trace/count_profile -B build/<新traced构建目录> -DCMAKE_PREFIX_PATH=<SDK前缀> -DDEX_TRACE_STAGE=<Dex准备目录绝对路径> -DPAIRED_TRACE_STAGE=<paired源码目录绝对路径> -DDEX_TRACE_MODE=traced -DCMAKE_BUILD_TYPE=Release
cmake --build build/<新traced构建目录> --config Release --target mgmem_dex_reader_count
python consumers/dex_trace/count_profile/run_count.py --input <工作区内DEX> --baseline <baseline程序> --traced <traced程序> --core <安装SDK共享核心> --candidate <候选归档> --candidate-evidence <对应候选摘要> --source-lock SOURCE_LOCK.json --dex-stage <Dex准备目录> --paired-stage <paired源码目录> --sdk-prefix <安装SDK前缀> --out-dir build/<全新输出目录>
```

运行器先核验原始文件、隔离源码、SDK 和 CMake 配置，将输入完整复制到私有新目录；分别运行 20 对独立进程，限额读取 stdout/stderr，并在后验重验输入、源码、构建配置、SDK 与每份输出的大小和 SHA-256。完整的 `count-receipt.json`、输入快照和原始进程输出留在 ignored `build/`，可能包含输入摘要或本机模块路径，不可直接公开。只有外层进程退出 0 且成功回执存在才是限定通过；时间上限是协作式边界，不保证中断单次系统调用或持久化。磁盘文件哈希、子进程自报的模块路径和源码锁不构成签名构建来源或映射字节证明。

如只需给某种策略估计容量，可显式传 `--variant D-C0 --case normal` 运行一个完整场景；不传选项时仍运行全部 20 对。单场景不减少该场景的输入、解析或查询工作量，回执和终端将明确标记 `EXPLICIT_SUBSET`，不能当作全部 20 对已完成。

宿主回归 `test_count.py` 检查报告边界；`host_stub` 中的 C++ 测试检查计数、位点、异常和 uint64 溢出。它们不使用设备或用户 DEX。
