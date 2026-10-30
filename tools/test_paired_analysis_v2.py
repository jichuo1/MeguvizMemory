#!/usr/bin/env python3
"""Deterministic synthetic corruption/estimator tests; never device evidence."""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

from paired_analysis_v2 import (NATIVE_RESOURCE_FIELDS, RESOURCE_FIELDS, T_975_DF11, analyze, digest,
                                file_sha, load_journal, make_plan, native_to_resources, strict_json,
                                write_new_journal, write_new_json)


ROOT = Path(__file__).resolve().parents[1]


def fixture(protocol, plan, phase, log_ratios=None):
    """All environments, clocks, resources and timings here are FABRICATED."""
    rows = []
    entries = [entry for entry in plan["entries"] if entry["phase"] == phase]
    for index, entry in enumerate(entries):
        window_index = int(entry["window_id"].rsplit("w", 1)[1])
        log_ratio = log_ratios[window_index] if log_ratios else -0.01 + (window_index % 3 - 1) * 0.0001
        candidate = entry["variant"] == protocol["primary_contrast"][0]
        start = 1_000_000_000 + index * 1_000_000_000
        native = {"schema_version": 2, "workload": protocol["estimand_id"], "variant": entry["variant"],
                  "diagnostic": False, "phases": None, "success": True, "case": "normal", **protocol["semantic_identity"],
                  "total_ns": round(100_000_000 * math.exp(log_ratio)) if candidate else 100_000_000,
                  "closed_backing_bytes": 0, "source_final_released": True,
                  "lease_final_correct": True, "final_resource_state": "CLOSED_ZERO",
                  "logical_requested_bytes": 10540184,
                  "unique_owned_backing_peak_bytes": 0 if candidate else 10540184,
                  "external_source_capacity_bytes": 10540184, "borrowed_view_peak_bytes": 10540184,
                  "arena_used_bytes": 0, "input_backing_padding_bytes": 0,
                  "retained_before_collect_bytes": 0, "reserved_final_bytes": 0,
                  "memory_metadata_bytes": None, "parser_peak_owned_bytes": 150000000,
                  "rss_pss_bytes": None, "energy": None}
        resources = native_to_resources(native)
        rows.append({**entry, "experiment_id": protocol["experiment_id"],
                     "attempt_id": protocol["attempt_id"], "estimand_id": protocol["estimand_id"],
                     "plan_canonical_sha256": digest(plan), "day_id": "synthetic-day",
                     "environment_id": "synthetic-environment", "host_clock_epoch_id": "synthetic-epoch",
                     "synthetic": True, "success": True, "native": native, "resources": resources,
                     "command_result": {"exit_code": 0, "stdout": json.dumps(native)},
                     "environment": {"complete": True, "valid": True, "battery_deci_c": 300,
                                     "thermal_status": 0, "background_cpu_busy": 0.1,
                                     "busy_begin_monotonic_ns": start + 1,
                                     "busy_end_monotonic_ns": start + 100_000_001,
                                     "thermal_observed_monotonic_ns": start + 2},
                     "clocks": {"observation_begin_monotonic_ns": start,
                                "observation_end_monotonic_ns": start + 200_000_000,
                                "launch_monotonic_ns": start + 210_000_000,
                                "completion_monotonic_ns": start + 400_000_000}})
    return rows


