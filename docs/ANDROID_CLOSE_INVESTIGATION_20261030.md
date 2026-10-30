# API33 关闭停滞调查与自动通知修复

2026-10-30。最终去debug路径安装版已完成API33 arm64真机15分钟采样长轮；API34 x86_64独立模拟器只通过短测。BIL保持未接入。

## 本机复现与冻结证据

先重新核对用户授权的API33 arm64手机：未发现其他测试进程或活动instrumentation；这是观测门，不具备跨会话共享锁。另一个API37设备的GPU测试仍活跃，本轮未操作。

使用上一阶段同一签名、逐字节核验的closefix1诊断APK再次运行，进度在第46轮附近停止。最末`native.snapshot`只是包含阶段写入的边界，不能据此断定JNI函数内部死锁。两次对自身PID的只读采样间隔约7秒，均见`cgroup.freeze=1`、`cgroup.events frozen 1`，且20/20线程处于`do_freezer_trap`；两次记录见[freezer证据](../artifacts/android-close-investigation-20261030/baseline-freezer.json)。Linux内核文档将该等待函数描述为将任务置于冻结状态；Android有使用cgroup冻结缓存进程的机制。[Linux内核说明](https://kernel.org/doc/html/next/driver-api/basics.html)、[Android cached apps freezer](https://source.android.com/docs/core/perf/cached-apps-freezer)。本次直接阻塞条件是进程被冻结；触发者、OEM策略及此前每次是否同因仍未知。

非root LLDB对自有debuggable PID尝试时，服务附加未能取得栈；初版辅助脚本因`ps`截断可执行路径误认为服务退出，实际仍有tracer。随后按该服务完整`/proc/.../cmdline`、目标PID和随机socket核实身份，仅终止这个自有服务，并确认目标tracer归零。原始记录与栈尝试日志仅本地，[脱敏摘要](../artifacts/android-close-investigation-20261030/baseline-lldb-summary.json)保留失败和人工纠正。新版helper按完整cmdline、UID、starttime和全部线程的tracer验证；离线11项回归通过，未把它称为本机已抓到线程栈。

## 前台与后台对照

[首次前台尝试](../artifacts/android-close-investigation-20261030/freezer-control-foreground/receipt.json)在instrumentation启动Activity、尚未resumed时观察到冻结，结果为INCOMPLETE；下面取得通过的路径使用shell预启动窗口和no-restart组合，不能把差异归因于单一变量。

新的同一诊断APK：[后台](../artifacts/android-close-investigation-20261030/freezer-bootstrap-background/receipt.json)被观察到自身cgroup冻结，中止为INCOMPLETE；[前台](../artifacts/android-close-investigation-20261030/freezer-bootstrap-foreground/receipt.json)先由shell启动受`android.permission.DUMP`保护的诊断Activity，再用`--no-restart`在同一PID运行，完成1000周期、62.846秒，24次采样均未观察冻结，最大间隔3.578秒，Activity正常finish。后台和前台的启动/重启方式不同，这是对本设备可行路径的功能对照，不是单变量性能或一般设备因果证明。

Activity仅属于诊断APK，调用方需持有系统DUMP权限，该权限可授予满足平台签名、特权或开发条件的调用方；不声称只有shell能调用。窗口只在自身生命周期内显示测试提示、保持亮屏，不改全局电源或冻结配置。每轮检查持续resumed、结束时确认本次onStop/onDestroy，未知执行不得与普通资源清理并发。

## 独立JNI自动完成缺陷

生产旧JNI在后台reaper首次Attach失败后无自动重试；真实native pressure/controller加fake VM的[有界反例](../artifacts/android-close-investigation-20261030/retry-host/red-receipt.json)证明VM恢复后、没有调用者poll时注册及两个global refs仍保留。修复复用已有reaper，以25ms到1秒退避持续重试，直到正常或错误状态被单次发布；并发poll仍可先完成，外部域owner不变、未知资源不强制free。

第一修订版（20863源码）Host Release/ASan各22项通过，其中4项覆盖附着恢复无poll、Java异常恢复、并发poll单次提交与错误隔离。[宿主收据](../artifacts/android-close-investigation-20261030/retry-host/summary.json)。API33真机修复版[短测](../artifacts/android-close-investigation-20261030/retry-smoke/receipt.json)完成92周期/6.058秒，预算、跨关闭lease、正常归零和调用者不poll的注册自动移除通过；没有在真实ART注入附着失败。VM永久不可用时每个受影响注册保留引用和一个低频重试线程，其开销不计入domain预算，JNI DSO动态卸载仍不支持。此独立缺陷不声称是本次系统冻结的原因。

## 当前长稳与边界

源SHA `20863c7d646b46f0d7f526aa5e064f877c530f0869ae20f484b54daa690ba3bd` 的首个修复版在API33前台完成15,048周期/901.086秒、225,879检查；343次采样无冻结、最大间隔4.219秒，窗口正常结束、domain backing归零。见[冻结计划](../artifacts/android-close-investigation-20261030/retry-long-plan.json)与[回执](../artifacts/android-close-investigation-20261030/retry-long/receipt.json)。随后发现的JNI错误分支及去debug路径的安装制品不同于此轮二进制，不继承逐字节资格。仅观察自身PID的cgroup.freeze/events，采样为0不等于连续证明未冻结；发现1、采样间隔超过10秒、前台暂停、功能/关闭错误或超时即不合格。没有系统OOM、性能配对、能源或RSS节省声明。

API34独立模拟器的同版R8短测完成78周期/8.031秒，调用者不poll的注册移除和每域最终backing=0通过；受控progressLock故障注入的16个Java线程栈只证明隔离工具，不是API33根因。见[短测](../artifacts/android-close-investigation-20261030/api34-retry-smoke-resume2/receipt.json)和[受控栈摘要](../artifacts/android-close-investigation-20261030/api34-retry-guard-resume2/stack-summary.json)。

## JNI 错误分支与最终制品

随后以 fake JNI 和真实 Host pressure/core 构造七组旧失败/新通过反例：创建/绑定期间12种 pending JNI 异常必须立即停止不允许的后续JNI调用、仅释放已创建引用并保留原异常；reaper压力快照报错、状态不符或域快照头无效，均隔离controller/owner并发布错误，不销毁domain或误报成功。自有与外部域分别覆盖。未知快照列0–12输出-1，错误/owned/done仍可识别；Java能区分未知快照与无符号溢出。异常fixture只在已确认隔离且reaper退出后受控清理，不能把违约/未知路径称正常归零。Host Release/ASan各29项通过，[故障注入证据](../artifacts/android-close-investigation-20261030/jni-errors-host/summary.json)。

调试信息中的本机路径由独立复制品去除：Android ELF逐项核对PT_LOAD（仅屏蔽ELF section table元数据）及动态符号，Windows PE/导入库原字节保留、静态库非debug section和relocation核对并通过独立消费者。原先在真机运行的文件未被覆盖或修改。最终AAR/API1.1与新JNI/Java由独立构建收据绑定；[发行版短测](../artifacts/android-close-investigation-20261030/distribution-smoke/receipt.json)在API33完成88周期/6.030秒，调用者不poll的自动收尾、未知原因区分和最终域归零通过。新的[发行版长稳计划](../artifacts/android-close-investigation-20261030/distribution-long-retry-plan.json)和[回执](../artifacts/android-close-investigation-20261030/distribution-long-retry/receipt.json)已通过：14959周期/901.089秒、224546检查，366次自身采样未见冻结、最大间隔3.391秒；前台Activity正常结束、正常域backing归零，调用者不poll的注册移除与未知原因检查通过。该结果来自最终实际安装的脱敏APK；旧APK长稳不转授新APK。

发行版首轮最后一次记录进度约870秒，随后Windows拒绝收据原子替换并返回WinError 5；具体拒绝原因未知，该轮未完成且未混入重试。仅本项目诊断进程结束；原始报错含机器路径的字节保留本地，公开回执作路径脱敏并保留原SHA。后续增加0.9秒有界原子重试及stdout进度、重新冻结独立计划；不得把首轮补记为成功。

独立[安装回读](../artifacts/android-close-investigation-20261030/distribution-postflight.json)核对了同一设备和启动周期、安装APK及证书、AAR中的原生库成员；测试前后观测门通过，诊断窗口已不处于 resumed。合成文件上的[Windows真实共享冲突回归](../artifacts/android-close-investigation-20261030/atomic-windows-os-sharing.json)证明短暂冲突可重试成功，持续冲突会保留旧文件和待写文件；它不能识别首轮的具体拒绝原因。

API34独立模拟器对最终脱敏 x86_64 APK 的[短测](../artifacts/android-close-investigation-20261030/api34-distribution-smoke/receipt.json)通过32周期、8.138秒、495检查；原因字符串、no-poll自动移除和域归零通过。三个自身cgroup样本为0，不构成连续或API34长稳证明。

核心ABI仍为1.1；真实16KB loader、MTE/HWASan、同完成量能量/RSS收益、家族桥、BIL产品路径、24小时/单长期域和正式Release仍未取得资格。系统可冻结后台进程，应用和JNI都不能在被冻结期间运行；本轮前台窗口只用于诊断，不修改或关闭设备全局冻结策略。
