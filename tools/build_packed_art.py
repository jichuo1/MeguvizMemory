#!/usr/bin/env python3
"""Build a min-API-27 ART test container without a device or production changes.

Only the verified production JAR and freshly compiled PackedReaderTests*.class
are D8 inputs. The isolated Kotlin oracle/BIL class directory is never passed to
D8. Public provenance uses workspace/SDK-relative names and hashes, not local
machine paths. No dependencies are downloaded and no ADB calls are made.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SHA = "b527d6333e3e379aab7a4b858973f9acb28d610686312200f438c142a0373ce6"
PRODUCTION_PREFIX = "io/github/jichuo1/meguviz/memory/packed/PackedDanmakuReader"
PRODUCTION_CLASSES = {PRODUCTION_PREFIX + tail + ".class" for tail in
                      ("", "$BudgetExceededException", "$Decision", "$ScanProvider")}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(argv, commands, name, timeout=120):
    result = subprocess.run([str(item) for item in argv], capture_output=True,
                            timeout=timeout, check=False)
    commands.append({"name": name, "exit_code": result.returncode,
                     "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
                     "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
                     "device_operation": False})
    if result.returncode:
        sys.stderr.write(result.stdout.decode("utf-8", "replace") +
                         result.stderr.decode("utf-8", "replace"))
        raise RuntimeError(name + "_failed")
    return result.stdout.decode("utf-8", "replace")


def dex_classes(raw):
    """Read the DEX class-def descriptors, with bounded table/string access."""
    require(len(raw) >= 112 and re.fullmatch(rb"dex\n[0-9]{3}\x00", raw[:8]), "invalid_dex_magic")
    u32 = lambda at: struct.unpack_from("<I", raw, at)[0]
    require(u32(32) == len(raw) and u32(36) == 112 and u32(40) == 0x12345678,
            "invalid_dex_header")
    require(hashlib.sha1(raw[32:]).digest() == raw[12:32], "invalid_dex_signature")
    require(zlib.adler32(raw[12:]) & 0xffffffff == u32(8), "invalid_dex_checksum")
    string_count, string_offset = u32(56), u32(60)
    type_count, type_offset = u32(64), u32(68)
    class_count, class_offset = u32(96), u32(100)
    for count, offset, width in ((string_count, string_offset, 4),
                                  (type_count, type_offset, 4), (class_count, class_offset, 32)):
        require(0 < count and 112 <= offset <= len(raw) and count <= (len(raw)-offset)//width,
                "invalid_dex_table_bounds")
    descriptors = []
    for index in range(class_count):
        type_index = u32(class_offset + 32*index)
        require(type_index < type_count, "invalid_dex_class_type")
        string_index = u32(type_offset + 4*type_index)
        require(string_index < string_count, "invalid_dex_descriptor_index")
        position = u32(string_offset + 4*string_index)
        require(112 <= position < len(raw), "invalid_dex_string_offset")
        for _ in range(5):
            require(position < len(raw), "truncated_dex_uleb")
            byte = raw[position]
            position += 1
            if byte & 0x80 == 0:
                break
        else:
            raise ValueError("oversized_dex_uleb")
        end = raw.find(b"\0", position)
        require(end >= position, "unterminated_dex_descriptor")
        descriptor = raw[position:end].decode("ascii")
        require(re.fullmatch(r"L[A-Za-z0-9_/$-]+;", descriptor), "invalid_dex_class_descriptor")
        descriptors.append(descriptor)
    require(len(descriptors) == len(set(descriptors)), "duplicate_dex_class_definition")
    return sorted(descriptors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--jdk", type=Path)
    parser.add_argument("--build-tools", default="37.0.0")
    parser.add_argument("--platform", default="android-37.0")
    parser.add_argument("--production-jar", type=Path,
                        default=ROOT/"build/packed-reader/meguviz-memory-packed-reader-0.1.0.jar")
    parser.add_argument("--expected-production-sha256", default=PRODUCTION_SHA)
    parser.add_argument("--out-dir", type=Path, default=ROOT/"build/packed-art")
    parser.add_argument("--receipt", type=Path, default=ROOT/"artifacts/features-20261030/packed-art-build.json")
    args = parser.parse_args()
    sdk = args.sdk.resolve()
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.build_tools) and
            re.fullmatch(r"android-[0-9]+(?:\.[0-9]+)?", args.platform), "invalid_sdk_component_name")
    d8 = sdk/"build-tools"/args.build_tools/"lib/d8.jar"
    android = sdk/"platforms"/args.platform/"android.jar"
    require(d8.is_file() and android.is_file(), "existing_d8_and_android_platform_required")
    suffix = ".exe" if os.name == "nt" else ""
    java = args.jdk/"bin"/("java"+suffix) if args.jdk else Path(shutil.which("java") or "")
    require(java.is_file(), "existing_java_required")
    production = args.production_jar.resolve()
    require(production.is_relative_to(ROOT) and production.is_file() and
            re.fullmatch(r"[0-9a-f]{64}", args.expected_production_sha256) and
            sha(production) == args.expected_production_sha256, "production_jar_identity_mismatch")
    with zipfile.ZipFile(production) as archive:
        require(set(archive.namelist()) == PRODUCTION_CLASSES and archive.testzip() is None,
                "production_jar_has_unexpected_classes")
    output = args.out_dir.resolve()
    receipt_path = args.receipt.resolve()
    require(output.is_relative_to(ROOT/"build") and output != ROOT/"build" and not output.exists(),
            "output_must_be_new_build_directory")
    require(receipt_path.is_relative_to(ROOT), "receipt_must_stay_inside_workspace")
    output.mkdir(parents=True)
    commands = []
    host = output/"host-build"
    verifier = [sys.executable, ROOT/"tools/verify_packed_reader.py", "--java-only",
                "--build-dir", host, "--receipt", host/"receipt.json"]
    if args.jdk:
        verifier += ["--jdk", args.jdk.resolve()]
    run(verifier, commands, "fresh_java_contracts")
    host_receipt = json.loads((host/"receipt.json").read_text(encoding="utf-8"))
    require(host_receipt["status"] == "PASS" and host_receipt["java_contracts"]["cases"] == 32,
            "fresh_java_contracts_not_qualified")
    require(host_receipt["jar"]["sha256"] == sha(production), "fresh_production_classes_differ")
    classes = host/"classes"
    tests = sorted(classes.glob("PackedReaderTests*.class"))
    require(tests and (classes/"PackedReaderTests.class") in tests, "missing_fresh_main_test")
    tests_jar = output/"packed-contract-test-classes.jar"
    with zipfile.ZipFile(tests_jar, "x", zipfile.ZIP_DEFLATED) as archive:
        for path in tests:
            item = zipfile.ZipInfo(path.name, (1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o644 << 16
            archive.writestr(item, path.read_bytes())
    dex_jar = output/"packed-tests.jar"
    run([java, "-cp", d8, "com.android.tools.r8.D8", "--release", "--min-api", "27",
         "--lib", android, "--output", dex_jar, production, tests_jar], commands, "D8_min_api_27")
    with zipfile.ZipFile(dex_jar) as archive:
        require(archive.namelist() == ["classes.dex"] and archive.testzip() is None,
                "unexpected_dex_container_entries")
        dex = archive.read("classes.dex")
    descriptors = dex_classes(dex)
    required = {"L"+name[:-6]+";" for name in PRODUCTION_CLASSES} | {"LPackedReaderTests;"}
    require(required.issubset(descriptors), "required_program_classes_missing")
    require(all(name in required or name.startswith("LPackedReaderTests$") or
                name.startswith("L"+PRODUCTION_PREFIX+"$") or name.startswith("L"+PRODUCTION_PREFIX+"-")
                for name in descriptors),
            "foreign_or_oracle_class_in_dex")
    require(sha(production) == args.expected_production_sha256, "production_jar_changed_during_build")
    def identity(path):
        return {"path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size, "sha256": sha(path)}
    receipt = {"schema_version": 1, "scope": "ART_PACKED_TEST_CONTAINER_BUILD_ONLY_NO_DEVICE",
               "status": "PASS", "actual_device_operations": 0, "minimum_api": 27,
               "main_class": "PackedReaderTests", "production_dependencies_added": [],
               "production_jar": identity(production), "fresh_java_contracts": host_receipt["java_contracts"],
               "fresh_host_receipt": identity(host/"receipt.json"),
               "test_class_inputs": [identity(path) for path in tests],
               "test_classes_jar": identity(tests_jar), "artifact": identity(dex_jar),
               "dex": {"bytes": len(dex), "sha256": hashlib.sha256(dex).hexdigest(),
                       "version": dex[4:7].decode("ascii"), "class_definitions": descriptors},
               "toolchain": [{"component": path.relative_to(sdk).as_posix(), "bytes": path.stat().st_size,
                              "sha256": sha(path)} for path in (d8, android)],
               "sources": [identity(ROOT/relative) for relative in ("tools/build_packed_art.py",
                    "tools/verify_packed_reader.py", "tests/packed/PackedReaderTests.java",
                    "adapters/packed/src/main/java/io/github/jichuo1/meguviz/memory/packed/PackedDanmakuReader.java")],
               "commands": commands, "excluded": ["Kotlin oracle/BIL class files", "Kotlin runtime", "private inputs"],
               "qualification": {"host_java_contracts": "PASS_32", "D8_build": "PASS", "ART_execution": "NOT_RUN",
                                 "BIL_integration": "NOT_PERFORMED", "performance": "NOT_MEASURED"}}
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+"\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": "PASS", "artifact": receipt["artifact"],
                      "dex_class_count": len(descriptors), "actual_device_operations": 0}))


if __name__ == "__main__":
    main()
