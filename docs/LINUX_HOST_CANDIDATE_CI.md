# Linux 宿主工程候选的隔离 CI

`.github/workflows/host-linux-candidate.yml` 是手动触发的独立任务；调用者必须填写仓库内完整的小写 40 位 commit，并选择恰好指向该 commit 的分支或标签。请求的 commit 必须与 workflow 的 `GITHUB_SHA` 一致，避免 workflow 与构建源码来自不同版本；分支移动时任务会失败。它使用 GitHub-hosted Linux x86_64、只授予 `contents: read`，检出时不保留凭据，不操作设备或发布正式 Release。目标 commit 必须包含本 workflow 使用的 runner 和宿主构建器；选择错误或工具不足会明确失败。本流程以指定提交可信为前提，不是执行恶意代码的安全沙箱。

## 源码与工具

`tools/run_linux_host_candidate_ci.py` 检查请求的 commit 与检出 HEAD 一致、仓库无未提交内容，再以 `git archive` 导出到新建的私有 `build/` 目录。它逐个比较全部 Git 文件和归档字节，拒绝符号链接、子模块、路径逃逸、大小写冲突及超限输入；构建前后再次检查快照文件集合、字节和检出身份。另在快照外生成现有 source-lock，并核对候选中的全部本项目来源记录。归档与锁的哈希不构成签名的完整构建来源证明。

任务只发现 runner 上已有的 CMake、Ninja、GCC/G++、JDK 和同一 LLVM 目录内的 `llvm-strip/readobj/nm/ar/readelf`；不安装工具或依赖。缺少任何一项、`build/` 与 `dist/` 不在同一文件系统、目标目录已存在或平台不符时直接失败。子进程只收到运行需要的环境字段，不传递 GitHub token、loader 注入、编译器额外标志或 Python/Java 注入变量。工具路径和原始标准输出/错误只留在隔离工作目录，不放进公开 receipt。

## 扫描、回读与上传

CI 使用新生成的**仅供该 runner 的 sentinel** 作为构建器要求的私有标识输入，另执行现有的有界通用路径和递归归档扫描。该 sentinel 不等于用户本机的私有标识清单，不能声称完成了本机标识扫描，也不能证明不存在所有敏感信息。仓库工作区及该 runner 只处理检出的公开源码，不引入本机 P6、DEX、私有日志或密钥。

构建器从已解包回读的候选 SDK/JAR 编译运行 pressure v1/v2 和真实 Java 图像消费者；图像覆盖默认 12 组、合成压力 12 组及 10 组失败路径。runner 进一步核对 SOURCE 与 source-lock、源码头文件、候选核心/JNI/JAR/pressure 哈希、ABI 标识、消费者 receipt、必需步骤及所有私有 journal/日志摘要。实际加载模块路径与磁盘哈希不等于映射内存字节或签名的来源证明。

仅当构建、受控消费、回读、扫描和前后身份检查全部通过后，runner 才创建 `build/linux-host-candidate-ci/export/`，其中恰有一个 `.tar.gz` 候选和一个只含相对名称、哈希及范围的 `receipt.json`。workflow 只上传这两个文件，保留 7 天；任何步骤失败时不执行上传。**不要上传整个 build、source.zip、源码快照、标识文件、原始日志或环境。**

## 证据边界

成功仅证明该 GitHub-hosted Linux 宿主、固定源码、检查过的工具与工作量；Linux 相对动态库链接必须在使用者解包时保留。结果不证明 Android ART、R8、任何真机、TLB 机制、性能、能耗或 BIL 集成。它是未签名工程候选，项目许可证仍需确定。CI 输出只记录指定文件与结果，不提供源码、构建环境或运行时映射字节的完整签名证明。

不需要 Linux 工具链或设备即可运行 runner 自身的离线合同测试：

```text
python tools/test_run_linux_host_candidate_ci.py
```