def run_tests(output, native_receipt=None):
    protocol = strict_json((ROOT / "bench/paired_protocol_v2_draft.json").read_text(encoding="utf-8"))
    plan = make_plan(protocol)
    formal, pilot = fixture(protocol, plan, "formal"), fixture(protocol, plan, "pilot")
    results = []

    def check(name, assertion):
        if not assertion:
            raise AssertionError(name)
        results.append({"case": name, "status": "PASS"})

    baseline = analyze(protocol, plan, formal, "formal")
    check("formal_384_tasks_12_window_units_and_draft_never_qualifies",
          baseline["attempted_rows"] == 384 and len(baseline["window_means"]) == 12 and
          baseline["plan_canonical_sha256"] == digest(plan) and
          baseline["protocol_canonical_sha256"] == digest(protocol) and
          "plan_sha256" not in baseline and "protocol_sha256" not in baseline and
          not baseline["functional_failures"] and not baseline["incomplete_reasons"] and
          baseline["performance_qualified"] is False and bool(baseline["freeze_errors"]))
    window_values = [item["mean_log_ratio"] for item in baseline["window_means"]]
    independent_mean = sum(window_values) / 12
    independent_sd = math.sqrt(sum((v - independent_mean) ** 2 for v in window_values) / 11)
    independent_half = T_975_DF11 * independent_sd / math.sqrt(12)
    expected_gain_interval = [1 - math.exp(independent_mean + independent_half),
                              1 - math.exp(independent_mean - independent_half)]
    actual = baseline["primary_estimate"]
    check("t_interval_uses_12_windows_and_reverses_gain_endpoints",
          abs(actual["achieved_log_half_width"] - independent_half) < 1e-14 and
          all(abs(a - b) < 1e-14 for a, b in zip(actual["primary_gain_interval"], expected_gain_interval)))
    check("null_resources_with_explicit_reasons_remain_unavailable",
          baseline["incomplete_reasons"] == [] and formal[0]["resources"]["rss_pss_bytes"] is None)
    check("cadence_records_actual_busy_window_and_launch_gap",
          baseline["cadence"][0]["actual_busy_window_ns"] == 100_000_000 and
          baseline["cadence"][1]["launch_interval_ns"] == 1_000_000_000 and
          baseline["cadence"][0]["launch_interval_ns"] is None)
    pilot_result = analyze(protocol, plan, pilot, "pilot")
    check("single_pilot_128_tasks_feasibility_only_no_formal_pooling",
          pilot_result["decision"] == "PILOT_FEASIBILITY_ONLY" and pilot_result["primary_estimate"] is None
          and pilot_result["attempted_rows"] == 128)
    wide_pilot = analyze(protocol, plan, fixture(protocol, plan, "pilot", [-0.3, 0.3, -0.3, 0.3]), "pilot")
    check("pilot_above_fixed_precision_cap_stops_without_extra_windows",
          wide_pilot["decision"] == "PRECISION_INSUFFICIENT_STOP_PROTOCOL")

    def changed(name, mutate, expected_decision, reason):
        rows = copy.deepcopy(formal)
        mutate(rows)
        report = analyze(protocol, plan, rows, "formal")
        check(name, report["decision"] == expected_decision and any(reason in str(value) for value in report["decision_reasons"])
              and not report["performance_qualified"])

    changed("missing_task_kept_incomplete_not_replaced", lambda rows: rows.pop(20), "INCOMPLETE", "task_count")
    changed("duplicate_identity_rejected", lambda rows: rows.insert(1, copy.deepcopy(rows[0])), "INCOMPLETE", "duplicate_task_identity")
    changed("boolean_position_is_not_integer_identity", lambda rows: rows[0].update(position=False),
            "INCOMPLETE", "invalid_block_or_position_integer")
    changed("reordered_rows_rejected_without_sorting", lambda rows: rows.__setitem__(slice(0, 2), [rows[1], rows[0]]),
            "INCOMPLETE", "physical_order")
    changed("wrong_checksum_stops_qualification", lambda rows: rows[0]["native"].update(result_checksum=0),
            "STOP_CORRECTNESS_OR_LIFETIME_FAILURE", "semantic_identity_mismatch:result_checksum")
    changed("wrong_native_variant_stops_qualification", lambda rows: rows[0]["native"].update(variant="D-C2"),
            "STOP_CORRECTNESS_OR_LIFETIME_FAILURE", "native_variant_differs")
    changed("diagnostic_binary_in_formal_stops_qualification", lambda rows: rows[0]["native"].update(diagnostic=True),
            "STOP_CORRECTNESS_OR_LIFETIME_FAILURE", "diagnostic_or_phase")
    changed("wrong_native_input_bytes_stops_qualification", lambda rows: rows[0]["native"].update(input_bytes=7),
            "STOP_CORRECTNESS_OR_LIFETIME_FAILURE", "native_input_bytes_mismatch")
    changed("raw_stdout_disagrees_with_native_stops_qualification", lambda rows: rows[0]["command_result"].update(stdout="{}"),
            "STOP_CORRECTNESS_OR_LIFETIME_FAILURE", "raw_command_binding_failed")
    changed("missing_thermal_is_not_zero", lambda rows: rows[0]["environment"].update(thermal_status=None),
            "INCOMPLETE", "environment_missing_or_invalid:thermal_status")
    changed("fractional_environment_monotonic_timestamp_rejected", lambda rows: rows[0]["environment"].update(busy_begin_monotonic_ns=1000000000.5),
            "INCOMPLETE", "environment_missing_or_invalid:busy_begin_monotonic_ns")
    changed("missing_resource_without_reason_is_incomplete", lambda rows: rows[0]["resources"]["unavailable_reasons"].pop("rss_pss_bytes"),
            "STOP_CORRECTNESS_OR_LIFETIME_FAILURE", "resource_adapter_differs")
    changed("source_not_released_stops_qualification", lambda rows: rows[0]["native"].update(source_final_released=False),
            "STOP_CORRECTNESS_OR_LIFETIME_FAILURE", "final_resource_contract")
    changed("malformed_native_result_is_reported_not_crash", lambda rows: rows[0].update(native=None),
            "STOP_CORRECTNESS_OR_LIFETIME_FAILURE", "missing_native_result")
    changed("missing_monotonic_timestamp_is_incomplete", lambda rows: rows[0]["clocks"].pop("launch_monotonic_ns"),
            "INCOMPLETE", "missing_or_invalid_monotonic_clock")
    changed("clock_epoch_change_rejected", lambda rows: rows[3].update(host_clock_epoch_id="another-epoch"),
            "INCOMPLETE", "host_clock_epoch_changed")
    mixed = analyze(protocol, plan, pilot + formal, "formal")
    check("pilot_in_formal_journal_rejected_never_pooled", mixed["decision"] == "INCOMPLETE" and
          mixed["attempted_rows"] == 384 and any("other_phase" in reason for reason in mixed["incomplete_reasons"]))
    trend = analyze(protocol, plan, fixture(protocol, plan, "formal", [-0.002] * 6 + [-0.04] * 6), "formal")
    check("large_favorable_trend_stays_inconclusive", trend["formal_trend_warning"] and
          trend["decision"] == "INCONCLUSIVE" and not trend["performance_qualified"])
    check("no_trend_never_proves_independence", not baseline["formal_trend_warning"] and
          any("independence_not_proved" in reason for reason in baseline["decision_reasons"]))

    ROOT.joinpath("build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="paired-v2-offline-", dir=ROOT / "build") as directory:
        folder = Path(directory)
        journal = folder / "formal.jsonl"
        write_new_journal(journal, formal)
        original_sha = file_sha(journal)
        recovered, errors = load_journal(journal)
        check("fsynced_hash_chain_roundtrip_all_384_synthetic_rows", recovered == formal and not errors)
        try:
            write_new_journal(journal, [])
            refused = False
        except FileExistsError:
            refused = True
        check("raw_journal_x_mode_refuses_overwrite", refused and file_sha(journal) == original_sha)
        text = journal.read_text(encoding="utf-8")
        tampered = folder / "tampered.jsonl"
        tampered.write_text(text.replace('"total_ns":100000000', '"total_ns":100000001', 1), encoding="utf-8")
        _, errors = load_journal(tampered)
        check("modified_raw_payload_breaks_hash_chain", bool(errors))
        truncated = folder / "truncated.jsonl"
        truncated.write_text(text[:-24], encoding="utf-8")
        _, errors = load_journal(truncated)
        check("truncated_tail_is_explicit_incomplete", bool(errors))
        plan_path = folder / "plan.json"
        write_new_json(plan_path, plan)
        plan_sha = file_sha(plan_path)
        try:
            write_new_json(plan_path, {})
            refused = False
        except FileExistsError:
            refused = True
        check("plan_file_bytes_distinct_from_canonical_identity_and_x_mode_refuses_overwrite",
              refused and file_sha(plan_path) == plan_sha and plan_sha != digest(plan))
        execution = subprocess.run([sys.executable, str(ROOT / "tools/run_paired_device_v2.py"),
                                    "--protocol", str(folder / "nonexistent.json"), "--execute"],
                                   capture_output=True, text=True, check=False)
        execution_result = json.loads(execution.stdout)
        check("execute_refused_before_reading_protocol_without_device_access", execution.returncode == 2 and
              execution_result["device_operations"] == 0)
    for name, content in (("duplicate_JSON_key_rejected", '{"a":1,"a":2}'),
                          ("nonfinite_JSON_rejected", '{"a":NaN}')):
        try:
            strict_json(content)
            refused = False
        except ValueError:
            refused = True
        check(name, refused)
    adapter_smoke = None
    if native_receipt:
        captured = strict_json(native_receipt.read_text(encoding="utf-8"))
        actual_runs = [run["native"] for run in captured["runs"]
                       if isinstance(run.get("native"), dict) and run["native"].get("diagnostic") is False
                       and run["native"].get("case") == "normal"]
        check("real_host_native_payloads_cover_all_four_variants",
              {native["variant"] for native in actual_runs} == {"D-C0", "D-Z0", "D-C1", "D-C2"})
        check("real_host_native_resource_fields_have_explicit_complete_mapping",
              all(all(source in native for source in NATIVE_RESOURCE_FIELDS.values()) for native in actual_runs))
        check("real_host_unknown_metadata_RSS_energy_remain_null_with_reasons",
              all(all(native_to_resources(native)[target] is None and
                      native_to_resources(native)["unavailable_reasons"][target]
                      for target in ("metadata_bytes", "rss_pss_bytes", "energy_joules")) for native in actual_runs))
        adapter_smoke = {"scope": "READ_ONLY_REAL_HOST_NATIVE_PAYLOAD_RESOURCE_ADAPTER_ONLY",
                         "source_receipt": str(native_receipt.resolve().relative_to(ROOT)).replace("\\", "/"),
                         "source_receipt_sha256": file_sha(native_receipt),
                         "native_payloads": [{"variant": native["variant"], "native_sha256": digest(native),
                                              "resources": native_to_resources(native)} for native in actual_runs],
                         "not_new_device_or_performance_evidence": True}
    receipt = {"schema_version": 2, "status": "PASS", "scope": "DETERMINISTIC_SYNTHETIC_OFFLINE_VALIDATOR_TESTS_ONLY",
               "device_operations": 0, "performance_qualified": False, "cases": len(results), "results": results,
               "protocol_file_sha256": file_sha(ROOT / "bench/paired_protocol_v2_draft.json"),
               "protocol_canonical_sha256": digest(protocol),
               "analysis_source_sha256": file_sha(ROOT / "tools/paired_analysis_v2.py"),
               "entrypoint_source_sha256": file_sha(ROOT / "tools/run_paired_device_v2.py"),
               "test_source_sha256": file_sha(__file__), "synthetic_formal_journal_sha256": original_sha,
               "real_native_adapter_smoke": adapter_smoke,
               "primary_estimator_synthetic_example": actual,
               "limitations": ["All observations in fixtures are fabricated; no device or product performance evidence.",
                               "Draft protocol remains not frozen; no live collector or performance qualification is implemented."]}
    write_new_json(output, receipt)
    print(json.dumps({"status": "PASS", "cases": len(results), "receipt": str(output)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-receipt", type=Path)
    args = parser.parse_args()
    run_tests(args.output, args.native_receipt)


if __name__ == "__main__":
    main()
