# 工具链运行时说明

Android 原生包使用现有 NDK 30.0.16248370 的静态 libc++/libc++abi 运行时。`llvm-NOTICE.txt` 原样来自该 NDK `toolchains/llvm/prebuilt/windows-x86_64/NOTICE`，随 Android 安装包放入 `share/MeguvizMemory/licenses`。这不是新增第三方 allocator 或生产后端。

Memory core 不包含 DexMeguviz/miniz 源码或二进制。Dex 独立消费者在显式指定的外部工程内使用其既有依赖；本批只分发 Memory 原生 SDK，不分发外部 parser 闭包。
