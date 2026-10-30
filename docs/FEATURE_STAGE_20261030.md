# 0.2 功能阶段收尾

本阶段交付 **native SDK 0.2.0 / ABI 1.1 工程预览**，独立packed reader模块0.1.0，以及Android压力AAR 0.2.0。三项优先功能已实现并取得下列限定范围的功能证据；**不接入BIL，不声明新性能、能量或正式发布资格**。

| 功能 | 本阶段实现 | 实际证据 |
|---|---|---|
| packed reader | 真实flat格式primitive读取；copy/immutable借用、presence、范围、独立payload/位图预算、关闭与原拒绝/回退规则 | Java32；真实Kotlin oracle48 fixtures×6 policy=288差分；Android ART32 |
| 外部内存导入 | SYSTEM域整region唯一owner；同runtime跨domain的identity/范围拒重；显式runtime cookie、READ权限、锁外retain/release、软预算、lease跨关闭 | Host Release/ASan联合10项CTest；新设备原生9项含正常导入7组及3个违约隔离子进程；原ABI1.0头编译客户端通过 |
| 压力与观测 | native cleanup worker、quiet-only trim、BUSY延后、显式restore；真实ComponentCallbacks2注册/注销、JNI opaque ID、自有域异步关闭和独立lease token；自身RSS/PSS/private dirty | Java38（另真实android.jar编译）；Android设备20检查，Application主动分发与AMS针对自身包注入递送分别通过 |

设备为先前用户明确授权的新设备2210132C，API33、arm64、4096B页，执行前重新查验无其他测试/仪器占用。本阶段不运行性能采样或制造整机OOM，也没有终止原设备另一个会话的GPU测试。

## 生命周期和预算

外部导入只管理origin-owner已提供的CPU region，不调用System free释放它；最后owner/lease结束后才归还origin引用。重复ID/重叠区域拒绝，不建立可绕过读写互斥的别名owner。跨runtime恶意重包装、不同虚拟地址指向同一物理存储及页保护不作自动安全保证；具体前置条件见[导入合同](EXTERNAL_IMPORT.md)。

软预算可降到0或低于现有charge，限制后续准入；已发布存储和已有reservation仍有效。压力回调不运行provider free；清理worker只在quiet时回收，持lease时延后。自有Android域close后lease仍可读；await超时不强制释放，归还lease并等待其C调用返回后才真正destroy。外部注册只关闭控制器，不擅自关闭foreign domain。

违约retain/release/provider-alias的3个隔离测试验证poison及不误释放旧地址后退出，**不算正常资源归零**；正常路径完整collect/destroy。Java copy/borrow不等于native Memory lease，GC何时回收不作承诺。Imported backing是原snapshot backing的子集，同一外部存储不与调用者source容量重复相加。

## Android真实观测与事件边界

本轮自身进程报告RSS91924kB、PSS16857kB、private-dirty10200kB，来源和时间与域逻辑账目分开；它们不是单域开销、同一原子采样或内存节省。能源为null并有无合格方法原因，缺失不写0。

AMS注入只针对本项目 `io.github.jichuo1.meguviz.memory.pressureprobe`，验证真实系统递送到注册callback；它仍是合成信号，不是实际整机低内存事件。API34起多数旧运行中压力级别及onLowMemory不再递送，自动通知不能保证；background/显式request与restore接口保留。[Android ComponentCallbacks2](https://developer.android.com/reference/android/content/ComponentCallbacks2)

首次设备段已通过native9和ART32，诊断APK也成功安装；验证器的Windows盘符脱敏误伤`package:/`、路径正则未接收CRLF，造成安装后检查中止。原收据和验证器精确源码已封存。只修正验证器并继续尚未执行的Android段，未重跑native/ART。继续段验证同证书才更新APK，未卸载或清数据，最终20项与backing=0通过。

## 家族和交付范围

[家族合同核实](FAMILY_MEMORY_CONTRACTS.md)确认实际SDK存在，但Compute host descriptor无外部CPU指针/provider入口；content lifetime不等于allocator注入。反向CPU READ map adapter仍是需要完整范围、稳定SDK及生命周期测试的候选，本阶段未实现或运行家族桥。BIL及其现有scratch/holder/staging保持原样，相关收益不重复计功。

新增Windows x64与Android arm64 SDK，原0.1制品和证据保留；其他ABI本阶段未重新构建。Android AAR包含同一shared Memory runtime及既有LLVM notice。packed生产JAR不包含Kotlin、oracle或测试类。本阶段未取得16KB/MTE/HWASan、能源、BIL host parseFrom/Hook/UI、Lumen/family桥、R8或正式Release资格。

主清单为 `artifacts/features-20261030/ARTIFACTS.json`；packed原始收据在 `artifacts/functional-next-20261030/packed/receipt.json`，设备初段与Android继续段各自保留source/hash关联，不把初段BLOCKED标签当成原生失败。原24个MM任务依赖未改，父任务未因局部功能通过标为全部DONE。
