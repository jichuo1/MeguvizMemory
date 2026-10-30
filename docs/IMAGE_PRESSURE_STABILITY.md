# 图像工作区、压力、预算与异步关闭组合

`consumers/image/pressure_stability.cpp` 是单独的安装后消费者目标 `mgmem_image_pressure_stability`，链接 `MeguvizMemory::pressure` 及其同一 `MeguvizMemory::mgmem` runtime。既有 `paired.cpp`、`perf.cpp`、冻结协议和历史数据不修改。本项是有限资源稳定性/功能验证，不做配对性能判定，不修改 BIL 或家族 owner。

## 工作量与 oracle

实际读取 P6 RGB 文件，使用既有消费者的整数灰度 `(77R+150G+29B)/256` 和 clamp 边缘 3×3 box blur，灰度除法与 blur 除法分开。三行环属于已实现的普通布局优化，不归 Memory 增量。

`tools/verify_image_pressure.py` 用独立 Python 全灰度平面和标量索引生成 P5 oracle。原生每一帧逐字节比较 oracle，检查每个灰度像素只转换一次；宿主再逐字节检查各尺寸的最终文件。输出文件按输入编号覆盖，因此不是保存每一轮文件的证据；每一帧的比较和总 checksum/count 由原生回执记录。Oracle 数组仅为测试输入，不在域内，也不把它与域 backing 合并成应用 RSS。

## 每轮安全合同

每个 cycle 创建一个独立域及压力控制器，依次处理多个真实尺寸，每帧包含：

1. 申请 RGB、三行灰度和输出三个 workspace，入队前取得全部 WRITE lease。
2. 显式 CRITICAL 将软预算降到 0。已有 workspace/lease 继续有效，1 B 新申请被拒绝；等待压力 worker 的 quiet-only trim 返回 BUSY，确认延后计数增加。
3. 显式 NORMAL 恢复原预算，新的 16 B marker 申请和释放成功；恢复不靠“没有回调”推断。
4. 归还 allocation owners，worker 持有的已准入 lease 继续保活。普通帧计算并释放后，等待压力 worker 实际完成 quiet trim，backing 回到 0，再换尺寸。
5. 最后一帧在 gate 后等待。主线程先停止并 join/destroy 压力控制器，确认原 owner retain/release 成对，再 begin_close 域；collect 为 BUSY，drain(0) 和 drain(5 ms) 均为 TIMEOUT，退役 payload/3 个 lease 仍在，关闭后新申请拒绝。
6. 放行异步 worker，在 domain CLOSING 后完成真实像素计算、oracle 比较和输出，再在 worker 上归还 lease。主线程 join 后才 collect/drain/snapshot/destroy，确认 backing/reservation/live/retiring/retained/owners/leases 全为 0。

裸 C domain 的寿命由消费者保持；destroy 与其他 API 调用串行化。该测试不提供新的通用 owner wrapper，也不通过超时强制 free。压力控制器只关闭自身，域由真正 owner 关闭。

异常清理若无法完成，Session 析构先向 stderr 写出清理未完成原因，再以 `_Exit(3)` 隔离退出，避免仍持有 `lifetime.context` 的控制器引用已离栈的 Session。该路径不继续运行、不重试未知回收、不输出正常 `CLOSED_ZERO`，也不算资源归零或正常稳定性通过；本次普通短回归不注入或取得该隔离路径的运行资格。

## 有界稳定性

支持 `--cycles 1..1000000`（最低完整循环数）、`--min-duration-ms 0..900000`（最低实际循环时长）、`--hold-ms 0..1000`、`--wait-ms 10..60000`、`--wall-ms 100..3600000`，输入最多 8 个，单图最多 8 Mi 像素。最低时长内持续完成真实变尺寸工作和完整关闭循环，默认 hold=0，不添加空闲 sleep 凑时长；达到最低循环数后仍继续至最低时长，最多 1000000 cycles。每次 cycle 完整关闭，cycle 内在不同尺寸间回收并重建 workspace。墙钟超限、循环上限先到或未完成最低 cycle 明确失败，不截取部分成功宣称整轮通过。单个阻塞 provider 的强制终止/恢复不在本消费者内实现。

回执记录最低/完成 cycle、最低与实际 active loop 时长、真实帧数、每帧预算拒绝与恢复准入、BUSY/trim、持 lease 关闭超时、累计 provider allocations、峰值唯一 managed backing 和实际诊断总时长。外部 source capacity 分栏。`rss_pss_bytes`、energy 为 null；逻辑容量、次数和时长不转换成进程驻留、能量、吞吐或 Memory 性能收益。

宿主示例（已安装 0.2 SDK）：

```powershell
.\tools\host-build.cmd -S consumers/image -B build/continuation-image-host -G Ninja -DCMAKE_BUILD_TYPE=Release "-DCMAKE_PREFIX_PATH=$PWD/install/features-host"
.\tools\host-build.cmd --build build/continuation-image-host --target mgmem_image_pressure_stability --parallel 2
python tools/verify_image_pressure.py --image-exe build/continuation-image-host/mgmem_image_pressure_stability.exe --runtime-dir install/features-host/bin --out-dir artifacts/continuation-20261030/image-host --receipt artifacts/continuation-20261030/image-host/receipt.json --cycles 20
```

对应 host 收据只证明记录的 cycles/时长。Android 交叉构建、arm64/API34+运行、framework callback、R8 和真实低内存分别需要父任务的独立设备/制品回执；本程序仅调用 native pressure API，不生成整机 OOM。

15 分钟 profile 使用 verifier 的 `--stability-min-seconds 900 --wall-ms 1200000`；两个 provider 各自至少 900 秒。父任务负责统一启动、设备准入及最终资格。本作者先前 500 固定 cycle 尝试在 profile 调整时主动中断，无完成回执、不计成功；中断记录独立保留，短测试执行源码亦封存，避免后来源码身份冒充先前执行字节。
