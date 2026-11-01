# Android 托管域独立长稳诊断协议

此工具只面向独立测试包 `io.github.jichuo1.meguviz.memory.stabilityprobe`，在预启动的同进程前台 Activity 中重复执行 System/Arena 的预算、合成压力、跨关闭 lease 和最终域账目检查。它不接入 BIL，也不证明真实系统低内存、性能、全进程无泄漏、24 小时稳定性或单个长寿命域的行为。该协议和运行器有离线检查，设备兼容性、设备实际运行和长稳资格均须另行记录。

## 冻结与准入

先运行 `python tools/test_android_managed_stability_round.py`，它仅使用合成数据和 fake 环境，不连接设备。用独立构建器生成诊断 APK 及构建回执，执行 `python tools/android_managed_stability_round.py freeze --help` 并显式提供选定设备、API、ABI、页大小、全新随机 16 位小写十六进制 run ID、APK、AAR、核心库、构建回执和工具路径。`freeze` 不调用 ADB；它检查签名、清单、原生库身份、构建日志与 R8 证据，输出只保存在 ignored `build/` 的私有计划和独立计划哈希。计划和原始日志不得发布，源文件与工具身份被冻结不单独证明构建来源。

运行前先协调共享设备窗口，检查相关会话，再把这一时点的观察结果保存为 `build/` 内私有 JSON：字段必须且仅能为 `state`（`CLEAR_OBSERVATIONAL_ONLY`）、`device_serial_sha256`（所选 serial 的 UTF-8 SHA-256 小写十六进制）、`observed_at_utc`（含时区）、`visible_sessions_checked`（仅实际核查后可为 true）和 `shared_device_lock`（false）。它须在执行前 120 秒内生成；它不是跨会话锁，不能代替检查。执行前保留冻结步骤输出的原始哈希，调用 `run --help` 中的全部参数，输出目录与原始日志目录均须是 `build/` 中互不包含、从未存在的独立目录；不要修改计划后重算哈希。

运行器先核验全部本地输入和私有准入记录，再用显式 serial 查询设备。它在安装前检查所有可见用户的同包状态和签名，异用户存在同包时不更新共享代码；安装前以及启动 instrumentation 前分别检查已知测试进程和活动 instrumentation，监测时也检查其他已知测试。清单解析失败或身份不明即中止。进程和会话检查只是离散观察，可能有不可见任务，不构成排他锁。

## 前台窗口与中止

通过 `am start` 启动本诊断 Activity 后，核验它已 resumed、进程 PID/UID 与启动身份，再以 `--no-restart` 在同进程启动 instrumentation。Activity 设置了 `FLAG_KEEP_SCREEN_ON`。诊断自身仅在已确认不存在活动域时，向已验证的自身 Activity 发出有时限的定向 finish 请求；请求调用完成不证明 Activity 已销毁。运行器不会发送全局返回键、强制停止进程或改变系统冻结策略。测试发生异常或窗口退场无法确认时，窗口仍可能残留并持续亮屏。回执与终端输出的 `foreground_window_state` 明示已观察到的前台状态，未知表示窗口可能残留，不能宣称设备已释放。用户或后续受明确授权的操作应先检查窗口与进程，再由用户自行关闭；不可对在途或状态未知的域强制清理。

运行期间分别读取独立 progress/stall、同进程身份、前台状态与自身 cgroup。`stall` 一旦存在始终优先；缺失、解析不完整、冻结、身份改变、其他测试出现或到达外部截止时间均不会作为 PASS。当已确认该进程发布同一 run 的 complete 阶段后，它主动结束前台窗口时允许有限时间交接到终态报告，但仍必须通过完整的报告、采样覆盖和后验。发生异常时最多终止本地 ADB transport，远端状态保持 UNKNOWN；绝不清数据、卸载或自动重试。ADB 超时并不证明远端操作终止。

## 判定及证据范围

PASS 需要唯一有效报告、正常 transport、设备端记录实际工作负载至少 900 秒、前台确认、至少两次离散自身 cgroup 采样、首尾与最坏相邻采样间隔均不超过 30 秒，且所有采样显示未冻结。每次采样记录读取区间，最坏间隔使用后一读取区间结束到前一读取区间开始的差值；前台、进程与 cgroup 分别在不同时间采样，均不是连续观测。终态 progress 必须与 instrumentation 报告一致，且无 stall。还须核对设备启动身份、用户、安装包字节及其他测试后验；同一进程仍在时还须确认该 Activity 已不是 resumed，若仍 resumed 则 INCOMPLETE。未 resumed 不证明 Activity 已销毁或全部设备资源已释放。FAIL 和 INCOMPLETE 单列，不从超时、无样本或进程退出推断通过。

设备端只检查被测域的最终账目，未据此证明整个进程内存归零或无泄漏；即便完成 900 秒重复建域/关闭，也不构成 24 小时或单域长寿命资格。私有输出保留本机路径与设备观察，公开时只能另做脱敏摘要，不上传原始设备身份、ADB 日志、测试签名或计划。
