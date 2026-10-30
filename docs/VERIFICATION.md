# 2026-10-30 工程验证回执

本批交付 CPU 核心、C/C++ API、安装包及两个真实可运行消费者。完整任务维度见 `TASKS.json`，实际源码和二进制哈希分别由 `SOURCE_LOCK.json`、`ARTIFACTS.json` 固定。

| 证据 | 实际结果 | 适用限制 |
|---|---|---|
| Windows x64 / MSVC 19.51.36247 / Release | CTest 4/4 PASS | core 11 组、145,033 check；纯 C、C++ RAII 5 case、两个独立 DLL 句柄隔离 |
| Windows x64 / MSVC AddressSanitizer | CTest 4/4 PASS | core、C、C++ 编译插桩；独立 runtime 测试验证身份，不将未插桩的辅助 DLL 算作完整 ASan 覆盖 |
| Windows x64 静态 SDK | 安装后图像消费 22/22 PASS | 独立 find_package 工程链接静态 mgmem.lib；没有依赖另一份 Memory DLL |
| GitHub Windows/Linux | 各 CTest 4/4、安装后图像 22/22 PASS | 代码提交 `58ed07f54199a1217ddef47735d4d297357654f4`；[构建 #40995571678](https://github.com/jichuo1/MeguvizMemory/actions/runs/40995571678)；没有冒充 CI 的 Dex SDK 验证 |
| 安装后的 DexMeguviz 消费者 | System/arena PASS | 真实 borrowed snapshot/query，完整消费两方法结果；与 copy-in oracle 相等；result 跨 snapshot/owner close 保活；最终两个库 borrowed/backing 归零 |
| 安装后的图像工作区 | 10 正例、12 错误路径 PASS | 五种尺寸×两模式；独立 Python luma/blur oracle 逐字节相等；截断/溢出/错误 header/不可写输出均失败退出 |
| Android NDK 30 / Clang 21 / API 27 基线 | x86_64、arm64-v8a、armeabi-v7a 构建安装成功 | 未削减 32 位；全部使用 static C++，实际 `libmgmem.so` 仅依赖系统 libm/libdl/libc |
| 最终 Android ELF | 三 ABI 的所有 PT_LOAD 对齐均 0x4000 | 静态 ELF 条件通过，不表示真实 16 KB loader 或 APK/AAR 打包通过 |
| API 34 x86_64 / 实际 4096 B 页模拟器 | core 11 组、C ABI、C++ RAII 实际运行 PASS | 独立 `/data/local/tmp` 目录真实动态装载；未运行 BIL/LSPosed |
| owned-region diagnostic | Windows 48/48、Android 模拟器单 case checksum PASS | HOST_DIAGNOSTIC；不作为性能/TLB 机制资格 |
| 研究模型重算 | 三个 calculator 与原 JSON 语义相同 | MODEL 算术复现，未测设备收益 |

真机只读核到了 API 37、arm64、4096 B 页、6.12 Android 内核，另一会话正在做 GPU 工作，本批未在该真机执行 Memory。没有可用的真实 16 KB 环境；MTE/HWASan/CheckJNI、arm64/armv7 实机、产品端到端延迟、能量和 BIL 集成都未验收。

## 实际复现入口

宿主 CMake/build/install 步骤见 README 与 INTEGRATION。当前机器的实际目录为 `build/host-ninja`、`build/host-asan`、`install/host`、`build/dex-locked`、`build/image`。Python 运行既有解释器，没有安装 Python 依赖：

```powershell
$env:PATH = 'D:/MeguvizMemory/install/host/bin;' + $env:PATH
python tools/verify_consumers.py --image-exe build/image/mgmem_image.exe --dex-exe build/dex-locked/mgmem_dex_consumer.exe --dex-fixture build/locked-inputs/DexMeguviz/tests/fixtures/generated/minimal.dex --out-dir artifacts/verification-20261030/consumers
install/host/bin/mgmem_tlb_owned.exe --matrix
```

Android 使用现有 NDK `30.0.16248370` 的 `build/cmake/android.toolchain.cmake`，`ANDROID_PLATFORM=android-27`、`ANDROID_STL=c++_static`、各自 ABI 与独立二进制/安装目录。先构建安装，再运行 `llvm-readelf -l -d install/android-<abi>/lib/libmgmem.so`。模拟器明确设置 `LD_LIBRARY_PATH` 为仅含本批库的临时目录，传入同批 core/C/C++ 测试 ELF；raw 结果保存为 `artifacts/verification-20261030/android-api34-*`。

测试覆盖真实风险：并发 reservation/close 竞争、provider OOM、异常不越过 C ABI、精确地址/容量/对齐归还、旧数据失败原子、读写互斥、跨域/重复释放、晚完成 worker、reset/trim 拒绝活对象、增长共存峰值、阻塞 deallocate 时 drain 真超时、双 runtime cookie 隔离。最终 destroy 与全部 API 仍由外层串行化，裸 domain 指针不提供销毁后安全。

## 经验与失败记录

1. 初次 Visual Studio 生成器未找到编译器。原因是当前终端未初始化 MSVC；使用可覆盖 VS 根目录的 `tools/host-build.cmd` 初始化现有工具链，Ninja 构建成功，没有安装新工具。
2. 首轮 ASan 测试启动超时。原因是直接运行 CTest 时缺少 MSVC ASan runtime DLL 搜索路径；给当前进程 PATH 加入编译器 Hostx64/x64 目录后 4/4 通过。保留这个运行环境前提，不把该超时隐去或当核心通过。
3. 审查发现 drain 曾同步执行自定义 provider 释放，因此 0ms 可能阻塞。最终方案拆出 cleanup worker 的 collect，drain 只等待；阻塞回调的确定性 CV 测试已通过。
4. 审查发现独立 core 模块的局部 domain ID 可相同。最终 handle 增加装载期 runtime_origin 路由 cookie，两个实际 DLL 的交叉释放返回 STALE_HANDLE；runtime 卸载后句柄仍禁止复用。
5. 观察到 arena 为小输入保留更多容量。保留原始结果，默认 System 仍是有效基线，不选择特殊布局，不将调用次数变化写成时长/RSS 收益。
6. Android 最终重传后测试 ELF 的执行位被重置，首次返回 Permission denied；给本批独立目录的测试文件恢复执行位后重新运行全部 core/C/C++，并核验设备上 libmgmem.so 的 SHA256 等于最终安装制品。该错误没有被计为通过。
7. 外部 Dex 会话在最终核对后更新 CMake/任务元数据。没有将漂移静默记为哈希通过；最终复制独立配置输入和源码快照，重新构建 parser 与安装消费者并重跑 24 条 oracle 命令。旧回执保留于前一证据提交，最终 SOURCE_LOCK、ARTIFACTS 与回放输入 ZIP 对应冻结版本。

本批没有修改外部 BIL/Dex/Compute/Media 源码或依赖版本，也没有正式发布 Release。公开仓库用于保存工程预览及进度，CPU 正式候选、家族集成和 TLB profile 各自仍需对应门槛。
