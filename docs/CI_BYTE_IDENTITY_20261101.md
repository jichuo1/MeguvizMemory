# 跨平台 CI 字节与宿主诊断

提交 `0382a07` 的[自动检查](https://github.com/jichuo1/MeguvizMemory/actions/runs/41272268881)未通过。Linux 的 Android 借用宿主桩启动器因子进程标准错误非空而失败；旧启动器没有输出原文，因此当时不能确认内容。该任务使用 JDK 25；native-access 警告是待后续对照确认的候选原因，而非已经证明的事实。

Windows 的预览包单元测试报桥源码哈希不匹配。同一任务中更早的历史 paired 归档检查也出现 raw hash mismatch，另一组历史测试在 setUp 时因锁定源码哈希不同而运行 0 项；原 PowerShell 多命令步骤只以最后一条命令的退出码结束，掩盖了前述失败。这些错误不能靠忽略 stderr、放宽原始哈希或将 0 项测试记为成功解决。

本地对已锁定源码和历史输入模拟 LF→CRLF 后得到不同 SHA；在 `core.autocrlf=true` 的过滤检查中，新增仓库属性使所查文件的检出过滤结果与 Git blob 字节相同。为保护整个源码和证据闭包，`.gitattributes` 明确禁用自动换行转换；没有执行仓库 renormalize，也没有改写历史文件。后续真实 Windows checkout 与每一项拆分检查的结果须以新 CI 为准。

宿主桩对 JDK 24+ 使用受限对照：先无选项运行并只接受完整已知 native-access 警告或空 stderr，未知错误仍失败；再显式启用 classpath native access 执行正式检查，正式轮要求 stderr 为空。失败诊断只记录固定类别和哈希，不公开捕获的路径或原始日志。JDK 17 不接收新选项。此兼容仅用于宿主测试，不是 Android ART、设备或生产路径资格。

已有预览包及其源码锁保留为当时的快照，不能声称包含本次 CI 修复；其中的库二进制没有因为这些测试脚本或 Git 属性修改而自动获得新的运行资格。

后续提交 `17aee94265c84cf0af00c0458383a950febbc4ae` 的[新 CI](https://github.com/jichuo1/MeguvizMemory/actions/runs/41273022264)在 Windows 和 Linux 均通过；历史归档审计、配对套件、预览包合同、宿主桩诊断及独立安装 Java 图像消费者分别通过。关键测试源码与当时源码锁的字节一致，见[脱敏记录](../artifacts/ci-portability-20261101/fresh-ci.json)。成功测试的控制轮输出未进入公开 CI 日志，因此先前 Linux stderr 的具体内容仍未知，不能倒推为已证实的 JDK 警告。
