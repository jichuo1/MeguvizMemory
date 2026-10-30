# 家族CPU内存合同核实

2026-10-30，只读审计；本批不修改Compute/Media/BIL，也不把当前源码或既有identity SDK当作新桥接运行资格。具体逐文件身份见 `artifacts/features-20261030/family-contracts.json`。Compute HEAD为7581256f、Media为4ebbfcd4，两工作树均有其他会话未提交修改；HEAD不足以标识所见文件内容。

## 已确认的真实接口

| 来源 | 实际合同 | 对Memory意味着什么 |
|---|---|---|
| Compute `MgcResourceDesc` / `mgc_resource_create` | usage=0表示host；公开descriptor没有外部CPU指针或allocator vtable；实现自行resize resource持有的vector | 不能把Memory backing直接注入Compute资源分配，也不能声称接入了零复制scratch provider |
| Compute `MgcLeaseDesc` | content_context retain/release保活内容，独立于allocation引用 | 内容寿命回调不是allocation-provider接口 |
| Compute `mgc_lease_map/unmap` | host-only；map返回lease范围内的指针，并保有lease引用；unmap减少映射计数并归还引用；in-flight/重复map会拒绝 | 只有保持map到最后一次Memory借用结束，才可能安全包装该CPU范围；不得用free释放它 |
| Compute `mgc_lease_publish_read` | 已完成WRITE可封为READ；有map或外部在途提交时拒绝 | READ导入应基于已封存的真实READ内容，不能把未完成写入当immutable |
| Media `MgmFrameDesc` / frame create | frame持有MgcLease；create retain content，final frame release归还 | 没有直接接受Memory handle的frame入口；frame clone不复制像素不代表可以换掉origin-owner |
| Compute/Media drain | session等待已受理工作、回调和提交引用；消费者保有的输出lease可继续有效 | Memory collect/drain不能代替家族retirement、fence或用户最后输出引用 |

既有安装目录存在真实ARM64/armv7 Config、头文件与静态库。所见identity profile的ncnn/RIFE开关为OFF，是CPU/reference范围，不代表完整GPU组合或当前dirty源码已经重新构建验证。

## 可研究的方向与仍缺的门槛

1. **Compute → Memory只读导入**：新外部导入API通过后，可以设计一个独立adapter持有完整CPU READ lease的map，由最后一次Memory release执行unmap及归还原引用。为了不越过lease权限或少算底层范围，应先限制 `byte_offset=0` 且 `byte_length=capacity_bytes`，拒绝GPU/protected/未就绪/部分范围。仅retaining resource而提前unmap不够。
2. **Memory → Compute host scratch**：当前缺少公开CPU pointer/provider入口。显式copy可以另做普通copy桥，但它是不同backing、初始化和预算合同，不是零复制或allocator替换。
3. **唯一身份与记账**：需要固定Compute domain/allocation/generation/content及Memory manager runtime之间的映射。Memory为导入范围收费不等于它分配了新物理内存；两库账目不可直接相加当作总RSS。跨runtime重复包装、不同VA映射到同一物理页及未知allocator额外容量不作自动去重承诺。
4. **生命周期**：需要拒绝重入关闭、跨线程最后释放、失败原子、输出延寿和map保活的实际测试，以及固定且可重建的SDK/source闭包。不能把当前source审阅升级为已运行adapter。

本批结论是**合同已核实，家族桥接尚未实现或取得资格**。阻塞不是“找不到SDK”，而是没有Memory-owned CPU scratch注入合同；反向READ-map adapter是一个有明确前置条件的候选。本批不接管AHB、GPU池、ncnn runtime或BIL帧账本。
