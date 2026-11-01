# 功能开发证据范围（2026-11-01）

`host-receipt-v2.json` 是本阶段宿主检查及本地工程构建的说明视图，补充标记三份原始 AAR 含环境标识、不可分享。`host-receipt.json` 为先前记录，保留但不覆盖；其 AAR 哈希只标识本地原件，不代表可分发。

`android-sanitized-v2/` 和 `windows-sanitized/`、`bridge-sanitized/` 记录独立脱敏副本的范围。Android 原始二进制和未脱敏中间文件均仅在忽略的构建目录内；本目录不收录它们。Windows 证明中的本地执行脚本含机器路径，ZIP 装配及部分补充扫描没有完整保存脚本，相关结果仅作为所记录的本机证据。

`android-borrow-r8-build.json` 只证明对指定 arm64 制品完成 R8 构建及静态检查，不证明已在 Android/ART 设备运行。宿主检查、二进制比较、设备、性能、正式发布均是不同资格；本阶段不接入 BIL。
