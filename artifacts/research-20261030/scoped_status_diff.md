# 状态范围修正

原基线为764bfee；原24个MM任务的dependencies/conditional_dependencies/required_outputs/dependency_contract逐项保持。

| 字段 | 修正与依据 |
|---|---|
| MM-003 | 拆分host、旧独立arm64、新v2、BIL产品；128 native任务不冒称128独立窗口 |
| MM-018 | 拆分arm64 4KB已验证、armv7未执行、真实16KB未执行 |
| CAPABILITY product_performance | BIL产品NOT_STARTED；独立retry1继续INCONCLUSIVE |
| workload formal_sampling | 补正已完成32块/128任务的过期NOT_STARTED字段；未重写原计划/数据/报告 |
| 新R1-R6 | 仅本批有限交付完成；不将MM父任务、性能或发布标DONE |
| planned/achieved precision | pilot未超cap与formal实际log半宽0.011913分别记录 |
