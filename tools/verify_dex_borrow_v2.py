"""Host functional qualification of the frozen-source DEX strategy harness.

This records functional timings only. It never samples a performance claim,
uses adb, downloads an input, or changes a frozen SDK or old experiment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
INPUT_SHA256 = "83ac99bc338e8bfc1980e382a4c318871cac0364dc449ae7b0316f601c136ab0"
METHODS = 54147
CHECKSUM = 12701547145945416225
VARIANTS = ("D-C0", "D-Z0", "D-C1", "D-C2")
CASES = ("normal", "cross-thread", "invalid-lease", "corrupt-input", "borrow-budget", "query-closed", "cancel-job")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build/dex-borrow-v2")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source-lock", type=Path, default=ROOT / "SOURCE_LOCK.json")
    parser.add_argument("--sdk", type=Path, default=ROOT / "build/locked-inputs/DexMeguviz")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/research-20261030/dex")
    args = parser.parse_args()
    source_lock = json.loads(args.source_lock.read_text(encoding="utf-8"))
    sdk_files = [entry for entry in source_lock["files"] if entry["root"] == "DexMeguviz"]
    assert sdk_files, "no frozen SDK closure in source lock"
    checked = []
    for entry in sdk_files:
        file = args.sdk / entry["path"]
        checked.append({"path": entry["path"], "expected_sha256": entry["sha256"],
                        "actual_sha256": digest(file), "bytes": file.stat().st_size})
        assert checked[-1]["actual_sha256"] == entry["sha256"], f"frozen SDK drift: {entry['path']}"
    assert digest(args.input) == INPUT_SHA256 and args.input.stat().st_size == 10540184, "wrong real DEX input"
    sdk_identity = hashlib.sha256(json.dumps(checked, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    gate = {
        "schema_version": 1, "state": "PASS_FOR_SCOPED_FROZEN_SDK",
        "source_lock_sha256": digest(args.source_lock), "source_lock_source_set_sha256": source_lock["source_set_sha256"],
        "sdk_git_identity": "FROZEN_INPUT_WITHOUT_INDEPENDENT_GIT_ROOT", "frozen_file_count": len(checked),
        "verified_files": checked, "verified_closure_receipt_sha256": sdk_identity,
        "contract_review": "docs/DEX_BORROW_CONTRACT.md",
        "contract_scope": "raw DEX immutable source / complete synchronous method query / existing Job cancellation",
        "not_qualified": ["arbitrary SDK versions", "BIL DexKit", "device execution", "performance", "external Memory import"],
    }
    save(args.output / "borrow_contract_gate.json", gate)
    env = dict(os.environ)
    env["PATH"] = str(ROOT / "install/host/bin") + os.pathsep + env.get("PATH", "")
    suffix = ".exe" if os.name == "nt" else ""
    binaries = {name: args.build_dir / (name + suffix)
                for name in ("mgmem_dex_paired_v2", "mgmem_dex_paired_v2_diagnostic")}
    for binary in binaries.values():
        assert binary.is_file(), f"missing build: {binary.name}"
    runs = []
    for name, binary in binaries.items():
        for variant in VARIANTS:
            for case in CASES if name == "mgmem_dex_paired_v2" else ("normal",):
                command = [str(binary), str(args.input), "--variant", variant, "--case", case]
                result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
                assert result.returncode == 0, (binary.name, variant, case, result.returncode, result.stdout, result.stderr)
                assert not result.stderr.strip(), result.stderr
                payload = json.loads(result.stdout)
                checks = {
                    "success": payload["success"] is True,
                    "case": payload["case"] == case,
                    "variant": payload["variant"] == variant,
                    "source_final_released": payload["source_final_released"] is True,
                    "balanced_callbacks": payload["callback_releases_including_caller"] == payload["callback_retains"] + 1,
                    "lease_final_correct": payload["lease_final_correct"] is True,
                    "final_resources": payload["final_resource_state"] == "CLOSED_ZERO" and payload["closed_backing_bytes"] == 0,
                    "unknown_resources_not_zero": all(payload[key] is None for key in ("memory_metadata_bytes", "rss_pss_bytes", "energy", "provider_attempt_count")),
                    "source_capacity": payload["external_source_capacity_bytes"] >= 10540184,
                    "unique_input_backing": payload["unique_owned_backing_peak_bytes"] == (0 if variant == "D-Z0" else 10540184),
                    "retained": payload["retained_before_collect_bytes"] == (10540184 if variant == "D-C2" else 0),
                    "build_kind": payload["diagnostic"] == (name == "mgmem_dex_paired_v2_diagnostic"),
                    "phase_policy": (payload["phases"] is None) == (name == "mgmem_dex_paired_v2"),
                    "expected_rejection_or_completion": payload["expected_status"] == payload["observed_status"],
                }
                if case not in ("invalid-lease", "corrupt-input", "borrow-budget"):
                    checks["source_digest"] = payload["source_sha256"] == INPUT_SHA256
                    checks["methods"] = payload["method_count"] == METHODS
                if case in ("normal", "cross-thread"):
                    checks["all_method_and_text_checksum"] = payload["result_checksum"] == CHECKSUM
                    checks["result_outlives_snapshot"] = payload["snapshot_released_before_last_read"] is True
                if case in ("cross-thread", "cancel-job"):
                    checks["final_sdk_callback_on_other_thread"] = payload["final_release_on_other_thread"] is True
                assert all(checks.values()), (binary.name, variant, case, checks)
                runs.append({"binary": binary.name, "variant": variant, "case": case, "checks": checks,
                             "native": payload, "timing_use": "FUNCTIONAL_ONLY_NOT_PERFORMANCE_SAMPLES"})
    invalid = subprocess.run([str(binaries["mgmem_dex_paired_v2"]), str(args.input), "--variant", "D-Z1"],
                             capture_output=True, text=True, env=env, timeout=30)
    invalid_payload = json.loads(invalid.stdout)
    assert invalid.returncode == 1 and not invalid_payload["success"] and invalid_payload["final_resource_state"] == "UNQUALIFIED"
    sources = ("consumers/dex/paired_v2.cpp", "consumers/dex/CMakeLists.txt", "tools/verify_dex_borrow_v2.py")
    receipt = {
        "schema_version": 1, "state": "PASS_HOST_FUNCTIONAL_ONLY", "device_executed": False,
        "performance_qualification": "NOT_STARTED_NEW_PROTOCOL_NOT_FROZEN", "old_experiment_modified": False,
        "input": {"bytes": args.input.stat().st_size, "sha256": digest(args.input), "publication": "HASH_ONLY_INPUT_NOT_PUBLISHED"},
        "build": {"platform": "Windows-x86_64" if os.name == "nt" else os.name, "kind": "Release",
                  "executables": [{"name": binary.name, "sha256": digest(binary)} for binary in binaries.values()],
                  "memory_installed_library_sha256": digest(ROOT / "install/host/bin/mgmem.dll") if os.name == "nt" else None},
        "source_identity": [{"path": name, "sha256": digest(ROOT / name)} for name in sources],
        "contract_gate_sha256": digest(args.output / "borrow_contract_gate.json"),
        "checks_passed": len(runs), "checks_total": len(runs), "runs": runs,
        "unsupported_variant_rejected": invalid_payload,
        "resource_result": {"copy_source_plus_input_unique_capacity_bytes": next(row["native"]["source_plus_input_unique_capacity_bytes"] for row in runs if row["variant"] == "D-C0" and row["case"] == "normal"),
                            "borrow_source_unique_capacity_bytes": next(row["native"]["source_plus_input_unique_capacity_bytes"] for row in runs if row["variant"] == "D-Z0" and row["case"] == "normal"),
                            "removed_copy_destination_bytes": 10540184,
                            "attribution": "ORDINARY_CONSUMER_INPUT_STRATEGY_NOT_MEMORY_GAIN",
                            "not_measured": ["RSS/PSS", "energy", "DRAM traffic", "TLB", "elapsed benefit"]},
    }
    save(args.output / "functional_receipt.json", receipt)
    print(json.dumps({"state": receipt["state"], "passed": len(runs), "total": len(runs),
                      "frozen_sdk_files": len(sdk_files), "device_executed": False}))


if __name__ == "__main__":
    main()
