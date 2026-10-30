#!/usr/bin/env python3
"""Independently recompute historical retry1 without rewriting its evidence.

Reads JSON/ZIP data only. No device, network, imported kit code or product code is
executed. Optional kit manifest checks verify every supplied non-manifest member.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import random
import statistics as st
import zipfile


BASE = Path("artifacts/arm64-20261030/paired-retry1")
IDENTITY = {"source_sha256": "83ac99bc338e8bfc1980e382a4c318871cac0364dc449ae7b0316f601c136ab0",
            "input_bytes": 10540184, "method_count": 54147,
            "result_checksum": 12701547145945416225, "closed_backing_bytes": 0,
            "lease_final_correct": True, "cpu_before": 2, "cpu_after": 2,
            "backing_peak_bytes": 10540184, "diagnostic": False}
PRESERVED = [BASE / name for name in ("formal.jsonl", "formal-plan.json", "report.json")]
PRESERVED += [Path(name) for name in ("consumers/dex/paired.cpp", "tools/run_paired_device.py",
                                    "tools/paired_analysis.py", "bench/paired_protocol_retry1.json")]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def quantile(values, fraction):
    index = (len(values) - 1) * fraction
    low, high = math.floor(index), math.ceil(index)
    return values[low] + (values[high] - values[low]) * (index - low)


def recompute(root):
    report = json.loads((root / BASE / "report.json").read_text(encoding="utf-8"))
    plan = json.loads((root / BASE / "formal-plan.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (root / BASE / "formal.jsonl").read_text(encoding="utf-8").splitlines()]
    require(digest((root / BASE / "formal.jsonl").read_bytes()) == report["formal_file_sha256"], "raw hash mismatch")
    require(digest((root / BASE / "formal-plan.json").read_bytes()) == report["plan_sha256"], "plan hash mismatch")
    require(len(rows) == 128 and len(plan["planned_blocks"]) == 32, "wrong block/task count")
    require([(r["block_id"], r["position"]) for r in rows] ==
            [(b["block_id"], p) for b in plan["planned_blocks"] for p in range(4)], "missing/duplicate/reordered row")
    differences = []
    for block in plan["planned_blocks"]:
        group = [r for r in rows if r["block_id"] == block["block_id"]]
        require([r["variant"] for r in group] == block["order"], "order differs from plan")
        for row in group:
            require(row["success"] is True and row["valid_environment"] is True, "failed task or environment")
            native = row["native"]
            require(native["success"] is True, "native failed")
            require(all(type(native[k]) is type(v) and native[k] == v for k, v in IDENTITY.items()), "semantic/lifetime identity mismatch")
            require(all(native[k] is None for k in ("setup_ns", "query_ns", "cleanup_ns")), "formal phases unexpectedly populated")
            require(json.loads(row["command_result"]["stdout"]) == native, "stdout/native mismatch")
            require(type(native["total_ns"]) is int and native["total_ns"] > 0, "invalid elapsed")
        times = {v: [r["native"]["total_ns"] for r in group if r["variant"] == v] for v in ("A1", "A3")}
        differences.append(st.mean(map(math.log, times["A3"])) - st.mean(map(math.log, times["A1"])))
    rng = random.Random(20261030)
    bootstrap = sorted(st.mean(rng.choices(differences, k=32)) for _ in range(20000))
    low, high = quantile(bootstrap, .025), quantile(bootstrap, .975)
    mean = st.mean(differences)
    gain, interval = 1 - math.exp(mean), [1 - math.exp(high), 1 - math.exp(low)]
    shift = st.mean(differences[16:]) - st.mean(differences[:16])
    x, y = differences[:-1], differences[1:]
    mx, my = st.mean(x), st.mean(y)
    lag = sum((a-mx)*(b-my) for a, b in zip(x, y)) / math.sqrt(sum((a-mx)**2 for a in x) * sum((b-my)**2 for b in y))
    expected = report["paired_estimate"]
    require(abs(gain - expected["gain"]) < 1e-14, "gain recomputation differs")
    require(all(abs(a-b) < 1e-14 for a, b in zip(interval, expected["gain_95_percentile_interval"])), "CI differs")
    require(abs(shift - report["serial_diagnostics"]["second_minus_first_half_mean_log_ratio"]) < 1e-14, "trend differs")
    require(abs(lag - report["serial_diagnostics"]["lag1_correlation"]) < 1e-14, "lag differs")
    require(report["decision"] == "INCONCLUSIVE", "historical decision changed")
    return {"evidence_type": "OFFLINE_RECOMPUTATION_NOT_NEW_DEVICE_MEASUREMENT", "status": "PASS",
            "blocks": 32, "tasks": 128, "gain": gain, "exploratory_gain_interval": interval,
            "half_shift_log": shift, "lag1": lag, "historical_decision": report["decision"],
            "planned_precision": {"pilot_cap_unmet": plan["precision_unmet"], "is_achieved_precision": False},
            "achieved_precision": {"exploratory_log_interval_half_width": (high-low)/2,
                                   "target_log_half_width": .01, "target_met": (high-low)/2 <= .01,
                                   "does_not_override_trend_or_inference_limits": True},
            "input_reparse": "NOT_PERFORMED_BY_THIS_AUDIT", "device_execution": "NOT_PERFORMED",
            "independence": "NOT_ESTABLISHED_BY_BLOCK_COUNT_OR_LAG1", "BIL_adoption": "NOT_SELECTED"}


def check_kit(path, root):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "duplicate archive member")
        require(all(not PurePosixPath(n).is_absolute() and ".." not in PurePosixPath(n).parts and "\\" not in n
                    for n in names), "unsafe archive name")
        manifests = [n for n in names if n.endswith("/MANIFEST.json")]
        require(len(manifests) == 1, "missing or ambiguous kit manifest")
        prefix = manifests[0][:-len("MANIFEST.json")]
        manifest = json.loads(archive.read(manifests[0]))
        records = manifest["files"]
        require(len({r["path"] for r in records}) == len(records), "duplicate manifest path")
        expected = {manifests[0]}
        for record in records:
            name = prefix + record["path"]
            require(name in names, "missing kit member")
            data = archive.read(name)
            require(len(data) == record["bytes"] and digest(data) == record["sha256"], "kit member identity mismatch")
            expected.add(name)
        require({n for n in names if not n.endswith("/")} == expected, "unlisted kit members")
        matches = []
        for name, relative in [("evidence/fire/" + BASE.as_posix() + "/formal.jsonl", BASE / "formal.jsonl"),
                               ("evidence/fire/" + BASE.as_posix() + "/formal-plan.json", BASE / "formal-plan.json"),
                               ("evidence/source/" + BASE.as_posix() + "/report.json", BASE / "report.json")]:
            kit_data, repo_data = archive.read(prefix + name), (root / relative).read_bytes()
            byte_equal = kit_data == repo_data
            # The returned kit reformatted report.json (one-space indentation).
            # Raw JSONL and frozen plan must remain byte-identical. A report may
            # only differ as a JSON rendering, and both identities are retained.
            semantic_equal = json.loads(kit_data) == json.loads(repo_data) if relative.name == "report.json" else byte_equal
            require(semantic_equal, "kit/repository evidence differs")
            matches.append({"path": relative.as_posix(), "byte_equal": byte_equal,
                            "semantic_equal": semantic_equal, "kit_sha256": digest(kit_data),
                            "repository_sha256": digest(repo_data)})
        return {"bytes": path.stat().st_size, "sha256": digest(path.read_bytes()),
                "manifest_files_verified": len(records), "repository_evidence_matches": matches,
                "kit_code_executed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--research-kit", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    protected = {p: (root / p).read_bytes() for p in PRESERVED}
    output = recompute(root)
    kit = check_kit(args.research_kit, root) if args.research_kit else None
    require(all((root / p).read_bytes() == raw for p, raw in protected.items()), "historical evidence was modified")
    manifest = {"schema_version": 1, "scope": "HISTORICAL_RETRY1_OFFLINE_AUDIT", "kit": kit,
                "historical_files": [{"path": p.as_posix(), "bytes": len(raw), "sha256": digest(raw)} for p, raw in protected.items()],
                "old_results_rewritten": False, "device_used": False}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in [("independent_recompute.json", output), ("evidence_manifest.json", manifest)]:
        target = args.output_dir / name
        require(target.resolve() not in {(root / p).resolve() for p in PRESERVED}, "output overlaps history")
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"audit": output["status"], "historical_decision": output["historical_decision"],
                      "kit_files": kit["manifest_files_verified"] if kit else None}))


if __name__ == "__main__":
    main()
