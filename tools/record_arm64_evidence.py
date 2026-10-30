#!/usr/bin/env python3
"""Verify completed arm64 receipts, immutable artifacts and raw journal archives.

No device access. Integrity PASS never means performance qualification PASS.
Run after both attempts have ended and command-logs.zip archives are complete.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

from run_paired_device import load_command_journal, public_value


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main():
    root = Path(__file__).resolve().parents[1]
    base = root / "artifacts/arm64-20261030"
    target = base / "ARTIFACTS.json"
    safety = read(base / "safety/receipt.json")
    assert safety["state"] == "PASS" and safety["all_passed"] is True
    assert safety["device"]["api"] == 37 and safety["device"]["page_bytes"] == 4096
    library = root / "install/android-arm64-v8a/lib/libmgmem.so"
    library_hash = sha(library)
    tested_library = next(row for row in safety["binary_manifest"] if row["name"] == "libmgmem.so")
    assert tested_library["sha256"] == tested_library["device_sha256"] == library_hash
    original = read(root / "ARTIFACTS.json")
    original_library = next(row for row in original["installed_profiles"]["android-arm64-v8a"]
                            if row["path"].endswith("/lib/libmgmem.so"))
    assert original_library["sha256"] == library_hash
    native_source = subprocess.check_output(["git", "-c", "safe.directory=" + root.as_posix(),
                                             "show", "5876530:consumers/dex/paired.cpp"], cwd=root)
    assert hashlib.sha256(native_source).hexdigest() == sha(root / "consumers/dex/paired.cpp")
    datasets = {}
    for name, protocol_name in (("paired", "paired_protocol_resolved.json"),
                                ("paired-retry1", "paired_protocol_retry1.json")):
        directory = base / name
        report = read(directory / "report.json")
        protocol = root / "bench" / protocol_name
        frozen_protocol = read(protocol)
        assert sha(protocol) == sha(directory / "formal-protocol.json")
        assert report["formal_file_sha256"] == sha(directory / "formal.jsonl")
        assert report["plan_sha256"] == frozen_protocol["final_plan_sha256"] == sha(directory / "formal-plan.json")
        assert report["analysis_tool_sha256"] == sha(root / "tools/paired_analysis.py")
        assert report["decision"] == "INCONCLUSIVE" and report["BIL_auto_enable"] is False
        deployment = read(directory / "deployment.json")
        assert deployment["artifacts"]["library"]["sha256"] == library_hash
        assert deployment["artifacts"]["executable"]["sha256"] == sha(root / "build/dex-paired-arm64/mgmem_dex_paired")
        archive = directory / "command-logs.zip"
        with zipfile.ZipFile(archive) as zipped:
            names = [entry.filename for entry in zipped.infolist() if not entry.is_dir()]
            expected = {path.relative_to(directory / "commands").as_posix()
                        for path in (directory / "commands").rglob("*") if path.is_file()}
            assert set(names) == expected and len(names) == len(expected)
            for member in names:
                assert hashlib.sha256(zipped.read(member)).hexdigest() == sha(directory / "commands" / member)
        journal_checks = {}
        if name == "paired-retry1":
            for phase in ("stage", "diagnostic", "pilot", "formal"):
                recovered = load_command_journal(directory / (phase + "-commands.jsonl"))
                receipt = read(directory / (phase + "-receipt.json"))
                assert receipt["state"] == "PASS" and receipt["all_passed"] is True
                expected_commands = {row["record_id"]: public_value(row) for row in receipt["commands"]}
                assert recovered == expected_commands
                journal_checks[phase] = len(recovered)
        datasets[name] = {"decision": report["decision"],
                          "planned_blocks": report["formal_blocks_planned"],
                          "valid_blocks": report["formal_blocks_valid"],
                          "full_protocol_complete": report["planned_protocol_complete"],
                          "exact_command_files_archived": len(names),
                          "journal_recovery_matches_final_receipt": journal_checks}
    assert datasets["paired"]["full_protocol_complete"] is False
    assert datasets["paired-retry1"]["full_protocol_complete"] is True
    assert datasets["paired-retry1"]["valid_blocks"] == 32
    assert read(base / "device-window-finished.json")["state"] == "RELEASED_TO_OTHER_SESSIONS"
    files = []
    for path in sorted(base.rglob("*")):
        if (not path.is_file() or path == target or "commands" in path.relative_to(base).parts or
                path.name.endswith(".pending")):
            continue
        files.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
                      "sha256": sha(path)})
    binaries = []
    for path in (library, root / "build/dex-paired-arm64/mgmem_dex_paired",
                 root / "build/dex-locked/mgmem_dex_paired.exe"):
        binaries.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
                         "sha256": sha(path)})
    result = {"schema_version": 1, "scope": "ARM64_CPU_SAFETY_AND_TWO_DISTINCT_PAIRED_ATTEMPTS",
              "integrity_check_scope": "PROVENANCE_ONLY_NOT_PERFORMANCE_QUALIFICATION",
              "integrity_checks_passed": True, "arm64_safety": "PASS", "performance": "INCONCLUSIVE",
              "BIL_integration": "NOT_SELECTED", "device_window": "RELEASED",
              "sdk_source_commit": "58ed07f54199a1217ddef47735d4d297357654f4",
              "benchmark_native_source_commit": "58765309752321c52dfeabb2f09b33dc998ca3a8",
              "collector_first_commit": "58765309752321c52dfeabb2f09b33dc998ca3a8",
              "collector_retry_commit": "601550c539b586a0a8e09e735e8ea5e047c92075",
              "datasets": datasets, "binary_artifacts": binaries, "files": files,
              "private_inputs": "Original APK/DEX and full device inventory are not published.",
              "raw_logs": "Filtered exact per-command streams are preserved in command-logs.zip; local unarchived duplicates remain in ignored commands directories.",
              "limits": ["Physical device/API37/arm64/4KB only", "No confirmed stable CPU performance gain",
                         "No BIL production integration", "16KB/MTE/HWASan/energy/RSS/PFN/PMU are separate unqualified fields"]}
    target.write_bytes((json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({"integrity_checks_passed": True, "files": len(files), "datasets": datasets,
                      "manifest": target.relative_to(root).as_posix()}))


if __name__ == "__main__":
    main()
