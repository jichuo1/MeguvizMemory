#!/usr/bin/env python3
"""Offline v2 window-level validation. No device operations or old-data pooling.

The draft cannot qualify performance. Its proposed t interval uses exactly 12
prescheduled window means, NOT its 384 tasks as independent observations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

T_975_DF11 = 2.200985160082949
ZERO_SHA256 = "0" * 64
ENV_FIELDS = ("battery_deci_c", "thermal_status", "background_cpu_busy",
              "busy_begin_monotonic_ns", "busy_end_monotonic_ns",
              "thermal_observed_monotonic_ns")
RESOURCE_FIELDS = ("logical_requested_bytes", "unique_owned_backing_bytes",
                   "external_source_capacity_bytes", "borrowed_view_bytes",
                   "arena_used_bytes", "padding_bytes", "retained_bytes",
                   "reserved_bytes", "metadata_bytes", "parser_internal_bytes",
                   "rss_pss_bytes", "energy_joules")
NATIVE_RESOURCE_FIELDS = {
    "logical_requested_bytes": "logical_requested_bytes",
    "unique_owned_backing_bytes": "unique_owned_backing_peak_bytes",
    "external_source_capacity_bytes": "external_source_capacity_bytes",
    "borrowed_view_bytes": "borrowed_view_peak_bytes",
    "arena_used_bytes": "arena_used_bytes",
    "padding_bytes": "input_backing_padding_bytes",
    "retained_bytes": "retained_before_collect_bytes",
    "reserved_bytes": "reserved_final_bytes",
    "metadata_bytes": "memory_metadata_bytes",
    "parser_internal_bytes": "parser_peak_owned_bytes",
    "rss_pss_bytes": "rss_pss_bytes",
    "energy_joules": "energy",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def integer(value, minimum=0):
    return not isinstance(value, bool) and isinstance(value, int) and value >= minimum


def number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def object_or_empty(value):
    return value if isinstance(value, dict) else {}


def native_to_resources(native):
    """Explicit v2 field mapping, with measurement scopes kept distinct."""
    resources, unavailable = {}, {}
    for target, source in NATIVE_RESOURCE_FIELDS.items():
        resources[target] = native.get(source)
        if resources[target] is None:
            unavailable[target] = ("NATIVE_REPORTS_UNAVAILABLE:" if source in native else "NATIVE_FIELD_MISSING:") + source
    resources["unavailable_reasons"] = unavailable
    resources["scopes"] = {"unique_owned_backing_bytes": "PEAK_INPUT_BACKING_EXCLUDES_SOURCE_AND_PARSER",
                           "external_source_capacity_bytes": "PRELOADED_SOURCE_CAPACITY",
                           "borrowed_view_bytes": "PEAK_NONOWNING_VIEW_NOT_ADDITIONAL_BACKING",
                           "retained_bytes": "BEFORE_COLLECT",
                           "reserved_bytes": "FINAL_AFTER_CLOSE",
                           "parser_internal_bytes": "PARSER_PEAK_OWNED_SEPARATE_BUDGET"}
    return resources


def strict_json(text):
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate_JSON_key:" + key)
            value[key] = item
        return value

    def invalid_constant(value):
        raise ValueError("nonfinite_JSON_constant:" + value)

    return json.loads(text, object_pairs_hook=object_pairs, parse_constant=invalid_constant)


def write_new_json(path, value):
    """Never replace a plan/report or raw capture from an earlier attempt."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def load_journal(path):
    """Validate a hash chain in physical line order. Never sort or repair rows."""
    records, errors, previous = [], [], ZERO_SHA256
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        try:
            if not line.strip():
                raise ValueError("blank_journal_line")
            event = strict_json(line)
            if not isinstance(event, dict) or not isinstance(event.get("record"), dict):
                raise ValueError("invalid_journal_event")
            payload = {key: item for key, item in event.items() if key != "event_sha256"}
            if event.get("sequence") != line_number or isinstance(event.get("sequence"), bool):
                raise ValueError("journal_sequence_mismatch")
            if event.get("previous_sha256") != previous:
                raise ValueError("journal_previous_hash_mismatch")
            if digest(event["record"]) != event.get("record_sha256"):
                raise ValueError("journal_record_hash_mismatch")
            if digest(payload) != event.get("event_sha256"):
                raise ValueError("journal_event_hash_mismatch")
            records.append(event["record"])
            previous = event["event_sha256"]
        except (ValueError, TypeError, OverflowError) as error:
            errors.append({"line": line_number, "reason": str(error)})
            # A broken chain is not recovered by accepting a later tail.
            break
    return records, errors


