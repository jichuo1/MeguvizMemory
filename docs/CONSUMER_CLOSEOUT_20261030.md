# 消费者阶段收尾与未获资格项

2026-10-30。按用户“快速收尾”要求结束本阶段。交付core 0.2.0 / ABI1.1及pressure closefix1工程修订，**Android新模块长稳未获资格，BIL不接入**。没有新增生产依赖或发布正式Release。

## 已完成的实际验证

DEX独立消费者使用SYSTEM完整region导入：明确runtime cookie、READ lease、origin retain/release、SDK结果跨snapshot/caller/owner关闭延寿、跨线程最后归还。普通direct与import均无额外input backing/复制，source+input唯一容量都是10,540,184B；imported charge是source的子集，不相加、不把普通删除复制的收益归给Memory。Host12功能矩阵、两组短循环、时长门槛和非法参数通过。

图像真实P6→整数灰度→三行环blur，每帧逐字节匹配独立Python标量oracle，验证压力降预算0/新准入拒绝、restore、BUSY延后trim、持lease关闭超时、CLOSING后异步完成及最终账目归零。三行环和既有scratch属于普通优化。

| 首个冻结窗口的原生profile | 循环时长（图像为active loop） | 完成量 |
|---|---:|---:|
| Windows DEX import | 900.222s | 3551 cycles complete 54,147-method queries |
| Windows image System | 900.039s | 5671 cycles / 22684 frames |
| Windows image arena | 900.133s | 5629 cycles / 22516 frames |
| API33 arm64 DEX import | 900.268s | 2632 cycles complete 54,147-method queries |
| API33 arm64 image arena | 900.010s | 39694 cycles / 158776 frames |

该窗口原生结果独立完整通过；父arm64收据的Android子项失败不抹去这些结果，也不使它们自动具备Android适配器长稳或性能资格。Host新消费者ASan短验通过。核心加可选JNI宿主回归的最终Release/ASan各18/18通过；17项公开金样离线变造回归通过，不计为新设备证据。

## 已复现并修复的JNI锁问题

旧代码在Bridge锁内调用Java/VM操作及foreign lifetime.release。8组原/新对照证明可重入阻塞：foreign release真实重入nativeBind的旧进程3秒超时、新代码返回BUSY；重入snapshot、并发单publisher、Attach/Java失败重试、错误隔离等亦通过。修复将JNI/Java和外部释放回调移到锁外，单publisher claim保证快照/global refs寿命及一次发布，保留无需Java再poll的自动收尾。

永久回归直接编译生产pressure_jni.cpp，使用真实Memory/pressure及JDK头的fake VM；不链接JVM，不替代ART设备证据。当前生产JNI SHA为fc439c597f3d8b6c45ddacfdd135c52332ee8261a2816151a2ea7603c7a5c96c。原/候选适配、换行和最终源身份分别记录，不能把适配副本当原始字节。

## Android、R8与未获资格

修复后的两种ABI真实R8短测通过(API33 arm64、独立API34 x86_64模拟器)。R8实际缩减/优化/混淆，mapping/usage/DEX和运行marker匹配；JNI完成通知名由AAR原规则保留。arm64使用closefix1 AAR，x86使用新core/JNI派生AAR，同一shared core闭包；未跑Gradle/AGP或BIL产品依赖图。

首个API33 Android子项1260秒外层超时，没有有效根因栈。最初测试中的UiAutomation整入口和runOnMainSync没有完整时限，故新版增加自身cache阶段记录、Handler有界派发、总体watchdog，AMS观察独立放在早期smoke。主任务已RUNNING的确定性注入设备3.127秒记录UNKNOWN、宿主3.812秒完成捕获并只隔离自身进程，禁止并发finally free；这不是正常CLOSED_ZERO。

**closefix1新API33长轮仍停在iteration48 / cycle.idempotent-close，进度elapsed_ms=4803，之后多次读取不变；确切卡点/根因仍未定位。** 不得宣布上述锁修复解决了此挂起。新API34窗口仍有进度，但按用户快速收尾要求在900秒前停止；两项均未获长稳资格。旧模块API34的28546循环/900.026秒独立保留，不转授新模块。

只停止本项目独立R8诊断进程，未清数据、卸载或向原PMA110 GPU测试/原API34模拟器的Lumen测试发送停止命令。收尾检查时两个模拟器均未出现在ADB，原因未核实；本轮没有执行停止模拟器命令。所有部分结果、timeout、测试见证和验证器修正均保留；原始长输出仅本地，公共收据记录hash和检查后的摘要。无后续排队测试。

下一阶段首要项为定位API33重复等待(含ART线程附着/分离与Java完成通知边界)，取得有效线程栈或可反驳复现后再改；Java自有完成线程只是候选，未实现。重新冻结独立Android长轮，不能补记本轮通过。16KB真实loader、MTE/HWASan、单长期域/24h、同完成量RSS/能量、性能、家族桥与正式Release仍分别缺失。

自身RSS/PSS/private-dirty只是全进程、非原子观测，不与domain backing相加，也不是内存节省或泄漏自由结论。本轮时长只用于有限稳定性，不做性能比值；普通优化和Memory增量分别记账。

主清单：[ARTIFACTS](../artifacts/continuation-20261030/ARTIFACTS.json)。首轮及closefix轮的采前plan、精确旧源码、失败/主动结束收据均在同目录。当前源码锁为SOURCE_LOCK.json(该目录内)，按源字节建立身份，不冒充交付commit时重新编译。
