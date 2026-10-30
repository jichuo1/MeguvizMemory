# 真实 CPU 工作负载候选审计（2026-10-30）

结论：优先把现有独立图像消费者的 **RGB8 → 整数 luma → clamp 边缘 3×3 box blur** 作为首个可执行 A1/A3 配对场景，使用固定真实像素输入。它是实际 CPU 图像工作，区别于 malloc 循环；但它不是已证明的 BIL 播放器热点。**即使此场景有收益，也不能直接据此接入 BIL。** 当前 BIL 适用性仍需真实热点/寿命证据，未据静态代码判定值得接入。

本审计只读 BIL/家族/外部工程，没有启动性能采样、没有操作设备，也没有修改外部仓库。本文件是本代理唯一写入。

## 当前来源

- BIL main：`00572940c354fbec7b6f311d4ecd6f02e66ea7c5`，通过 `git -c safe.directory=BIL_WORKSPACE -C BIL_WORKSPACE rev-parse HEAD` 读取。
- `Bilibili_Innocent_Lab/app/src/main/cpp/player_interpolation/frame_engine.cpp` SHA256：`8dce89c83b52ba734723846879326005b2d43de6f648610759a11ded51124a87`。
- 已读 Memory `consumers/image/main.cpp` 全文、`consumers/dex/main.cpp` 的真实 source/query/lease 入口，以及 BIL 的 frame engine、scratch、capability、packed 调用线索。
- 外部项目仍有并行会话；最终实验应使用固定快照与实际制品 hash。本审计的 current-source 读数不能自动覆盖之后的变化。

## BIL 候选及去重

| 路径 | 当前源码事实 | 进入 Memory 的判断 |
|---|---|---|
| frame engine staging | `Impl` 约 :235 为持久 `std::vector<uint8_t> staging`；process :487 在需要时 resize；:490–493 拷贝端点帧；:764 将 ncnn 输出写进 staging；:770–774 只在完整有效结果后逐行 commit | 不得把每次 resize 当每帧 malloc，也不能取消结果 commit/错误原子性。当前没有 profile 证明此 owner 管理是热点；不应凭名字再建 CPU/GPU pool |
| ncnn input/output CPU 转换 | `inputTensor` :104 使用 ncnn::Mat::from_pixels；:750–764 先 crop/逐平面有限值检查，再 normalize/to_pixels | 分配器与 Mat 寿命属于原 ncnn/Compute owner。Memory 外置图像 benchmark 不等于这条路径已接入，不得删 finite 检查或算子来制造收益 |
| GPU 提交局部容器 | :536 `keepAlive`、:538 `downs`、:540 `fresh`；:571/:580/:647 constants vector；原注释要求 Extractor/VkMat 活到 submit_and_wait 之后 | 当前文件未见 reserve()；不能声称已 reserve。仍需调用栈和完整 workload 计数，不能用几行 vector 构造推算毫秒。不得为 Memory 接入改变 GPU retirement |
| 准备 scratch | `FrameInterpolationEngineHolder.kt` :193–204 在 preparation session 建立既有 FrameScratchArena/max-size hint；:252–259 warm 使用原 scratch/zeroed；probe 保留四输入和单独输出及第二快统计 | 既有 P1-07 属于 A1，不重复计 Memory 收益。Java direct→native 还改变 backing/owner，差值只能称迁移与接入总差 |
| packed reader | `DanmakuPurifyFeatureInstaller.kt` :419 已有 !changed 快路，:434 host parseFrom 复检，:444 仍 `scan(...).let(DanmakuWire::fromFlattened)` | packed 对象物化可独立优化，但这批没有实现。不能拿现有 flat 数据大小或 !changed 快路当新增 Memory 节省 |

首报中“局部 vector 已 reserve”的判断不准确，已在读取当前文件后更正：本次 `rg reserve\(` 未发现调用。该更正不形成“因此必须接入 Memory”的结论。

## 可固定的非私密真实输入

以下均为已在本机存在的文件，审计只读取字节并计算 hash。可在本地实验使用；**没有据此假定 APK/图片的公开再分发许可**。输入及派生 PPM 宜放 `artifacts/local/`，公开仓库只保留身份、生成方法、结果 checksum 和统计。

### 真实像素首选

- 本机随系统预置：`C:/Windows/Web/Wallpaper/Windows/img0.jpg`。
- 原始尺寸 3840×2400，RGB，1,602,752 B。
- SHA256 `a37491a5e479e191cfe547b484426037455ce8aacff7ab15f91925330c24864f`。
- 已有解释器 `D:/stable-diffusion-webui/venv/Scripts/python.exe` 的 Pillow 为 9.5.0，可离线 decode；无需新增生产依赖。
- 建议固定中心 crop 到 1920×1080，不缩放：left=960、top=660、right=2880、bottom=1740；RGB P6 生成在计时外，记录 decode/crop/P6 hash。源图像超过现有 8M-pixel CLI 上限，不能直接传入原尺寸后把失败快返作为成功样本。