def write_new_journal(path, records):
    """Fixture/offline exchange writer; x mode, append-only within one writer."""
    import os
    previous = ZERO_SHA256
    with Path(path).open("x", encoding="utf-8", newline="\n") as output:
        for sequence, record in enumerate(records, 1):
            event = {"schema_version": 2, "sequence": sequence,
                     "previous_sha256": previous, "record_sha256": digest(record),
                     "record": record}
            event["event_sha256"] = digest(event)
            output.write(canonical(event).decode("utf-8") + "\n")
            output.flush()
            os.fsync(output.fileno())
            previous = event["event_sha256"]


def make_plan(protocol):
    """Create proposed balanced orders; this DOES NOT freeze a protocol."""
    rng = random.Random(protocol["sampling"]["order_seed"])
    variants = protocol["primary_contrast"]
    reference, candidate = variants[1], variants[0]
    orders = ((reference, candidate, candidate, reference),
              (candidate, reference, reference, candidate))
    entries = []
    for phase, windows in (("pilot", 4), ("formal", 12)):
        for window in range(windows):
            balanced = [orders[0]] * 4 + [orders[1]] * 4
            rng.shuffle(balanced)
            for block, order in enumerate(balanced):
                for position, variant in enumerate(order):
                    entries.append({"phase": phase, "window_id": phase + "-w%02d" % window,
                                    "session_id": phase + "-s%02d" % window,
                                    "block_id": block, "position": position, "variant": variant})
    return {"schema_version": 2, "state": "DRAFT_ORDER_ONLY_NOT_FROZEN_NOT_EXECUTED",
            "experiment_id": protocol["experiment_id"], "attempt_id": protocol["attempt_id"],
            "estimand_id": protocol["estimand_id"], "protocol_canonical_sha256": digest(protocol),
            "order_seed": protocol["sampling"]["order_seed"], "entries": entries,
            "rule": "One pilot only; no favorable stop, replacement rows or pooling. New identity for repairs."}


def freeze_errors(protocol, plan):
    errors = []
    if protocol.get("state") != "FROZEN_BEFORE_PILOT":
        errors.append("protocol_not_frozen_before_pilot")
    if plan.get("state") != "FROZEN_ORDER_BEFORE_PILOT":
        errors.append("order_not_frozen_before_pilot")
    if protocol.get("device_execution") != "LATER_SEPARATELY_AUTHORIZED":
        errors.append("device_execution_not_authorized_by_protocol")
    freeze = protocol.get("freeze", {})
    for field in ("device_profile", "artifact_closure", "functional_receipt_sha256",
                  "scheduled_windows", "single_task_timeout_seconds",
                  "session_wall_cap_seconds", "overall_wall_cap_seconds",
                  "maximum_environment_waits", "cadence", "resource_budgets"):
        if freeze.get(field) is None:
            errors.append("missing_freeze_field:" + field)
    if plan.get("protocol_canonical_sha256") != digest(protocol):
        errors.append("plan_protocol_canonical_hash_mismatch")
    return errors


def percentile(values, fraction):
    point = (len(values) - 1) * fraction
    lo, hi = math.floor(point), math.ceil(point)
    return values[lo] + (values[hi] - values[lo]) * (point - lo)


def window_estimate(values, seed=20261030):
    if not values:
        return None
    mean = statistics.mean(values)
    result = {"windows": len(values), "mean_log_ratio": mean,
              "time_ratio": math.exp(mean), "gain": -math.expm1(mean),
              "primary_log_interval": None, "primary_gain_interval": None,
              "achieved_log_half_width": None,
              "window_bootstrap_sensitivity_gain_interval": None}
    if len(values) == 12:
        half = T_975_DF11 * statistics.stdev(values) / math.sqrt(12)
        result.update({"primary_log_interval": [mean - half, mean + half],
                       "primary_gain_interval": [-math.expm1(mean + half), -math.expm1(mean - half)],
                       "achieved_log_half_width": half})
        rng = random.Random(seed)
        boot = sorted(statistics.mean(rng.choices(values, k=12)) for _ in range(10000))
        result["window_bootstrap_sensitivity_gain_interval"] = [
            -math.expm1(percentile(boot, 0.975)), -math.expm1(percentile(boot, 0.025))]
    return result


