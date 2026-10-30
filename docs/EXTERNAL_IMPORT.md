# 外部内存导入与软预算（ABI 1.1）

本批新增 `mgmem_import_external_v1`、`mgmem_domain_import_snapshot_v1` 和 `mgmem_domain_set_budget_v1`，旧ABI1.0的POD布局和符号不变。C++ `domain::import_external` 返回原有 `owned_allocation`，借用、跨线程延寿和关闭沿用原状态机。它不接管ART、Java DirectByteBuffer、AHB或GPU retirement，也不提供任意allocator互换释放。

## 唯一backing合同

`mgmem_external_buffer_v1` 必须描述一个完整、真实、稳定的外部backing：地址、全部可访问容量、对齐、外部owner的非零稳定ID、读写权限和retain/release。一次成功导入创建一个owner handle，**全容量**计入既有domain预算；多个子视图用同一handle的offset/length lease表达，不重新导入或重复收费。当前仅支持SYSTEM域，arena导入明确返回UNSUPPORTED。

`external_id` 在同一manager runtime的所有活跃导入中必须唯一对应这份region及其origin owner；独立origin也不能使用相同ID表示不同存储。不能给同一backing的子区间另造ID规避账目。模块范围registry拒绝重复ID及与任何已登记owned/external region的重叠，包括其他domain和在途import。相邻不重叠region允许。虚拟地址区间是检查依据，不能识别不同VA映射的相同物理页，也不是进程范围PFN去重。

descriptor的 `managing_runtime_origin` 必须显式设置为管理它的 `mgmem_runtime_origin_v1()`；复制到另一份已装载core会被路由cookie拒绝。C和C++wrapper都拒绝cookie=0或foreign descriptor，不自动重标，以免同一未绑定descriptor在不同runtime里分别被静默接受。该cookie是防误路由，不是认证；恶意重包装、不同VA别名及跨runtime协调均不受支持，同一物理backing必须只交给一个manager runtime。

调用者负责提供有效内存和真实owner、保持自身引用到导入调用返回，并在整个retained期间保持地址/容量稳定。origin回调代码及其所属模块也必须保持装载，retain不会自动取得动态库loader引用。READ禁止WRITE lease，但不执行页保护；C调用者不能通过裸指针违反取得的访问权限，外部别名也必须遵守origin owner的同步合同。

## 生命周期、失败和关闭

先在锁内校验、预留预算与metadata，再在domain/registry锁外调用retain。retain返回非OK必须失败原子、不增加引用；此时不调用release。成功retain后若close竞争，锁外release回滚，输出handle仍为空。地址范围、对齐、size_t缩窄、ID、版本或预算拒绝发生在任何retain前。

成功owner release后，已取得lease继续保活region。最后lease结束后才执行origin release；Memory不对external地址调用free或domain provider.deallocate。release可能同步阻塞，应在允许清理的线程完成。callback期间pending、capacity charge及registry claim仍保持，不能让下一次申请或重叠导入绕过预算。begin_close不撤销现有lease；collect/drain/destroy不强制回收它们。

registry锁序固定为domain→registry，或不持domain时单独取得registry；绝不registry→domain。任何provider/external回调均在两类锁外执行。provider正常重用一个刚释放地址时，只有旧claim已进入无owner/lease的callback回收状态才允许新owned claim暂态重叠；external import仍等待旧claim真正解除。active owned或external region冲突的provider返回值属于违约，核心隔离该请求，不调用可能释放原owner的deallocator。

retain抛异常时是否已加引用不可知；release抛异常时是否已释放不可知。因此核心poison域、保留不确定容量和claim，不重试、不强行释放。它不是可继续使用的正常关闭路径。普通使用必须满足thread-safe/no-throw/failure-atomic回调合同。

## 软预算与观测

`set_budget` 在OPEN域上设置新的准入上限，允许0和低于当前backing+reservation。已有资源及已经拥有reservation的allocate/import继续完成；后续新准入在当前charge超上限时拒绝，arena已有空隙也不能绕过。恢复预算必须显式调用，不隐式close、移动或free现存资源。

旧snapshot的backing/reserved/peak现在也包含external容量。新import snapshot只提供其中external committed/count、pending import及累计成功import数量，还有poison标记；**不能再加回旧总数**。provider_allocation_count仍仅计成功发布的provider backing；external不增加该计数。capacity是请求/申报容量，不是malloc usable size、metadata、RSS/PSS或物理RAM。

消费者若导入预读vector，外部vector capacity与Memory imported backing是同一个region的两个账目视角，端到端唯一容量必须按identity去重，不能再次相加。旧“预读source再复制到新input”的两份真实存储才可以相加；不能沿用旧复制对照公式误计新的导入路径。

## 验证范围

`tests/import_tests.cpp` 的正常7组覆盖参数/溢出/预算前拒绝、retain失败原子、重复ID/重叠/跨domain拒绝、owned backing别名、只读及整个allocation互斥、close竞争、pending import/provider软降预算、callback保留charge、最后跨线程释放和C++RAII延寿，正常路径全部归零并销毁。

3个独立 `--poison-*` 模式仅注入回调违约或provider别名，检查隔离状态、保留账目及不误释放原地址，然后用 `_Exit` 结束隔离进程；不能把这些资源声称为正常回收成功。正常测试不使用该退出方式，ASan/普通回归分别验收。host通过不自动构成新Android装载、16KB、MTE、性能或BIL接入资格。
