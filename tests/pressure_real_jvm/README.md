# 真实宿主 JVM 与压力 JNI 关闭验证

本测试使用真实 JVM、`NativePressureBackend`、`MemoryPressureAdapter` 和真实 native core、pressure controller、JNI 源码。Android framework 类型来自宿主桩；core、pressure 和 JNI 编译进一个仅供宿主测试的共享库，不代表 Android AAR 的动态链接闭包。

测试重复创建独立 arena，直接调用合成压力回调，同时启动两个线程分别调用 backend 和 adapter 的关闭入口。若调用遇到仍在受理中的请求，测试显式重试；已有 probe 跨关闭保持可读，有界等待不得强行回收。释放 probe 后检查完成通知、正常关闭和最终 backing、reservation、live、retiring、retained 归零。

同时放行两个线程并不保证每次都命中竞争窗口；确定性的暂停和失败注入由 `tests/jni_lock_tests.cpp` 与 `tests/android_pressure_close/NativeBackendConcurrencyTest.java` 分别覆盖。本测试不证明 Android ART、系统低内存、设备长稳、产品行为或性能收益。

```text
cmake -S tests/pressure_real_jvm -B build/pressure-real-jvm -DCMAKE_BUILD_TYPE=Release
cmake --build build/pressure-real-jvm --config Release
ctest --test-dir build/pressure-real-jvm -C Release --output-on-failure
```
