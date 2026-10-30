"""Qualify the independent DEX external-import consumer; never run adb or performance sampling."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
INPUT_HASH = "83ac99bc338e8bfc1980e382a4c318871cac0364dc449ae7b0316f601c136ab0"
INPUT_BYTES = 10540184
METHODS = 54147
CHECKSUM = 12701547145945416225
CASES = {"direct": ("normal", "cross-thread", "corrupt", "borrow-budget"),
         "import": ("normal", "cross-thread", "soft-budget", "duplicate", "import-budget", "foreign-cookie", "corrupt", "borrow-budget")}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, default=ROOT / "build/continuation-20261030/dex-host/mgmem_dex_import_consumer.exe")
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / "install/features-host/bin")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--sdk", type=Path, default=ROOT / "build/locked-inputs/DexMeguviz")
    parser.add_argument("--source-lock", type=Path, default=ROOT / "SOURCE_LOCK.json")
    parser.add_argument("--stability-cycles", type=int, default=4, help="1..65536 minimum cycles per variant, in one process each")
    parser.add_argument("--stability-min-seconds", type=int, default=0, help="0..900 actual work per variant; never sleep to fill time")
    parser.add_argument("--wall-seconds", type=int, default=1200, help="1..1200 new-cycle admission cap; current full task finishes")
    parser.add_argument("--receipt", type=Path, default=ROOT / "artifacts/continuation-20261030/dex/host-receipt.json")
    args = parser.parse_args()
    assert 1 <= args.stability_cycles <= 65536, "stability cycles must be 1..65536"
    assert 0 <= args.stability_min_seconds <= 900 and 1 <= args.wall_seconds <= 1200
    assert args.stability_min_seconds <= args.wall_seconds
    assert args.input.stat().st_size == INPUT_BYTES and digest(args.input) == INPUT_HASH, "wrong real input"
    lock = json.loads(args.source_lock.read_text(encoding="utf-8"))
    entries = [entry for entry in lock["files"] if entry["root"] == "DexMeguviz"]
    assert len(entries) == 65, "expected frozen 65-file SDK closure"
    checked = []
    for entry in entries:
        actual = digest(args.sdk / entry["path"])
        assert actual == entry["sha256"], f"frozen SDK drift: {entry['path']}"
        checked.append({"path": entry["path"], "sha256": actual})
    assert args.exe.is_file()
    runtime_name = "mgmem.dll" if os.name == "nt" else "libmgmem.so"
    runtime = args.runtime_dir / runtime_name
    assert runtime.is_file(), "installed runtime missing"
    assert not (args.exe.parent / runtime_name).exists(), "executable-local runtime would shadow selected installed library"
    env = dict(os.environ)
    env["PATH" if os.name == "nt" else "LD_LIBRARY_PATH"] = str(args.runtime_dir.resolve()) + os.pathsep + env.get("PATH" if os.name == "nt" else "LD_LIBRARY_PATH", "")
    runs = []
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def execute(variant: str, case: str, cycles: int, phase: str, min_seconds: int = 0) -> None:
        result = subprocess.run([str(args.exe.resolve()), str(args.input.resolve()), "--variant", variant,
                                 "--case", case, "--cycles", str(cycles), "--min-duration-ms", str(1000 * min_seconds),
                                 "--wall-ms", str(1000 * args.wall_seconds)], env=env, capture_output=True,
                                text=True, timeout=args.wall_seconds + 60)
        raw_path = args.receipt.parent / f"{args.receipt.stem}-{phase}-{variant}-{case}.json"
        raw_path.write_text(json.dumps({"exit_code": result.returncode, "stdout": result.stdout,
                                        "stderr": result.stderr}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert result.returncode == 0 and not result.stderr.strip(), (variant, case, result.returncode, result.stdout, result.stderr)
        payload = json.loads(result.stdout)
        assert payload["success"] is True and payload["state"] == "PASS" and payload["cycles_completed"] >= cycles
        assert payload["cycles_completed"] == len(payload["iterations"]) and payload["actual_elapsed_ms"] >= 1000 * min_seconds
        assert payload["variant"] == variant and payload["case"] == case
        assert payload["performance_qualification"] == "NOT_MEASURED"
        for row in payload["iterations"]:
            assert row["input_bytes"] == INPUT_BYTES and row["source_capacity_bytes"] >= INPUT_BYTES
            assert row["source_plus_input_unique_capacity_bytes"] == row["source_capacity_bytes"]
            assert row["extra_input_backing_bytes"] == row["copy_bytes"] == row["provider_published_count"] == 0
            expected_charge = row["source_capacity_bytes"] if variant == "import" else 0
            assert row["memory_backing_peak_bytes"] == row["imported_backing_subset_bytes"] == expected_charge
            assert row["origin_retains"] == (1 if variant == "import" else 0)
            assert row["origin_releases_including_caller"] == row["origin_retains"] + 1
            assert row["SDK_releases_including_caller"] == row["SDK_retains"] + 1
            assert row["source_freed"] is True and row["final_resources"] == "ZERO"
            assert row["memory_domain"] == ("CLOSED_ZERO" if variant == "import" else None)
            assert all(row[name] is None for name in ("memory_metadata_bytes", "rss_pss", "energy"))
            if case not in ("corrupt", "borrow-budget"):
                assert row["source_sha256"] == INPUT_HASH and row["method_count"] == METHODS and row["result_checksum"] == CHECKSUM
            else:
                assert row["source_sha256"] is None and row["method_count"] == row["result_checksum"] == 0
            if case in ("cross-thread", "soft-budget"):
                assert row["last_release_on_worker"] is True
        runs.append({"phase": phase, "variant": variant, "case": case, "cycles": payload["cycles_completed"], "native": payload})
        print(json.dumps({"phase": phase, "variant": variant, "case": case, "cycles": payload["cycles_completed"],
                          "duration_ms": payload["run_duration_ms"], "state": "PASS"}), flush=True)

    for variant, cases in CASES.items():
        for case in cases:
            execute(variant, case, 1, "functional")
    for variant in CASES:
        execute(variant, "cross-thread", args.stability_cycles, "bounded_stability", args.stability_min_seconds)
    execute("import", "cross-thread", 1, "minimum_duration_gate", 1)
    wall_stop = subprocess.run([str(args.exe.resolve()), str(args.input.resolve()), "--variant", "import",
                                "--case", "cross-thread", "--cycles", "65536", "--wall-ms", "1"],
                               env=env, capture_output=True, text=True, timeout=60)
    wall_payload = json.loads(wall_stop.stdout)
    (args.receipt.parent / f"{args.receipt.stem}-wall-stop.json").write_text(
        json.dumps({"exit_code": wall_stop.returncode, "native": wall_payload, "stderr": wall_stop.stderr},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert wall_stop.returncode == 2 and not wall_stop.stderr.strip()
    assert wall_payload["state"] == "INCOMPLETE" and wall_payload["success"] is False
    assert wall_payload["cycles_completed"] < 65536 and wall_payload["stop_reason"] == "WALL_BEFORE_REQUIRED_WORK"
    assert all(row["source_freed"] and row["final_resources"] == "ZERO" for row in wall_payload["iterations"])
    rejected = []
    for option in (("--cycles", "0"), ("--cycles", "65537"), ("--case", "unknown"),
                   ("--min-duration-ms", "900001"), ("--wall-ms", "1200001")):
        result = subprocess.run([str(args.exe.resolve()), str(args.input.resolve()), "--variant", "import", *option],
                                env=env, capture_output=True, text=True, timeout=10)
        payload = json.loads(result.stdout)
        assert result.returncode == 1 and payload["success"] is False
        rejected.append({"option": list(option), "native": payload})
    files = ("consumers/dex/import_consumer.cpp", "consumers/dex/CMakeLists.txt", "tools/verify_dex_import.py")
    receipt = {"schema_version": 1, "state": "PASS_HOST_FUNCTIONAL_AND_BOUNDED_STABILITY",
               "device_executed": False, "performance_qualification": "NOT_MEASURED", "BIL": "NOT_MODIFIED_NOT_INTEGRATED",
               "input": {"bytes": INPUT_BYTES, "sha256": INPUT_HASH, "publication": "HASH_ONLY"},
               "binary": {"name": args.exe.name, "sha256": digest(args.exe)},
               "installed_runtime": {"name": runtime.name, "sha256": digest(runtime)},
               "source_identity": [{"path": path, "sha256": digest(ROOT / path)} for path in files],
               "source_lock_sha256": digest(args.source_lock), "frozen_SDK_files": checked,
               "functional_cases": sum(len(cases) for cases in CASES.values()),
               "stability_minimum_cycles_per_variant": args.stability_cycles,
               "stability_minimum_duration_seconds_per_variant": args.stability_min_seconds,
               "stability_completed_owner_tasks": sum(row["cycles"] for row in runs if row["phase"] == "bounded_stability"),
               "wall_duration_seconds": time.monotonic() - started, "runs": runs, "wall_stop_gate": wall_payload,
               "invalid_options_rejected": rejected,
               "resource_result": {"both_variants_copy_bytes": 0, "both_variants_extra_input_backing_bytes": 0,
                                   "imported_charge_is_source_capacity_subset": True,
                                   "attribution": "BORROW_REMOVES_COPY_IN_BOTH_MEMORY_ADDS_ACCOUNTING_AND_LIFETIME_NO_NEW_SAVING_CLAIM",
                                   "not_measured": ["speedup", "RSS/PSS savings", "energy", "TLB", "24h stability"]}}
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"state": receipt["state"], "functional_cases": receipt["functional_cases"],
                      "stability_owner_tasks": receipt["stability_completed_owner_tasks"],
                      "duration_seconds": receipt["wall_duration_seconds"]}), flush=True)


if __name__ == "__main__":
    main()
