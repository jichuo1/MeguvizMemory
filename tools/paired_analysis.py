#!/usr/bin/env python3
"""Plan and analyze whole-block A1/A3 sessions using only the standard library.

Plan: --pilot pilot.jsonl --plan plan.json [--expected-input-sha256 SHA]
Report: --formal formal.jsonl --plan plan.json --output report.json
No device operations, sampling, input mutation, timing selection or BIL enablement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import statistics


ORDERS = (("A1", "A3", "A3", "A1"), ("A3", "A1", "A1", "A3"))
DEFAULT_SEED = 20261030
RESAMPLES = 20000
EXPECTED_PILOT_BLOCKS = 6
MIN_BLOCKS, MAX_BLOCKS = 32, 96
HALF_WIDTH = 0.01


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finite_positive(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 < value <= (1 << 64) - 1
    return isinstance(value, float) and math.isfinite(value) and value > 0


def unsigned_integer(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def field(native: dict, *names):
    for name in names:
        if name in native:
            return native[name]
    return None


def load_rows(path: Path, phase: str):
    rows, errors = [], []
    for line_number, text in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not text.strip():
            continue
        try:
            row = json.loads(text)
            if not isinstance(row, dict):
                raise ValueError("row is not an object")
        except (json.JSONDecodeError, ValueError) as failure:
            errors.append({"line": line_number, "reason": "invalid_json_row", "detail": str(failure)})
            continue
        item = {"line": line_number, "row": row, "reasons": []}
        if row.get("phase") != phase:
            item["reasons"].append("wrong_or_missing_phase")
        if not unsigned_integer(row.get("block_id")):
            item["reasons"].append("invalid_block_id")
        if not unsigned_integer(row.get("position")) or row.get("position", 4) > 3:
            item["reasons"].append("invalid_position")
        if row.get("variant") not in ("A1", "A3"):
            item["reasons"].append("invalid_variant")
        if row.get("success") is not True:
            item["reasons"].append("run_not_successful")
        if row.get("valid_environment") is not True:
            item["reasons"].append("environment_not_explicitly_valid")
        native = row.get("native")
        if not isinstance(native, dict):
            native = {}
            item["reasons"].append("missing_native_result")
        if not unsigned_integer(native.get("total_ns")) or not finite_positive(native.get("total_ns")):
            item["reasons"].append("invalid_total_ns")
        if "success" in native and native["success"] is not True:
            item["reasons"].append("native_not_successful")
        source_hash = field(native, "source_sha256", "input_sha256")
        if not (isinstance(source_hash, str) and len(source_hash) == 64 and
                all(char in "0123456789abcdefABCDEF" for char in source_hash)):
            item["reasons"].append("missing_or_invalid_input_hash")
        checksum = field(native, "result_checksum", "checksum")
        method_count = field(native, "method_count", "method_def_count")
        if not unsigned_integer(checksum):
            item["reasons"].append("missing_or_invalid_checksum")
        if not unsigned_integer(method_count) or method_count == 0:
            item["reasons"].append("missing_or_invalid_method_count")
        if not unsigned_integer(native.get("closed_backing_bytes")) or native.get("closed_backing_bytes") != 0:
            item["reasons"].append("missing_or_nonzero_closed_backing")
        item["native"] = native
        item["identity"] = {"input_sha256": source_hash.lower() if isinstance(source_hash, str) else source_hash,
                            "result_checksum": checksum, "method_count": method_count}
        rows.append(item)
    return rows, errors


def group_blocks(items, expected_identity=None, expected_hash=None, planned=None):
    groups, blocks, problems = {}, [], []
    for item in items:
        block_id = item["row"].get("block_id")
        if unsigned_integer(block_id):
            groups.setdefault(block_id, []).append(item)
        else:
            problems.append({"line": item["line"], "reason": "unassigned_row"})
    identity = expected_identity
    for block_id in sorted(groups):
        members = groups[block_id]
        reasons = []
        positions = [item["row"].get("position") for item in members]
        if len(members) != 4 or sorted(positions, key=lambda x: (str(type(x)), str(x))) != [0, 1, 2, 3]:
            reasons.append("block_requires_exactly_one_run_at_each_position")
        ordered = sorted(members, key=lambda item: item["row"].get("position") if unsigned_integer(item["row"].get("position")) else 99)
        order = tuple(item["row"].get("variant") for item in ordered)
        if order not in ORDERS:
            reasons.append("order_is_not_ABBA_or_BAAB")
        if planned is not None:
            expected = planned.get(block_id)
            if expected is None:
                reasons.append("unplanned_block")
            elif order != tuple(expected):
                reasons.append("order_differs_from_frozen_plan")
        for item in members:
            reasons.extend("line_" + str(item["line"]) + ":" + why for why in item["reasons"])
            if expected_hash is not None and item["identity"]["input_sha256"] != expected_hash.lower():
                reasons.append("line_" + str(item["line"]) + ":input_differs_from_expected_hash")
            if not item["reasons"]:
                if identity is None:
                    identity = item["identity"]
                if item["identity"] != identity:
                    reasons.append("line_" + str(item["line"]) + ":semantic_identity_mismatch")
        times = {variant: [item["native"].get("total_ns") for item in ordered
                           if item["row"].get("variant") == variant] for variant in ("A1", "A3")}
        can_describe = (order in ORDERS and all(len(value) == 2 and all(finite_positive(t) for t in value)
                                              for value in times.values()))
        block = {"block_id": block_id, "order": list(order), "valid": not reasons,
                 "reasons": sorted(set(reasons)), "lines": [item["line"] for item in ordered],
                 "times_ns": times, "descriptive_log_ratio": None,
                 "arithmetic_time_ratio": None, "backing_peak_ratio": None}
        if can_describe:
            block["descriptive_log_ratio"] = statistics.mean(math.log(t) for t in times["A3"]) - statistics.mean(math.log(t) for t in times["A1"])
            block["arithmetic_time_ratio"] = statistics.mean(times["A3"]) / statistics.mean(times["A1"])
            peaks = {v: [field(item["native"], "peak_backing_bytes", "backing_peak_bytes") for item in ordered
                         if item["row"].get("variant") == v] for v in ("A1", "A3")}
            if all(len(values) == 2 and all(finite_positive(value) for value in values) for values in peaks.values()):
                block["backing_peak_ratio"] = max(peaks["A3"]) / max(peaks["A1"])
        blocks.append(block)
    return blocks, identity, problems


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def make_plan(args):
    items, parse_errors = load_rows(args.pilot, "pilot")
    blocks, identity, group_errors = group_blocks(items, expected_hash=args.expected_input_sha256)
    block_ids = [block["block_id"] for block in blocks]
    order_counts = {",".join(order): sum(tuple(block["order"]) == order for block in blocks) for order in ORDERS}
    valid = (not parse_errors and not group_errors and len(items) == EXPECTED_PILOT_BLOCKS * 4 and
             block_ids == list(range(EXPECTED_PILOT_BLOCKS)) and all(block["valid"] for block in blocks) and
             len(set(order_counts.values())) == 1)
    differences = [block["descriptive_log_ratio"] for block in blocks if block["valid"]]
    sd = statistics.stdev(differences) if len(differences) > 1 else None
    required = math.ceil((1.96 * sd / HALF_WIDTH) ** 2) if sd is not None else None
    count = min(MAX_BLOCKS, max(MIN_BLOCKS, required)) if valid else None
    if count is not None and count % 2:
        count += 1
    rng = random.Random(args.seed)
    orders = [ORDERS[0]] * (count // 2) + [ORDERS[1]] * (count // 2) if count is not None else []
    rng.shuffle(orders)
    plan = {
        "schema_version": 1, "protocol_id": "MM-003-MM-012-arm64-native-parser-A1-A3-v1",
        "state": "FROZEN_SAMPLE_SIZE_AND_ORDER_FROM_PILOT" if valid else "PILOT_INVALID_NOT_FROZEN",
        "scope": "INDEPENDENT_NATIVE_CPU_WORKLOAD_ONLY_NO_AUTOMATIC_BIL_ENABLEMENT",
        "pilot_file_sha256": sha256(args.pilot), "analysis_tool_sha256": sha256(Path(__file__)),
        "semantic_identity": identity, "pilot_blocks_expected": EXPECTED_PILOT_BLOCKS,
        "pilot_blocks_observed": len(blocks), "pilot_order_counts": order_counts,
        "pilot_valid": valid, "pilot_log_ratio_sd": sd, "required_blocks_unclipped": required,
        "formal_blocks": count, "minimum_blocks": MIN_BLOCKS, "maximum_blocks": MAX_BLOCKS,
        "target_log_ratio_95_half_width": HALF_WIDTH,
        "precision_unmet": bool(required is not None and required > MAX_BLOCKS),
        "sample_size_rule": "ceil((1.96*pilot_sd/0.01)^2), clamp to 32..96 then round up to an even count; planning approximation, not guaranteed precision.",
        "planned_blocks": [{"block_id": index, "order": list(order)} for index, order in enumerate(orders)],
        "analysis": {"order_seed": args.seed, "bootstrap_seed": args.seed, "bootstrap_resamples": RESAMPLES,
                     "lag1_abs_warning_threshold": 0.5, "half_log_ratio_shift_warning_threshold": 0.01,
                     "dependence_warning_blocks_adoption": True, "minimum_point_gain": 0.03,
                     "maximum_owned_backing_ratio": 1.05},
        "pilot_parse_errors": parse_errors, "pilot_group_errors": group_errors, "pilot_blocks": blocks,
        "rules": ["Pilot samples are never pooled into the formal estimator.",
                  "Stop at frozen N or the parent protocol's cap; no favorable early stop or post-result N change.",
                  "The parent must freeze device/workload/artifact/environment eligibility before formal launch.",
                  "All missing, failed, semantic-mismatch and environmental-violation runs remain in the report and prevent qualification.",
                  "16KB, MTE, energy, BIL production equivalence and non-target regressions remain separate gates."]}
    write_json(args.plan, plan)
    print(json.dumps({"state": plan["state"], "formal_blocks": count,
                      "precision_unmet": plan["precision_unmet"], "plan": str(args.plan)}))


def percentile(sorted_values, fraction):
    position = (len(sorted_values) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def describe(differences, seed, resamples):
    if not differences:
        return None
    mean = statistics.mean(differences)
    result = {"blocks": len(differences), "mean_log_ratio": mean, "time_ratio": math.exp(mean),
              "gain": 1 - math.exp(mean), "gain_95_percentile_interval": None,
              "log_ratio_95_percentile_interval": None, "gain_interval_half_width": None}
    if len(differences) > 1:
        rng = random.Random(seed)
        samples = sorted(statistics.mean(rng.choices(differences, k=len(differences))) for _ in range(resamples))
        low, high = percentile(samples, 0.025), percentile(samples, 0.975)
        interval = [1 - math.exp(high), 1 - math.exp(low)]
        result.update({"log_ratio_95_percentile_interval": [low, high],
                       "gain_95_percentile_interval": interval,
                       "gain_interval_half_width": (interval[1] - interval[0]) / 2})
    return result


def serial_diagnostics(values, settings):
    lag = None
    if len(values) >= 3:
        first, second = values[:-1], values[1:]
        m1, m2 = statistics.mean(first), statistics.mean(second)
        denominator = math.sqrt(sum((x - m1) ** 2 for x in first) * sum((x - m2) ** 2 for x in second))
        if denominator > 0:
            lag = sum((x - m1) * (y - m2) for x, y in zip(first, second)) / denominator
    half = len(values) // 2
    shift = statistics.mean(values[half:]) - statistics.mean(values[:half]) if half else None
    warnings = []
    if lag is not None and abs(lag) > settings["lag1_abs_warning_threshold"]:
        warnings.append("absolute_lag1_above_frozen_warning_threshold")
    if shift is not None and abs(shift) > settings["half_log_ratio_shift_warning_threshold"]:
        warnings.append("first_to_second_half_shift_above_frozen_warning_threshold")
    return {"lag1_correlation": lag, "second_minus_first_half_mean_log_ratio": shift,
            "warning_thresholds": {key: settings[key] for key in ("lag1_abs_warning_threshold", "half_log_ratio_shift_warning_threshold")},
            "warnings": warnings,
            "limitation": "Exploratory diagnostics do not prove independence when no warning fires."}


def analyze(args):
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    planned_entries = plan.get("planned_blocks", [])
    planned = {entry["block_id"]: entry["order"] for entry in planned_entries}
    settings = plan["analysis"]
    items, parse_errors = load_rows(args.formal, "formal")
    blocks, identity, group_errors = group_blocks(items, expected_identity=plan.get("semantic_identity"), planned=planned)
    observed = {block["block_id"] for block in blocks}
    missing = sorted(set(planned) - observed)
    extra = sorted(observed - set(planned))
    valid_blocks = [block for block in blocks if block["valid"] and block["block_id"] in planned]
    valid_values = [block["descriptive_log_ratio"] for block in valid_blocks]
    descriptive_values = [block["descriptive_log_ratio"] for block in blocks if block["descriptive_log_ratio"] is not None]
    expected_n = plan.get("formal_blocks")
    complete = (plan.get("state") == "FROZEN_SAMPLE_SIZE_AND_ORDER_FROM_PILOT" and
                isinstance(expected_n, int) and MIN_BLOCKS <= expected_n <= MAX_BLOCKS and
                len(planned_entries) == len(planned) == expected_n and not missing and not extra and
                not parse_errors and not group_errors and len(items) == 4 * expected_n and
                len(valid_blocks) == expected_n)
    estimate = describe(valid_values, settings["bootstrap_seed"], settings["bootstrap_resamples"])
    all_descriptive = describe(descriptive_values, settings["bootstrap_seed"], settings["bootstrap_resamples"])
    serial = serial_diagnostics(valid_values, settings)
    peak_ratios = [block["backing_peak_ratio"] for block in valid_blocks]
    peak_complete = bool(peak_ratios) and all(value is not None for value in peak_ratios)
    peak_max = max(peak_ratios) if peak_complete else None
    decision, reasons = "INCONCLUSIVE", []
    if not complete:
        reasons.append("formal_plan_incomplete_or_run_semantics_environment_failed")
    if plan.get("precision_unmet"):
        reasons.append("pilot_required_sample_count_exceeded_frozen_cap")
    if serial["warnings"]:
        reasons.append("serial_dependence_or_time_trend_requires_separate_validation")
    if not peak_complete:
        reasons.append("owned_backing_regression_gate_unavailable")
    analysis_valid = complete and not reasons
    if complete and not reasons and estimate and estimate["gain_95_percentile_interval"]:
        low, high = estimate["gain_95_percentile_interval"]
        if peak_max > settings["maximum_owned_backing_ratio"]:
            decision = "REGRESSION"
            reasons.append("owned_backing_peak_exceeds_frozen_regression_budget")
        elif high < 0:
            decision = "REGRESSION"
            reasons.append("paired_gain_interval_is_entirely_negative")
        elif estimate["gain"] >= settings["minimum_point_gain"] and low > 0:
            decision = "ELIGIBLE_ONLY_FOR_INDEPENDENT_WORKLOAD"
            reasons.append("prespecified_independent_workload_time_and_backing_gates_met")
        elif high < settings["minimum_point_gain"]:
            decision = "NO_STABLE_NET_GAIN"
            reasons.append("gain_interval_does_not_reach_prespecified_engineering_threshold")
        else:
            reasons.append("gain_or_precision_does_not_establish_prespecified_gate")
    failures = [{"line": item["line"], "block_id": item["row"].get("block_id"),
                 "position": item["row"].get("position"), "variant": item["row"].get("variant"),
                 "reasons": item["reasons"]} for item in items if item["reasons"]]
    report = {
        "schema_version": 1, "scope": "REAL_NATIVE_CPU_TASK_A1_A3_ONLY",
        "formal_file_sha256": sha256(args.formal), "plan_sha256": sha256(args.plan),
        "analysis_tool_sha256": sha256(Path(__file__)), "pilot_file_sha256": plan.get("pilot_file_sha256"),
        "decision": decision, "decision_reasons": reasons, "planned_protocol_complete": complete,
        "BIL_auto_enable": False, "BIL_decision": "KEEP_CURRENT_PATH_PENDING_SEPARATE_PRODUCT_EQUIVALENCE_AND_INTEGRATION_GATES",
        "semantic_identity": identity, "formal_blocks_planned": expected_n, "formal_blocks_observed": len(blocks),
        "formal_blocks_valid": len(valid_blocks), "attempted_rows": len(items), "missing_blocks": missing,
        "extra_blocks": extra, "parse_errors": parse_errors, "group_errors": group_errors,
        "failed_or_environment_invalid_rows": failures, "precision_unmet": plan.get("precision_unmet"),
        "primary_estimator": "1-exp(mean(block mean(log(A3))-mean(log(A1))))",
        "paired_estimate": estimate,
        "estimate_scope": "CONFIRMATORY_WITH_RECORDED_SCOPE_LIMITS" if analysis_valid else "DESCRIPTIVE_ONLY_QUALIFICATION_NOT_PASSED",
        "all_timed_blocks_descriptive_estimate": all_descriptive,
        "paired_arithmetic_mean_of_block_ratios": statistics.mean(block["arithmetic_time_ratio"] for block in valid_blocks) if valid_blocks else None,
        "owned_backing_peak_ratio_max": peak_max, "serial_diagnostics": serial,
        "bootstrap": {"unit": "WHOLE_FOUR_SESSION_BLOCK", "resamples": settings["bootstrap_resamples"],
                      "seed": settings["bootstrap_seed"], "interval": "TWO_SIDED_95_PERCENTILE"},
        "blocks": blocks,
        "limitations": ["All raw attempts remain in the input JSONL bound by its SHA; no timing-selected samples are removed.",
                        "Invalid/incomplete blocks are explicitly reported; their describable timings are secondary only and cannot pass qualification.",
                        "No precise p95/p99, energy, RSS, physical-contiguity or hardware mechanism claim follows from this analysis.",
                        "16KB/MTE/non-target/BIL end-to-end and family integration gates require separate evidence."]}
    write_json(args.output, report)
    print(json.dumps({"decision": decision, "formal_blocks_valid": len(valid_blocks),
                      "formal_blocks_planned": expected_n, "output": str(args.output)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pilot", type=Path)
    source.add_argument("--formal", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--expected-input-sha256")
    args = parser.parse_args()
    if args.formal and args.output is None:
        parser.error("--formal requires --output")
    if args.pilot:
        make_plan(args)
    else:
        analyze(args)


if __name__ == "__main__":
    main()