def analyze(protocol, plan, records, phase, journal_errors=None):
    """Normalize nothing: missing fields and order discrepancies are failures."""
    errors = list(journal_errors or [])
    functional, incomplete = [], []
    expected = [entry for entry in plan["entries"] if entry["phase"] == phase]
    phase_rows = [row for row in records if row.get("phase") == phase]
    other_phases = sorted({str(row.get("phase")) for row in records if row.get("phase") != phase})
    if other_phases:
        incomplete.append("journal_contains_other_phase_rows_no_pooling:" + ",".join(other_phases))
    if len(phase_rows) != len(expected):
        incomplete.append("task_count_differs_from_plan")
    ids = ("phase", "window_id", "session_id", "block_id", "position", "variant")
    seen, groups, cadence, last_launch = set(), {}, [], {}
    window_contexts = {}
    clock_epoch = None
    plan_hash = digest(plan)
    identity = protocol["semantic_identity"]
    last_host_end = None
    for index, row in enumerate(phase_rows):
        label = "task_%d:" % index
        if not integer(row.get("block_id")) or not integer(row.get("position")) or row.get("position", 4) > 3:
            incomplete.append(label + "invalid_block_or_position_integer")
        key = digest({field: row.get(field) for field in ids[:-1]})
        if key in seen:
            incomplete.append(label + "duplicate_task_identity")
        seen.add(key)
        if index >= len(expected) or any(row.get(field) != expected[index].get(field) for field in ids):
            incomplete.append(label + "physical_order_or_identity_differs_from_plan")
        for field in ("experiment_id", "attempt_id", "estimand_id"):
            if row.get(field) != protocol.get(field):
                functional.append(label + "wrong_" + field)
        for field in ("day_id", "environment_id", "host_clock_epoch_id"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                incomplete.append(label + "missing_hierarchy_or_clock_identity:" + field)
        context = (row.get("day_id"), row.get("environment_id"))
        window_key = str(row.get("window_id"))
        if window_key in window_contexts and context != window_contexts[window_key]:
            incomplete.append(label + "window_environment_identity_changed")
        window_contexts[window_key] = context
        if clock_epoch is not None and row.get("host_clock_epoch_id") != clock_epoch:
            incomplete.append(label + "host_clock_epoch_changed_do_not_compare_monotonic_clocks")
        clock_epoch = row.get("host_clock_epoch_id")
        if row.get("plan_canonical_sha256") != plan_hash:
            functional.append(label + "wrong_plan_canonical_hash")
        if row.get("success") is not True:
            functional.append(label + "task_not_successful")
        native = row.get("native")
        if not isinstance(native, dict):
            functional.append(label + "missing_native_result")
            native = {}
        if native.get("success") is not True:
            functional.append(label + "native_not_successful")
        command = object_or_empty(row.get("command_result"))
        try:
            if not isinstance(command.get("stdout"), str):
                raise ValueError("missing_stdout")
            raw_native = strict_json(command["stdout"])
            if canonical(raw_native) != canonical(native):
                raise ValueError("stdout_native_mismatch")
            if command.get("exit_code") != 0 or isinstance(command.get("exit_code"), bool):
                raise ValueError("command_exit_not_zero")
        except (ValueError, TypeError, OverflowError) as error:
            functional.append(label + "raw_command_binding_failed:" + str(error))
        if native.get("variant") != row.get("variant"):
            functional.append(label + "native_variant_differs_from_planned_variant")
        if native.get("input_bytes") != identity["input_bytes"] or not integer(native.get("input_bytes"), 1):
            functional.append(label + "native_input_bytes_mismatch")
        if native.get("diagnostic") is not False or native.get("phases") is not None:
            functional.append(label + "diagnostic_or_phase_instrumented_binary_in_sampling")
        if native.get("workload") != protocol["estimand_id"] or native.get("schema_version") != 2:
            functional.append(label + "wrong_native_workload_or_schema")
        if native.get("case") != "normal":
            functional.append(label + "functional_case_not_formal_workload")
        for field in ("source_sha256", "method_count", "result_checksum"):
            value = native.get(field)
            if value != identity[field] or isinstance(value, bool):
                functional.append(label + "semantic_identity_mismatch:" + field)
        if not integer(native.get("total_ns"), 1):
            functional.append(label + "invalid_total_ns")
        if (native.get("closed_backing_bytes") != 0 or isinstance(native.get("closed_backing_bytes"), bool)
                or native.get("source_final_released") is not True
                or native.get("lease_final_correct") is not True
                or native.get("final_resource_state") != "CLOSED_ZERO"):
            functional.append(label + "final_resource_contract_failed_or_missing")
        resources = object_or_empty(row.get("resources"))
        if resources != native_to_resources(native):
            functional.append(label + "resource_adapter_differs_from_raw_native_mapping")
        unavailable = object_or_empty(resources.get("unavailable_reasons"))
        for field in RESOURCE_FIELDS:
            value = resources.get(field)
            if value is None:
                if not isinstance(unavailable.get(field), str) or not unavailable[field].strip():
                    incomplete.append(label + "resource_missing_without_reason:" + field)
            elif not number(value) or value < 0 or (field != "energy_joules" and not integer(value)):
                functional.append(label + "invalid_resource_value:" + field)
        env, clocks = object_or_empty(row.get("environment")), object_or_empty(row.get("clocks"))
        if env.get("complete") is not True or env.get("valid") is not True:
            incomplete.append(label + "environment_not_explicitly_complete_and_valid")
        for field in ENV_FIELDS:
            valid_value = integer(env.get(field), 1) if field.endswith("_monotonic_ns") else number(env.get(field))
            if not valid_value:
                incomplete.append(label + "environment_missing_or_invalid:" + field)
        if all(number(env.get(field)) for field in ENV_FIELDS):
            limits = protocol["environment_limits"]
            if not (0 <= env["background_cpu_busy"] <= limits["background_cpu_busy_max"]
                    and env["battery_deci_c"] <= limits["battery_max_deci_c"]
                    and 0 <= env["thermal_status"] <= limits["thermal_status_max"]):
                incomplete.append(label + "environment_limit_exceeded")
            if not env["busy_begin_monotonic_ns"] < env["busy_end_monotonic_ns"]:
                incomplete.append(label + "invalid_actual_busy_observation_window")
        clock_fields = ("observation_begin_monotonic_ns", "observation_end_monotonic_ns",
                        "launch_monotonic_ns", "completion_monotonic_ns")
        if not all(integer(clocks.get(field), 1) for field in clock_fields):
            incomplete.append(label + "missing_or_invalid_monotonic_clock")
        else:
            begin, observed, launch, end = (clocks[field] for field in clock_fields)
            if not begin < observed <= launch < end:
                incomplete.append(label + "monotonic_event_order_invalid")
            if last_host_end is not None and begin < last_host_end:
                incomplete.append(label + "overlapping_or_reordered_tasks")
            last_host_end = end
            if all(number(env.get(field)) for field in ENV_FIELDS):
                if not (begin <= env["busy_begin_monotonic_ns"] < env["busy_end_monotonic_ns"] <= observed
                        and begin <= env["thermal_observed_monotonic_ns"] <= observed):
                    incomplete.append(label + "environment_timestamps_outside_observation")
            session = str(row.get("session_id"))
            gap = launch - last_launch[session] if session in last_launch else None
            cadence.append({"task_index": index, "session_id": session,
                            "observation_to_launch_ns": launch - observed,
                            "actual_busy_window_ns": (env["busy_end_monotonic_ns"] - env["busy_begin_monotonic_ns"])
                            if all(number(env.get(f)) for f in ("busy_end_monotonic_ns", "busy_begin_monotonic_ns")) else None,
                            "launch_interval_ns": gap})
            last_launch[session] = launch
        group_key = tuple(str(row.get(field)) for field in ("window_id", "session_id", "block_id"))
        groups.setdefault(group_key, []).append(row)
    blocks, windows = [], {}
    candidate, reference = protocol["primary_contrast"]
    for key, rows in groups.items():
        times = {v: [object_or_empty(r.get("native")).get("total_ns") for r in rows if r.get("variant") == v]
                 for v in (candidate, reference)}
        valid = len(rows) == 4 and all(len(ts) == 2 and all(integer(t, 1) for t in ts) for ts in times.values())
        value = (statistics.mean(math.log(t) for t in times[candidate]) -
                 statistics.mean(math.log(t) for t in times[reference])) if valid else None
        blocks.append({"window_id": key[0], "session_id": key[1], "block_id": key[2],
                       "log_ratio": value})
        if value is not None:
            windows.setdefault((key[0], key[1]), []).append(value)
    expected_windows = 4 if phase == "pilot" else 12
    window_values = [statistics.mean(values) for values in windows.values() if len(values) == 8]
    if len(window_values) != expected_windows:
        incomplete.append("window_count_or_eight_complete_blocks_contract_failed")
    estimate = window_estimate(window_values) if phase == "formal" else None
    pilot_half = (T_975_DF11 * statistics.stdev(window_values) / math.sqrt(12)) if phase == "pilot" and len(window_values) == 4 else None
    shift = (statistics.mean(window_values[6:]) - statistics.mean(window_values[:6])) if len(window_values) == 12 else None
    trend = shift is not None and abs(shift) > protocol["sampling"]["trend_gate_absolute_half_shift_log"]
    achieved = estimate and estimate["achieved_log_half_width"]
    frozen_errors = freeze_errors(protocol, plan)
    reasons = list(errors) + functional + incomplete
    if functional:
        decision = "STOP_CORRECTNESS_OR_LIFETIME_FAILURE"
    elif errors or incomplete:
        decision = "INCOMPLETE"
    elif phase == "pilot":
        decision = "PILOT_FEASIBILITY_ONLY" if pilot_half <= 0.01 else "PRECISION_INSUFFICIENT_STOP_PROTOCOL"
    else:
        decision = "INCONCLUSIVE"
    if trend:
        reasons.append("formal_half_window_trend_gate_failed")
    if phase == "formal" and achieved is not None and achieved > 0.01:
        reasons.append("formal_achieved_precision_target_not_met")
    # This revision deliberately does not award performance qualification. The
    # window-independence design and resource gates need review before freeze.
    reasons.extend(frozen_errors)
    reasons.append("outer_independence_not_proved_by_no_trend_or_fresh_process")
    return {"schema_version": 2, "scope": "OFFLINE_VALIDATOR_V2_NO_DEVICE_EXECUTION",
            "phase": phase, "decision": decision, "performance_qualified": False,
            "device_experiment_executed_by_tool": False, "BIL_auto_enable": False,
            "protocol_canonical_sha256": digest(protocol), "plan_canonical_sha256": plan_hash,
            "attempted_rows": len(phase_rows), "planned_rows": len(expected),
            "functional_failures": functional, "incomplete_reasons": incomplete,
            "journal_errors": errors, "freeze_errors": frozen_errors,
            "decision_reasons": reasons, "primary_estimate": estimate,
            "pilot_planning_log_half_width": pilot_half,
            "precision_target_log_half_width": 0.01,
            "pilot_planning_is_not_achieved_precision": True,
            "formal_second_minus_first_half_window_log_ratio": shift,
            "formal_trend_warning": trend, "blocks": blocks,
            "window_means": [{"window_id": key[0], "session_id": key[1],
                              "complete_blocks": len(values), "mean_log_ratio": statistics.mean(values)}
                             for key, values in windows.items()],
            "cadence": cadence, "pilot_pooled_into_formal": False,
            "limitations": ["Raw events are hashed in physical order; failed rows are never replaced or deleted.",
                            "Missing RSS/PSS, energy or metadata is unavailable, not zero or no regression.",
                            "Bootstrap is a prespecified sensitivity only; it cannot override t, precision or trend gates.",
                            "Window-level inference requires a defensible outer design, not merely 12 labels."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--phase", choices=("pilot", "formal"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    protocol, plan = (strict_json(path.read_text(encoding="utf-8-sig")) for path in (args.protocol, args.plan))
    records, errors = load_journal(args.journal)
    report = analyze(protocol, plan, records, args.phase, errors)
    report.update({"journal_file_sha256": file_sha(args.journal),
                   "protocol_file_sha256": file_sha(args.protocol), "plan_file_sha256": file_sha(args.plan),
                   "analysis_tool_sha256": file_sha(__file__)})
    write_new_json(args.output, report)
    print(json.dumps({"decision": report["decision"], "performance_qualified": False,
                      "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
