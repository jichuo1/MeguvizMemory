#!/usr/bin/env python3
"""Build the dependency-free packed adapter and compare the real Kotlin oracle.

No external checkout is modified or built. Hash-pinned public oracle sources are
copied to build/ and compiled with an existing local Kotlin compiler. Receipts
contain relative paths/hashes and test summaries, never local command paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
import re

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "Bilibili_Innocent_Lab/app/src/main/java/com/Bilibili_Innocent_Lab/xposedmodule/hook/feature/danmakuwire"
ORACLE_FILES = {
    "DanmakuWire.kt": "ce221d29a11b1d8b435df751bcd00ba9b173a3b66f949edebcf87ae79c377ae8",
    "DanmakuWireFields.kt": "00b263ea3e347add784c3eaa8e14e76dc8aa7da5a7c12af53a44da62fe8745a8",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(argv, cwd):
    result = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120, check=False)
    if result.returncode:
        # Errors are local diagnostics, never copied verbatim into public JSON.
        sys.stderr.write(result.stdout + result.stderr)
        raise RuntimeError("compiler_or_test_failed:" + Path(str(argv[0])).name)
    return result.stdout, result.stderr


def last_json(text):
    return json.loads(next(line for line in reversed(text.splitlines()) if line.startswith("{")))


def external_head(path):
    result = subprocess.run(["git", "-c", "safe.directory=" + path.resolve().as_posix(),
                             "-C", str(path), "rev-parse", "HEAD"], capture_output=True,
                            text=True, timeout=10, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bil-source", type=Path, default=ROOT.parent / "Bilibili_Innocent_Lab")
    parser.add_argument("--jdk", type=Path)
    parser.add_argument("--kotlin-lib", type=Path)
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build/packed-reader")
    parser.add_argument("--receipt", type=Path, default=ROOT / "artifacts/functional-next-20261030/packed/receipt.json")
    parser.add_argument("--java-only", action="store_true", help="Public CI can validate standalone Java contracts without the external oracle")
    args = parser.parse_args()
    build = args.build_dir.resolve()
    if not build.is_relative_to(ROOT):
        parser.error("build output must stay inside this workspace")
    build.mkdir(parents=True, exist_ok=True)
    classes = build / "classes"
    classes.mkdir(exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    javac = args.jdk / ("bin/javac" + suffix) if args.jdk else Path(shutil.which("javac") or "")
    java = args.jdk / ("bin/java" + suffix) if args.jdk else Path(shutil.which("java") or "")
    javap = args.jdk / ("bin/javap" + suffix) if args.jdk else Path(shutil.which("javap") or "")
    if not javac.is_file() or not java.is_file() or not javap.is_file():
        parser.error("provide an existing JDK")
    production = ROOT / "adapters/packed/src/main/java/io/github/jichuo1/meguviz/memory/packed/PackedDanmakuReader.java"
    java_tests = ROOT / "tests/packed/PackedReaderTests.java"
    command([javac, "--release", "11", "-encoding", "UTF-8", "-Xlint:all", "-d", classes,
             production, java_tests], ROOT)
    output, _ = command([java, "-ea", "-cp", classes, "PackedReaderTests"], ROOT)
    java_summary = last_json(output)
    assert java_summary["status"] == "PASS"
    bytecode, _ = command([javap, "-classpath", classes, "-c", "-p",
                            "io.github.jichuo1.meguviz.memory.packed.PackedDanmakuReader"], ROOT)
    assert not re.search(r"ElemSignal|ArrayList|Integer\.valueOf|java/lang/Integer", bytecode)
    new_classes = sorted(set(re.findall(r"\bnew\s+#[0-9]+\s+// class ([^\s]+)", bytecode)))
    oracle_summary, oracle_inputs = None, []
    head_before = external_head(args.bil_source) if not args.java_only else None
    if not args.java_only:
        if not args.kotlin_lib or not args.kotlin_lib.is_dir():
            parser.error("provide an existing Kotlin compiler library directory; no download is performed")
        oracle_dir = build / "locked-oracle"
        oracle_dir.mkdir(exist_ok=True)
        sources = []
        for name, expected in ORACLE_FILES.items():
            path = args.bil_source / PACKAGE / name
            assert path.is_file() and sha(path) == expected, "real oracle source drift:" + name
            destination = oracle_dir / name
            destination.write_bytes(path.read_bytes())
            sources.append(destination)
            oracle_inputs.append({"repository": "Bilibili_Innocent_Lab", "path": PACKAGE + "/" + name,
                                  "bytes": path.stat().st_size, "sha256": expected})
        stdlibs = sorted(args.kotlin_lib.glob("kotlin-stdlib-*.jar"))
        assert len(stdlibs) == 1, "unique existing kotlin stdlib required"
        compile_cp = str(classes) + os.pathsep + str(stdlibs[0])
        command([java, "-cp", str(args.kotlin_lib / "*"), "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
                 "-no-stdlib", "-no-reflect", "-jvm-target", "11", "-classpath", compile_cp,
                 "-d", classes, *sources, ROOT / "tests/packed/PackedOracleTests.kt"], ROOT)
        output, _ = command([java, "-ea", "-cp", compile_cp,
                             "com.Bilibili_Innocent_Lab.xposedmodule.hook.feature.danmakuwire.PackedOracleTestsKt"], ROOT)
        oracle_summary = last_json(output)
        assert oracle_summary["status"] == "PASS"
        for name, expected in ORACLE_FILES.items():
            assert sha(args.bil_source / PACKAGE / name) == expected, "external oracle changed during validation"
    head_after = external_head(args.bil_source) if not args.java_only else None
    jar = build / "meguviz-memory-packed-reader-0.1.0.jar"
    package_classes = classes / "io/github/jichuo1/meguviz/memory/packed"
    expected_classes = {"PackedDanmakuReader.class", "PackedDanmakuReader$BudgetExceededException.class",
                        "PackedDanmakuReader$Decision.class", "PackedDanmakuReader$ScanProvider.class"}
    assert {p.name for p in package_classes.glob("*.class")} == expected_classes, "unexpected stale production class; choose a fresh build directory"
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package_classes.glob("*.class")):
            entry = zipfile.ZipInfo(path.relative_to(classes).as_posix(), (1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, path.read_bytes())
    with zipfile.ZipFile(jar) as archive:
        assert archive.testzip() is None
        assert all(name.startswith("io/github/jichuo1/meguviz/memory/packed/") for name in archive.namelist())
    receipt = {
        "schema_version": 1, "scope": "STANDALONE_JVM_PACKED_READER_FUNCTIONAL_ONLY",
        "status": "PASS", "production_dependencies_added": [], "BIL_modified": False,
        "java_contracts": java_summary, "real_kotlin_oracle": oracle_summary,
        "oracle_inputs": oracle_inputs,
        "oracle_scope": "REAL_HASH_LOCKED_SOURCE_COMPILED_IN_ISOLATION" if oracle_summary else "NOT_RUN_JAVA_ONLY_CI",
        "oracle_baseline_commit": "00572940c354fbec7b6f311d4ecd6f02e66ea7c5" if oracle_summary else None,
        "external_checkout_heads": {"before": head_before, "after": head_after},
        "bytecode_structure": {"reader_sha256": hashlib.sha256(bytecode.encode()).hexdigest(),
                               "new_reference_classes": new_classes,
                               "ElemSignal_ArrayList_Integer_boxing_references": False,
                               "scope": "Static bytecode structure; not an allocation profile"},
        "sources": [{"path": p.relative_to(ROOT).as_posix(), "sha256": sha(p)}
                    for p in (production, java_tests, ROOT / "tests/packed/PackedOracleTests.kt", Path(__file__))],
        "jar": {"path": jar.relative_to(ROOT).as_posix(), "bytes": jar.stat().st_size, "sha256": sha(jar),
                "contents": "Production Java class/nested classes only; oracle/tests/Kotlin runtime excluded"},
        "attribution": "ORDINARY_CONSUMER_OBJECT_MATERIALIZATION_AVOIDANCE_NOT_MEMORY_GAIN",
        "limits": ["No native/JNI producer or host parseFrom execution; selected-provider refusal tested by contract fixtures.",
                   "Constructor count is a structural N+N_prime model, not a heap allocation profile or elapsed/RSS/energy claim.",
                   "Copied snapshots and borrowed immutable arrays have separate ownership; neither is a native Memory lease.",
                   "Semantic judging and text normalization remain supplied by the caller; no BIL integration performed."]}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": "PASS", "java_cases": java_summary["cases"],
                      "oracle_policy_comparisons": oracle_summary["policy_comparisons"] if oracle_summary else None,
                      "jar_sha256": sha(jar), "BIL_modified": False}))


if __name__ == "__main__":
    main()
