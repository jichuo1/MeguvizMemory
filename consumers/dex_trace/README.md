# DEX 元数据读取轨迹：隔离诊断

本工具直接调用锁定的 DexMeguviz `read_metadata`，记录该 reader 实际执行的源码级字节读取，供诊断使用。它不修改共享 DexMeguviz 仓库；`prepare.py` 仅在本项目 `build/` 的新目录生成原样和插桩两份三个文件的副本。原始 `reader.cpp`、`reader.hpp`、`checked_leb128.hpp` 必须匹配内置 SHA-256 和长度；源码变化或读取位点数变化即拒绝准备，不尝试猜测适配。生成清单记录 31 个 reader 位点和一个 LEB 字节位点，并复查源文件。诊断程序直接使用 DEX 字节 vector，不经过 Memory 分配、DEX SDK snapshot 或查询接口，不代表这些消费者的端到端行为。

插桩版本在每次选定的 `bytes[index]` 成功取值后记录位点和相对偏移；`cursor++` 只求值一次，原本短路与边界检查保留。事件不包含字节值、原始指针或本机路径。只覆盖这三个冻结文件的选定读取语义：**不包括**输入加载或复制、SHA-256、其他 DEX 查询、指令解码、debug、CFG/dataflow、压缩、第三方库、分配器或操作系统。事件顺序属于插桩源码执行，非未插桩程序的机器指令顺序、硬件 cache/TLB 或性能结果。动态命中的位点数可少于静态的 32 个，不能将未命中位点声称已运行。

采集器用同一个 DEX 文件分别运行原样和插桩 reader，比较解析状态、错误位置或结构化 Facts 的确定性摘要，并前后核对输入、所请求程序与准备清单的字节；它根据固定哈希的原样源码重新生成插桩文本与位点清单后逐字节比较。事件数、位点和范围必须完整有效。原样程序不产生事件。位点解析失败、读取越界、事件超限、进程超时、输出或身份不一致都不能获得 PASS。畸形文件可得到与原样一致的 reader 拒绝，此时 PASS 仅表示该拒绝路径的采集比较完成，并不表示输入是有效 DEX。

Facts 摘要覆盖当前 reader 返回的字段、字符串、定义和索引目录，但不是正式 DEX 语义证明或生产查询等价证明。读取磁盘文件和记录其哈希不证明实际执行映像、加载模块、编译来源或输入在每个瞬间未被同权限进程篡改。采集插桩改变工作量，不得用于比较性能。原始 trace 的偏移、频率、输入哈希及错误位置可能敏感，完整输出只应保存在被忽略的本地 `build/`；公开结果应单独脱敏。

```text
python consumers/dex_trace/prepare.py --dex-source <冻结DexMeguviz源码> --out-dir build/<全新源码目录>
cmake -S consumers/dex_trace -B build/<全新构建目录> -DDEX_TRACE_COPY=<准备好的绝对目录>
cmake --build build/<全新构建目录> --config Release
python consumers/dex_trace/capture.py --input <本工作区的原始DEX> --baseline <原样程序> --instrumented <插桩程序> --manifest build/<源码目录>/manifest.json --out-dir build/<全新采集目录>
```

`--max-events` 默认 250000，上限 1000000；超过时整个采集为 INCOMPLETE，不静默截断。允许的 DEX 输入为 1 至 32 MiB，读取与摘要计算均在上限加一个检测字节处停止，即使文件在读取期间增长也不会无限读入；外部等待默认 60 秒、最高 300 秒。采集器并行读取两个输出管道，stdout 限 32 MiB、stderr 限 8 KiB；超限或超时终止自身启动的子进程，结果仍为 INCOMPLETE。它不负责结束子进程可能创建的后代，后代持有管道时也不能视为资源已经全部退出。该私有工具不自动重试、不清理或覆盖已有输出；失败后没有 PASS 回执。`test_contract.py` 是不依赖 DexMeguviz 的离线合同测试；真实 DEX 运行需自行提供冻结源码、编译器和输入，不能把合成 fixture 当成产品工作负载。

`smoke.py` 接受与 `capture.py` 相同的五个路径参数，在私有新目录内用所选有效 DEX 执行原样与插桩对照，另生成一个 magic 损坏副本验证两者拒绝一致，并检查事件上限失败不会写 PASS 回执。原始输入不会修改；该测试仍不提供性能或产品资格。
