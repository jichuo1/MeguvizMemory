# MeguvizMemory

显式 CPU 内存域、System provider、有界 arena 和可验证的所有权/借用合同。当前版本为 **0.1.0 工程预览**，公共原生接口使用 `mgmem_*_v1` / ABI 1.0。独立于 BIL、Xposed、GPU 后端及 Android UI；不接管进程全局 allocator。

提供 CMake 安装包、C ABI 与可选 C++17 move-only RAII 适配。没有新增第三方生产依赖。

本批交付范围与证据见 [STATE.md](STATE.md)、[验证记录](docs/VERIFICATION.md) 和 [TASKS.json](TASKS.json)。功能验证、设备运行、性能收益、TLB 机制和发布资格分别记录；研究模型中的 3.55% 等数值不代表本库实测。

已完成 API 37、arm64、4 KB 真机的核心/C/C++ 安全用例；独立真实 DEX A1/A3 比较完成 32 块、128 次任务，性能为 INCONCLUSIVE，当前不接入 BIL。点估计与探索性区间不构成稳定收益，详见 [性能报告](docs/PERFORMANCE_A1_A3.md) 和 [接入决定](docs/BIL_ADOPTION_DECISION.md)。

## 构建和安装

```sh
cmake -S . -B build/core -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PWD/install/host"
cmake --build build/core --config Release --parallel 2
ctest --test-dir build/core -C Release --output-on-failure
cmake --install build/core --config Release
```

Windows 使用已初始化的 Visual Studio C++ 开发环境。当前机器也可用 `tools/host-build.cmd` 包装上述 CMake 参数；`MGMEM_VS_ROOT` 可覆盖 Visual Studio 路径。地址检测构建用 `-DMGMEM_SANITIZE=ON`，MSVC 运行时须能找到其 `clang_rt.asan_dynamic-x86_64.dll`。`BUILD_SHARED_LIBS=OFF` 提供 PIC 静态核心，家族组合仍须遵守单一最终 runtime 合同。

消费者独立配置，只使用安装导出目标：

```cmake
find_package(MeguvizMemory CONFIG REQUIRED)
target_link_libraries(my_consumer PRIVATE MeguvizMemory::mgmem)
```

```sh
cmake -S consumers/image -B build/image -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$PWD/install/host"
cmake --build build/image --config Release
# P6 RGB → 整数灰度 → clamp 边缘 3×3 box blur → P5 PGM
build/image/mgmem_image input.ppm output.pgm
# System 对照使用 --system；两种模式工作量与输出相同。
```

Windows 运行消费者前将 `install/host/bin` 加入当前进程 PATH；Unix 按安装位置配置动态库搜索路径。完整 DexMeguviz 消费者需要真实外部源码和 DEX 输入，见 [集成说明](docs/INTEGRATION.md)，未捆绑或伪造 Dex SDK。

## 所有权与关闭

`allocate` 返回 owner handle；访问前获取读/写 lease。读 lease 可共享，写 lease 排斥其他 lease。owner release 后已有 lease 保活 backing，最后一次访问结束再归还 lease。异步任务必须先取得 lease 再入队。

```text
OPEN → begin_close → CLOSING
  → 完成并 release 全部 owner/lease
  → cleanup worker 调用 collect（同步 provider 释放）
  → CLOSED → drain 等待确认 → destroy
```

`begin_close` 不等待任务、不运行 provider 回调；`drain(0)` 只轮询，超时不强制释放。`collect`、`trim` 及最后一次 System release 可能同步调用 provider，需安排在允许阻塞的线程。C++ RAII 的最后一个 wrapper 同样负责同步 cleanup。裸指针、裸 domain 指针及运行库卸载都有外部寿命要求，完整合同见 [ADR](docs/ADR-001-ownership-and-abi.md) 和头文件。

预算约束唯一 provider backing 与 pending reservation，不包含 allocator/容器元数据或 RSS/PSS。arena 的 retained 含 reset 前不能复用的已释放空间，不能把它直接称为 RSS 节省。默认 dense/System；slab、page hint、TLB placement、JNI 和 GPU 桥均未启用。

本仓库保留实现与可审查进度；没有发布正式 Release 或宣称 BIL 性能获益。原始研究附件留在本地 `research/`，公开文档仅记录其 hash、合同摘要及当前实现状态。