### 更接近 BIL 输入来源的备选

- APK：`BIL_WORKSPACE/Bilibili_Innocent_Lab/哔哩哔哩_9.17.0(9170100).apk`。
- APK 243,687,778 B；SHA256 `f0f880e841d017db335b3919e6d4792961f2e08aa9993da661b626d52cfcb955`。
- 图像 member：`assets/composeResources/srcs.app.theseus.theseus.ogv.generated.resources/drawable-xxhdpi/theseus_ogv_video_card_go.png`。
- 原始 1254×1254，690,905 B；member SHA256 `6f82f11a6cfbb909b2e6440dab26dff39db839b6c08fdc0a3900eb77f3a42b8a`。
- 这是宿主打包的 UI 图像，不能把对它运行独立 blur 的结果称为真实视频播放热点、质量等价或 BIL 端到端收益。

### 真实 CPU 解析备选

同一 APK 有 35 个 DEX member。`classes.dex` 为 10,540,184 B、DEX 037，SHA256 `83ac99bc338e8bfc1980e382a4c318871cac0364dc449ae7b0316f601c136ab0`。

可用真实 DexMeguviz borrowed snapshot/完整 method query/每字段结果摘要作为 CPU 任务，并保留 copy-in 语义 oracle。但 Memory 在当前接入只拥有一份输入 backing；parser 内部事实/查询容器不经 Memory 分配。A1 原生 System 的单块输入与 A3 arena 的单块输入几乎没有申请合并机会，不能将 parser 内部成本或既有 mmap 复用算成库收益。它适合测接入总差或取得负结果，不能据此直接代表 BIL 的 DexKit 查询路径。

## 首个 A1/A3 最小重放合同

1. **A1 必须新增真实现有 System 控制。** 当前图像 `--system` 仍通过 Memory System provider，所以它是 A2 功能路径，不能改名 A1。A1 用相同原生系统分配 primitive、64B 对齐、同样三个有效区间，申请失败显式失败。
2. A1 和 A3 都按 session 建立并复用 RGB/luma/output 工作区，使用相同帧数和同一真实 P6。不能让 A1 每帧重新申请、A3 却 session 复用，把常规优化再计一次。
3. 算法冻结为现有消费者的 `(77R+150G+29B)>>8` 与每个像素 clamp 3×3 sum/9。相同输入加载、初始化、copy、线程/排队、warmup、输出验证和任务成功条件；若输出与中间区均完全被写入，双方均可不额外清零，不能只给一边取消初始化。
4. A3 使用安装后的 Memory API、显式 owner/lease；lease 必须在任务受理之前取得，最后访问之后归还。domain create、预算/统计/lease、collect/drain/destroy 的成本均须进入所声明的总工作量。A1 也要把匹配的 buffer create/free 与 join/完成成本算入。
5. 记录分段 CPU 任务 elapsed 和完整 session elapsed。磁盘 decode/read/write 是否计入主指标须预先固定，不能把成本挪出后宣称端到端消失。若计时外预读共同输入，列为共享外部存储；不能从唯一 backing 预算中悄悄漏算。
6. 输出逐字节 oracle 在资格轮验证；低扰动计时轮保留固定 checksum/成功计数，不能允许编译器消除重复计算。源码、工具链、输入、库、harness 和两个 variant 制品都须固定 hash。
7. 独立 pilot 只用于样本量与热阈值；正式配对 session 的 AB/BA 或 ABBA 顺序、样本数、排除阈值、统计方法、性能/内存回归预算先冻结。保留慢样本、失败、超时和回退，不挑最快值。
8. 首轮结论只针对该设备/核心/系统页/图像 workload 的 `(A1-A3)/A1` 净增量。原始 phase 若有嵌套不重复相加；不能把微基准、申请次数或 arena backing 改变外推 FPS、RSS 或能量。

## BIL 进入/退出依据

当前没有足够证据选择 BIL 原生 owner 迁移。若独立 CPU 配对无稳定净收益或回归超预算，保留 System，记录“不接入 BIL”。若有净收益，仍需先在实际 BIL source/访问/寿命/成功工作量中找到同类可干预成本，并重新评估已有 scratch、ncnn ownership 与 GPU/帧 retirement，再决定是否建立薄 adapter。16 KB/MTE/能量等不可用条件独立标记，不拿它们作为修改工作量或伪造收益的理由。
