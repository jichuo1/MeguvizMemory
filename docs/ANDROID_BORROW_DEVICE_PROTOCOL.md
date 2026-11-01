# NativeMemoryLease 独立设备诊断协议

本协议用于后续的功能验证，不操作 BIL、其他应用的数据或系统冻结策略。诊断 APK 使用测试专用包 `io.github.jichuo1.meguviz.memory.borrowprobe`，执行真实 Java、JNI 和单一共享 Memory core；它不代表产品集成、性能、能耗或长期稳定性资格。

## 冻结与输入

用 `python tools/android_borrow_round.py freeze --help` 查看必填参数。冻结步骤只检查本地输入，不调用 ADB。必须显式给出目标序列、预期 API/页大小、APK、对应 AAR、安装 SDK 中的 core、构建回执、ADB 和本地签名/包检查工具；禁止让工具自动选择设备。APK、AAR、core、JNI、fixture、测试源码、诊断工具及工具链均按字节哈希，APK 包名、唯一 instrumentation 的名称及目标包、单一签名和三个原生库闭包会在冻结前检查。冻结文件只保存序列 SHA-256，不保存原序列。

冻结后从标准输出单独保存 `plan_sha256`；执行 `run --help` 所列命令时明确传入同一计划及**预先保存**的 `--plan-sha256`。运行前重新核验计划字节、目标序列哈希、所有输入及本地工具哈希；修改后重新计算出的哈希不能被冒称为原先的冻结值。源码和制品哈希用于身份绑定，本身不单独证明制品由该源码构建。

测试当前采用后台 instrumentation 直接启动，不带前台预启动或 `--no-restart`；不把旧前台长轮资格转授给它。用户已告知某测试手机装有冻结模块，这属于用户提供的设备环境信息；是否在本轮发生冻结仍以本轮对自身 cgroup 的实际观察为准。

## 准入、运行与后验

执行前和安装前分别观察 `ps -A` 与 `dumpsys activity`；要求清单可解析，且没有 Meguviz、DexMeguviz、DexDash、相关测试进程或活动 instrumentation。检查只是时点观察，不提供共享设备锁，其他会话仍可能随后申请设备。设备 API、ABI、页面大小和启动标识须符合计划。

工具只读枚举 Android 用户及各用户可见的同包安装/卸载残留。若当前用户已有同包，必须先拉取该 APK 并核对签名，只有相同签名才允许更新；如果其他用户也存在同包，则禁止安装或更新共享代码（当前用户已安装且字节完全一致时才可以不更新）。用户/包清单不完整、卸载残留、包路径不明确或无法核对签名等状态按阻塞处理；这些系统清单仍不是共享锁，也不保证暴露所有系统隐藏状态。安装命令在发出前记录 `REQUEST_IN_FLIGHT_OUTCOME_UNKNOWN`；超时或未能回读字节时，安装结果保持未知，不能自动卸载、清数据或重复覆盖。安装后与后验均回读该包 APK，并和冻结字节一致才可通过。

测试期间只观察本包 PID、UID、启动时刻与对应 cgroup；采样中的 `freeze=1` 或 `frozen=1`、超时及进程身份变化均记录为 **INCOMPLETE**。测试很短时可能没有任何 cgroup 样本；完整功能报告仍可单独记录，但冻结观察写 `NOT_OBSERVED`，不等于证明全程没有冻结。有样本也只是离散观察。采样跨越超时边界后才观察到 transport 退出，仍按超时未完成处理。

超时或冻结后，仅当本轮已观察到的 PID、UID、启动时刻仍一致，且新一轮清单未发现其他相关进程或活动 instrumentation 冲突时，才请求停止**本包**；仍无共享锁，不保证别的会话不会在检查后进入。身份或占用不明时不对设备 force-stop，只结束本地 transport 并将远端状态记为 UNKNOWN。异常时不会假设远程安装或进程已结束。

完整 PASS 还需恰好一条 JSON 报告、唯一且在报告后的 `INSTRUMENTATION_CODE: 0`、transport 退出码为 0、29 项检查、READ/WRITE 两组、关闭后 lease 访问及最终域 backing 为 0，并通过设备后验、同一 boot 和已安装 APK 字节回读。明确的测试 FAIL 与环境 INCOMPLETE 分开；正常报告已经通过而后验失败时，报告结果保留，但整轮不会标为完成。若 instrumentation 已结束而本包进程仍驻留，也不会为了制造“空闲”而强停它。

原始命令输出、拉回的 APK 和诊断签名材料只放新建的 ignored `build/` 目录，不进入公开回执。公开回执只含固定状态、计数及哈希，采用 fsync 和有界原子替换；共享冲突后保留上一次完整文件和 pending，不覆盖旧证据。可用 `python tools/test_android_borrow_round.py` 执行纯本机模拟测试；该测试不会调用 ADB，也不是设备结果。
