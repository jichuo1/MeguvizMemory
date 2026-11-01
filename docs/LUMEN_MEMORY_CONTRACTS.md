# Lumen 内存与生命周期合同核实

2026-11-01，只读审计，不修改 Lumen 或任何生产依赖。当前可选方向是由宿主协调既有压力与观测接口；源码中没有可直接把 Memory CPU backing 注入 Lumen 软件工作区的公开合同。本记录不代表桥已运行、设备已验证或取得性能收益。

## 源码身份与范围

所查 Lumen 本地工作树 HEAD 为 `0eab3d6aeda19d0a810b89350d35038acbeb6ae7`，本地 `origin/main` 为 `55a150f6de321902987d38f62c967256abecb6f1`，工作树落后四次提交，另有未跟踪的会话目录；未联网刷新。下列文件在所查 HEAD 与本地 `origin/main` 之间无差异，文件本身按工作树原字节记录 SHA-256。路径均相对 Lumen 源码根目录；该身份不等于对远端最新版本、构建产物或运行时的认证。

| 相对源码路径 | SHA-256 |
|---|---|
| `lumen-engine/src/main/java/com/lumen/coacervation/engine/runtime/LumenMemoryPressureHub.kt` | `fb416be0444b9b5231ce8e41f1506c9ff863e886950ddb641dbb6aaa3766fecf` |
| `lumen-engine/src/main/java/com/lumen/coacervation/engine/LumenEngine.kt` | `aa9c5bb4ef6646473071fed078cd2b4b16b6a34f6742ca1bb982d45ef946ae59` |
| `lumen-engine/src/main/java/com/lumen/coacervation/engine/host/LumenSurfaceSession.kt` | `405265f44703febaad1eb03705a319c2cf118aecad8eb13718f60f4ae425aaf7` |
| `lumen-engine/src/main/java/com/lumen/coacervation/engine/host/LumenSurfaceOptions.kt` | `66efb485a51860f6651693dd2034feb2246b16318f18470c9c4a5e2cbcfda8c2` |
| `lumen-engine/src/main/java/com/lumen/coacervation/engine/host/LumenSurfacePerformance.kt` | `1c59fe4d55adf5c03f4442f7e3f5ea383b835c8bb2738a44e530219ae0c12f3e` |
| `lumen-engine/src/main/java/com/lumen/coacervation/engine/host/LumenMicaImage.kt` | `aa3007a10d2feb2a06d134a43cc34348c547361b6963dfa16a6dc1800659e71f` |
| `lumen-engine/src/main/java/com/lumen/coacervation/engine/material/LiveBackdropSampler.kt` | `92bfce69faf91a810377d7e4d5a2ccef1ac98af93c97b73706d2b34f683945d1` |
| `lumen-engine/src/main/java/com/lumen/coacervation/engine/material/ModernMaterialPolicy.kt` | `0f523c51f39aded04482de389a62ba651fb14e31eaa5d6e0419ad5ef0f42362b` |

## 公开压力接口与重复递送

- `LumenSurfaceSession` 是主线程对象，公开 `releaseGraphics()`、`resume()`、`performanceDiagnostics()` 和 `close()`。释放图形资源会使现有会话进入相应降级状态；显式恢复、内容或位置通知可触发其自身恢复路径，不能假定 Memory 预算恢复会自动恢复 Lumen。
- `LumenEngine.releaseGraphics()` 是公开的主线程全局入口；内部 `LumenMemoryPressureHub` 和监听器不是公开扩展点。宿主若选择协调器，应只持有自己有权操作的会话或使用明确授权的全局入口，在主线程派发，关闭后阻止迟到回调；不能让 Memory 的 worker 直接操作这些 UI 对象。
- 默认 `LumenSurfaceSessionOptions.registerMemoryCallbacks=true` 时，已绑定 session 自行注册 `ComponentCallbacks2`；Memory 的 `MemoryPressureAdapter` 也注册自身回调。宿主额外扇出时可能重复触发；可由宿主显式选择 session 的 `registerMemoryCallbacks=false` 并统一协调，但这不自动关闭 Activity delegate 或 `LumenEngine.releaseGraphics()` 的其他既有入口。必须按实际宿主枚举和去重，不能仅凭该选项声称全局只有一次。
- Lumen 当前 session 对 `TRIM_MEMORY_RUNNING_CRITICAL`（15）、`TRIM_MEMORY_COMPLETE`（80）及以上与 `onLowMemory` 释放图形；20（UI hidden）不是低内存。Memory 分类将 15–19 归为 critical，若要保持 Lumen 原行为，应保留原始事件级别而非仅使用归一化 critical 事件。新 Android 版本的实际事件递送能力仍需单独验证。
- `performanceDiagnostics().activeSoftwareBytes` 是 Lumen 自己的活跃软件缓冲逻辑预算，释放时可以先扣除，后台工作或显示列表仍可能保留旧对象。它不是 Memory 域 backing、RSS 或 GPU 总量；与 Memory 统计必须分栏，不能直接相加或解释为同步物理回收。Memory 的压力 worker、domain close/collect 不接管 Lumen Bitmap、HWUI 或 GPU retirement。

## 实际 CPU 工作区与所有权

`LiveBackdropSampler` 的内部 `Entry` 为软件采样持有 Bitmap、`IntArray` 像素、scratch 和输出，并按工作量预留软件预算；采样作业持有自身需要的引用，单飞和 generation/batch token 用于抑制迟到发布。释放或取消时仅丢弃引用，不强制 recycle 仍可能被后台或硬件显示列表使用的 Bitmap。对任务应用 Memory lease 时必须在任务入队前取得，贯穿最后一次后台访问；关闭与预算压力不得撤销已受理 lease，已发布图形仍遵守 Lumen 自己的生命周期。

`ModernBackdropBlur.blurInto` 是内部 CPU 路径，已复用调用方两块 `IntArray`，并以线程本地数组复用竖向累加器；它不是公开的外部 allocator 或可替换 native workspace 接口。`LumenMicaImage.prepare` 是公开的后台准备入口，借用输入软件 Bitmap 至调用返回，样本最大 96×96，文档给出的准备工作集上限为 144 KiB（不含输入）；其过程也使用内部数组和 Bitmap。

当前所查公开 API 不接受 Memory domain、allocation、lease、外部 CPU 指针或 `ByteBuffer` 作为上述工作区的 backing。Memory 的受控 Java 字节复制接口不使 Java `IntArray` 或 Bitmap 自动变成 Memory 拥有的存储；不得据此声称零复制、合并账本或真实 Lumen 集成。

## 后续选择与验证边界

如果真实场景需要联动，优先考虑由宿主显式启用的压力事件协调与分栏观测，不改变 Lumen 和 Memory 的默认依赖图；当前只是接口方向，尚无实现或运行结果。

如果研究软件采样或模糊的 CPU 工作区，先冻结真实 Lumen 源码和已复用的普通实现作为对照，完整计入 `getPixels`、`setPixels`、JNI、复制、初始化及关闭成本。必须检验相同输入输出、视觉像素、取消与在飞任务、压力、预算、关闭、fallback 和回收；不得把普通数组复用归为 Memory 新增收益。没有可证明的工作量与净收益前，不接入生产，也不推断 GPU、帧时或能耗收益。
