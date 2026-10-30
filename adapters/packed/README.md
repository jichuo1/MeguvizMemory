# Packed reader

独立、无第三方生产依赖的Java11 adapter。生产JAR只包含 `io.github.jichuo1.meguviz.memory.packed.PackedDanmakuReader` 及其嵌套类型，不包含BIL源码、测试或Kotlin运行时。

它直接读取已存在的 `int[]` flat，提供primitive字段和保留/改色位图；不建立逐条 `ElemSignal` 或 `List<Int?>`。这是普通消费者对象物化优化，与Memory C ABI无依赖关系，不能算成Memory净收益。

使用前阅读 [完整合同与证据](../../docs/PACKED_READER.md)。`copyOf` 默认隔离输入别名；`borrowImmutable` / `selectImmutable` 需要调用者在reader关闭前保持flat和reply不可变。close仅丢引用，不保证立即GC或RSS下降。

```java
try (PackedDanmakuReader reader = PackedDanmakuReader.copyOf(flat, replyBytes, visibleBudget)) {
    PackedDanmakuReader.Decision decision = reader.decide(
        hasThreshold, minimumWeight, removeVipColorful, vipValue, semanticBlocked);
    // 由原调用者保留 !changed、原wire rewrite、rescan和host parseFrom步骤。
    // decision的三份独立boolean数组可传给已有rewriter；不改变native准入策略。
}
```

验证和打包使用已有JDK，不联网：

```text
python tools/verify_packed_reader.py --java-only --build-dir build/packed-java-ci --receipt build/packed-java-ci/receipt.json
python tools/verify_packed_reader.py --bil-source <只读BIL目录> --kotlin-lib <已有Kotlin compiler jar目录>
```

第二条会核验并复制两份真实oracle源码到本仓库build目录，独立编译；不运行Gradle、不修改或构建BIL。JAR默认输出在 `build/packed-reader/`；不是已发布版本或BIL接入制品。
