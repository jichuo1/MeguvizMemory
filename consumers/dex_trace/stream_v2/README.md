# DEX reader V2 流式采集与回放

本独立诊断只覆盖 snapshot-open 调用线程内的元数据 reader 选定位点；不覆盖完整查询、其他线程、硬件访问或性能。它不修改 V1、普通 Dex 消费者、Memory 公共 API 或共享 Dex 源码。二进制帧和单个 worker 的合同分别见 [capture](capture/README.md) 与 [replay](replay/README.md)。

先用 V1 的隔离源码准备器及 [V2 生成器](capture/README.md)准备固定源码，分别建立 baseline 和 traced 可执行文件，再提供同一安装候选归档、其证据、解包 SDK 和核心库。调用者须明确提供事件、单文件和整个控制器持久化文件的字节预算：

```text
python consumers/dex_trace/stream_v2/controller.py --input <工作区内DEX> --baseline <baseline程序> --traced <traced程序> --core <安装SDK核心> --candidate <候选包> --candidate-evidence <候选摘要> --source-lock SOURCE_LOCK.json --dex-stage <Dex隔离目录> --paired-stage <V2源码目录> --sdk-prefix <安装SDK目录> --out-dir build/<全新目录> --max-events <每例事件数> --max-trace-bytes <每例字节数> --total-disk-bytes <全部持久化字节数>
```

外层先核验候选/SDK、隔离源码与构建配置；使用输入私有快照、四策略各五场景各自运行 baseline 与 traced，边读 stdout 边以固定缓冲写入私有文件。采集进程必须退出 0、stderr 为空；独立 parser 验证完整头、逐事件、终帧、消费者报告和 EOF，独立 oracle 与 worker 按 site/offset/byte 计算摘要并比较。输出、请求和 worker 日志在最终前重新哈希，输入、脚本和 SDK 也重新核对；私有回执包含每项请求及输出的摘要和长度、子进程退出码、工作区相对脚本名及文件摘要和请求解释器的文件摘要。只有 20 对全成功才能无覆盖发布回执。

单文件和总预算在持久化前检查，包含输入快照、baseline、trace、请求、worker 输出和最终回执；不包含文件系统元数据、OS 缓冲或子进程自行创建的临时文件。失败保留私有部分文件，不生成可信 PASS；单次阻塞系统调用无法被协作式 deadline 强制中断。最终回执发布后仍会检查截止时间，因此**必须同时取得控制器退出码 0 和完整 PASS 回执**；单独存在回执不能授予资格。终止本地子进程不证明其后代或 OS 资源已经释放。

D-C0/D-Z0 只清理本地引用，不代表 Memory 域或 OS 回收；D-C1/D-C2 的 worker 使用真实 System/Arena 和独立合成寿命，只有闭域、归零及销毁成功才继续。坏输入绑定实际被解析的首字节置零内容，lease/预算拒绝可有零事件；worker 的合成寿命不重现原始拒绝或跨线程回调。任何未知 native 状态保持隔离，不重复清理。子进程自报的模块路径与磁盘哈希、源码锁及请求解释器哈希均不是签名来源或映射字节证明。输入、trace、请求、日志和私有回执不可公开；采集与回放耗时不可作为 Memory 净收益。
