#!/usr/bin/env python3
"""Synthetic tests only: never query a device or import product code."""
from __future__ import annotations

from copy import deepcopy
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import sys
import unittest

from paired_suite_v2 import (analyze_family, critical_value, default_protocol, digest,
                             environment_errors, freeze_errors, make_plan, protocol_errors,
                             pilot_eligibility, resource_view, t_cdf, t_quantile)


def protocol_fixture():
    protocol=default_protocol()
    protocol.update({"attempt_id":"synthetic-only","state":"FROZEN_BEFORE_PILOT",
                     "device_execution":"AUTHORIZED_CURRENT_BATCH"})
    start=datetime(2026,10,30,8,0,tzinfo=timezone.utc)
    windows=[]
    for index in range(16):
        phase="pilot" if index<4 else "formal"
        windows.append({"phase":phase,"window_id":f"{phase}-w{index:02}",
                        "start_utc":(start+timedelta(seconds=index*120)).isoformat(),
                        "day_id":"synthetic-day","environment_id":"synthetic-env"})
    protocol["freeze"].update({"device_profile":{"selected_cpu":2},
        "artifact_closure":[{"role":"dummy","sha256":"a"*64}],
        "functional_receipt_sha256":"b"*64,"scheduled_windows":windows,
        "single_task_timeout_seconds":30,"session_wall_cap_seconds":90,
        "overall_wall_cap_seconds":2400,"maximum_environment_waits":3,
        "cadence":{"busy_interval_min_ns":350_000_000,"busy_interval_max_ns":900_000_000,
                   "observation_to_launch_max_ns":100_000_000},"resource_budgets":{"synthetic":True}})
    image=protocol["families"]["image"]
    image["semantic_identity"].update({"source_sha256":"c"*64,"input_bytes":123,
        "width":7,"height":5,"result_checksum":1234,"output_hash_fnv64":5678,"gray_conversions":105})
    for family in protocol["families"].values():
        family["resource_limits"]={variant:{"reserved_final_bytes":0,"unique_owned_backing_peak_bytes":1_000_000_000}
                                   for variant in family["variants"]}
    return protocol


def records_fixture(protocol,plan):
    rows=[]
    multiplier={"D-C0":1.,"D-Z0":.95,"D-C1":1.01,"D-C2":1.02,
                "I-DF":1.,"I-DR":.9,"I-SR":.91,"I-AR":.92}
    for index,entry in enumerate(plan["entries"]):
        family=protocol["families"][entry["family"]]
        native={**family["semantic_identity"],**family["required_success"],
                **family["variants"][entry["variant"]]["native_match"],
                "total_ns":round(100_000_000*multiplier[entry["variant"]]),"cpu_before":2,"cpu_after":2,
                "logical_requested_bytes":100,"unique_owned_backing_peak_bytes":100,
                "external_source_capacity_bytes":200,"retained_before_collect_bytes":0,
                "reserved_final_bytes":0,"memory_metadata_bytes":None,"rss_pss_bytes":None,
                "energy":None,"parser_peak_owned_bytes":300}
        host=(index+1)*2_000_000_000; device=(index+1)*3_000_000_000
        env={"clock_domain":"DEVICE_BOOTTIME_PROC_UPTIME_NS","complete":True,"valid":True,
             "battery_deci_c":290,"thermal_status":0,"background_cpu_busy":.1,
             "busy_begin_monotonic_ns":device,"busy_end_monotonic_ns":device+400_000_000,
             "busy_observation_lower_bound_ns":390_000_000,"busy_observation_upper_bound_ns":410_000_000,
             "thermal_observed_monotonic_ns":device+420_000_000,"device_end_monotonic_ns":device+430_000_000}
        post={"clock_domain":env["clock_domain"],"complete":True,"valid":True,
              "battery_deci_c":290,"thermal_status":0,"thermal_observed_monotonic_ns":device+700_000_000,
              "device_end_monotonic_ns":device+710_000_000}
        rows.append({**entry,"experiment_id":protocol["experiment_id"],"attempt_id":protocol["attempt_id"],
            "estimand_id":protocol["estimand_id"],"plan_canonical_sha256":digest(plan),"success":True,
            "native":native,"command_result":{"stdout":json.dumps(native),"stderr":"","exit_code":0,"timed_out":False},
            "resources":resource_view(protocol,entry["family"],native),"environment":env,"environment_after":post,
            "host_clock_epoch_id":"synthetic-clock-epoch",
            "started_at_utc":(datetime.fromisoformat(entry["scheduled_window_start_utc"])+timedelta(seconds=(index%32)*.5)).isoformat(),
            "clocks":{"observation_begin_monotonic_ns":host,"observation_end_monotonic_ns":host+500_000_000,
                      "launch_monotonic_ns":host+510_000_000,"completion_monotonic_ns":host+700_000_000}})
    return rows


class SuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protocol=protocol_fixture(); cls.plan=make_plan(cls.protocol); cls.rows=records_fixture(cls.protocol,cls.plan)

    def result(self,rows=None,phase="formal",family="dex",protocol=None):
        return analyze_family(protocol or self.protocol,self.plan,self.rows if rows is None else rows,phase,family)

    def altered(self,change):
        rows=deepcopy(self.rows); row=next(r for r in rows if r["family"]=="dex" and r["phase"]=="formal")
        change(row); return rows

    def test_t_quantile_matches_existing_df11_reference(self):
        self.assertAlmostEqual(t_quantile(.975,11),2.200985160082949,places=9)

    def test_t_quantile_matches_exact_cauchy(self):
        self.assertAlmostEqual(t_quantile(.975,1),12.7062047361747,places=8)

    def test_t_cdf_symmetry(self):
        self.assertAlmostEqual(t_cdf(2.3,11)+t_cdf(-2.3,11),1,places=13)

    def test_multiplicity_coefficient_larger_than_unadjusted(self):
        self.assertTrue(2.200985<critical_value()<4)

    def test_plan_has_512_unique_tasks(self):
        self.assertEqual(len(self.plan["entries"]),512)
        self.assertEqual(len({e["task_id"] for e in self.plan["entries"]}),512)

    def test_plan_mirrored_and_each_variant_twice(self):
        for start in range(0,512,8):
            order=[e["variant"] for e in self.plan["entries"][start:start+8]]
            self.assertEqual(order,order[::-1]); self.assertEqual(sorted(order.count(v) for v in set(order)),[2]*4)

    def test_family_first_alternates(self):
        first=[self.plan["entries"][i]["family"] for i in range(0,512,32)]
        self.assertEqual(first,["dex","image"]*8)

    def test_plan_deterministic_and_frozen(self):
        self.assertEqual(self.plan,make_plan(self.protocol)); self.assertEqual(freeze_errors(self.protocol,self.plan),[])

    def test_protocol_rejects_unfilled_template(self):
        self.assertTrue(protocol_errors(default_protocol()))

    def test_window_spacing_gate(self):
        p=deepcopy(self.protocol);p["freeze"]["scheduled_windows"][1]["start_utc"]=p["freeze"]["scheduled_windows"][0]["start_utc"]
        self.assertIn("window_start_gap_below_frozen_minimum",protocol_errors(p))

    def test_protocol_does_not_allow_inference_upgrade(self):
        p=deepcopy(self.protocol);p["sampling"]["outer_inference"]="INDEPENDENT"
        self.assertIn("unsupported_outer_inference_claim",protocol_errors(p))

    def test_valid_formal_remains_inconclusive(self):
        r=self.result();self.assertEqual(r["decision"],"INCONCLUSIVE_OUTER_INDEPENDENCE_NOT_ESTABLISHED")
        self.assertFalse(r["performance_qualified"]);self.assertEqual(r["attempted_tasks"],192)
        self.assertEqual(r["functional_failures"],[]); self.assertEqual(r["incomplete_reasons"],[])

    def test_all_four_attributions_and_reverse_gain_interval(self):
        r=self.result();self.assertEqual(set(r["contrasts"]),{"ordinary","interface","arena","memory_net"})
        e=r["contrasts"]["ordinary"]["estimate"]
        self.assertAlmostEqual(e["gain"],.05,places=10);self.assertLessEqual(e["gain_interval"][0],e["gain_interval"][1])

    def test_secondary_other_baseline_is_separate(self):
        r=self.result(); item=r["secondary_contrasts"]["arena_vs_direct_copy"]
        self.assertIn("UNADJUSTED",item["simultaneous_interval_class"])
        self.assertFalse(item["pilot_precision_screen_applies"])
        self.assertAlmostEqual(item["estimate"]["gain"],-.02,places=10)

    def test_resource_denominator_zero_is_unavailable(self):
        rows=deepcopy(self.rows)
        for row in rows:
            if row["family"]=="dex" and row["variant"]=="D-Z0":
                row["native"]["unique_owned_backing_peak_bytes"]=0
                row["command_result"]["stdout"]=json.dumps(row["native"])
                row["resources"]=resource_view(self.protocol,"dex",row["native"])
        result=self.result(rows);comparison=result["contrasts"]["memory_net"]["resource_comparison"]
        self.assertIsNone(comparison["owned_backing_peak_ratio"]["value"])
        self.assertEqual(comparison["source_plus_input_or_workspace_capacity_ratio"]["value"],1.5)
        self.assertFalse(result["descriptive_memory_target"]["source_plus_backing_gate"])

    def test_pilot_not_pooled_and_feasible(self):
        r=self.result(phase="pilot");self.assertEqual(r["attempted_tasks"],64)
        self.assertEqual(r["decision"],"PILOT_FEASIBLE_FORMAL_ALLOWED")
        self.assertIsNone(r["contrasts"]["ordinary"]["estimate"])

    def test_formal_requires_identity_bound_feasible_pilot(self):
        r=self.result(phase="pilot")
        self.assertTrue(pilot_eligibility(self.protocol,self.plan,r,"dex")["formal_allowed"])
        self.assertFalse(pilot_eligibility(self.protocol,self.plan,r,"image")["formal_allowed"])

    def test_pilot_wrong_plan_is_not_eligible(self):
        r=self.result(phase="pilot");r["plan_canonical_sha256"]="0"*64
        self.assertFalse(pilot_eligibility(self.protocol,self.plan,r,"dex")["formal_allowed"])

    def test_precision_stop_cannot_admit_formal(self):
        r=self.result(phase="pilot");r["decision"]="STOP_FAMILY_PILOT_PRECISION_INSUFFICIENT"
        self.assertFalse(pilot_eligibility(self.protocol,self.plan,r,"dex")["formal_allowed"])

    def test_late_actual_window_start_incomplete(self):
        rows=deepcopy(self.rows)
        row=next(r for r in rows if r["phase"]=="formal")
        row["started_at_utc"]=(datetime.fromisoformat(row["scheduled_window_start_utc"])+timedelta(seconds=61)).isoformat()
        self.assertTrue(any("window_actual_start" in str(reason) for reason in self.result(rows)["incomplete_reasons"]))

    def test_window_before_schedule_incomplete(self):
        rows=deepcopy(self.rows);row=next(r for r in rows if r["phase"]=="formal")
        row["started_at_utc"]=(datetime.fromisoformat(row["scheduled_window_start_utc"])-timedelta(seconds=1)).isoformat()
        self.assertTrue(any("window_actual_start" in str(reason) for reason in self.result(rows)["incomplete_reasons"]))

    def test_global_family_interleaving_is_not_hidden_by_filtering(self):
        rows=deepcopy(self.rows);start=next(i for i,r in enumerate(rows) if r["phase"]=="formal")
        rows[start+15],rows[start+16]=rows[start+16],rows[start+15]
        self.assertTrue(any("global_physical_suite_order" in str(reason) for reason in self.result(rows)["incomplete_reasons"]))

    def test_preserved_pre_gate_failure_is_incomplete_not_exception(self):
        rows=self.altered(lambda r:r.update(success=False,native=None,command_result=None,resources=None,environment=None,environment_after=None,clocks={}))
        r=self.result(rows);self.assertEqual(r["decision"],"INCOMPLETE")

    def test_missing_row_incomplete(self):
        rows=deepcopy(self.rows);rows.pop(next(i for i,r in enumerate(rows) if r["phase"]=="formal" and r["family"]=="dex"))
        self.assertEqual(self.result(rows)["decision"],"INCOMPLETE")

    def test_duplicate_row_incomplete(self):
        rows=deepcopy(self.rows); rows.append(deepcopy(next(r for r in rows if r["phase"]=="formal" and r["family"]=="dex")))
        self.assertEqual(self.result(rows)["decision"],"INCOMPLETE")

    def test_physical_order_incomplete(self):
        rows=deepcopy(self.rows); indexes=[i for i,r in enumerate(rows) if r["phase"]=="formal" and r["family"]=="dex"]
        a,b=indexes[:2];rows[a],rows[b]=rows[b],rows[a]
        self.assertEqual(self.result(rows)["decision"],"INCOMPLETE")

    def test_wrong_output_stops_entire_suite(self):
        def change(r):
            r["native"]["result_checksum"]+=1;r["command_result"]["stdout"]=json.dumps(r["native"])
        self.assertEqual(self.result(self.altered(change))["decision"],"STOP_ENTIRE_SUITE_CORRECTNESS_OR_LIFETIME_FAILURE")

    def test_stdout_binding_rejected(self):
        self.assertTrue(self.result(self.altered(lambda r:r["command_result"].update(stdout="{}")))["functional_failures"])

    def test_native_variant_rejected(self):
        self.assertTrue(self.result(self.altered(lambda r:r["native"].update(variant="D-Z0-WRONG")))["functional_failures"])

    def test_diagnostic_rejected(self):
        self.assertTrue(self.result(self.altered(lambda r:r["native"].update(diagnostic=True)))["functional_failures"])

    def test_missing_environment_not_zero(self):
        self.assertTrue(self.result(self.altered(lambda r:r["environment"].pop("battery_deci_c")))["incomplete_reasons"])

    def test_impossible_battery_rejected(self):
        self.assertTrue(self.result(self.altered(lambda r:r["environment"].update(battery_deci_c=-999)))["incomplete_reasons"])

    def test_fractional_device_ns_rejected(self):
        self.assertTrue(self.result(self.altered(lambda r:r["environment"].update(busy_begin_monotonic_ns=1.5)))["incomplete_reasons"])

    def test_clock_domains_cannot_be_silently_mixed(self):
        self.assertTrue(self.result(self.altered(lambda r:r["environment"].update(clock_domain="HOST")))["incomplete_reasons"])

    def test_actual_busy_bounds_required(self):
        self.assertTrue(self.result(self.altered(lambda r:r["environment"].update(busy_observation_upper_bound_ns=2_000_000_000)))["incomplete_reasons"])

    def test_closed_false_not_zero(self):
        self.assertTrue(self.result(self.altered(lambda r:r["native"].update(closed_backing_bytes=False)))["functional_failures"])

    def test_resource_budget_enforced(self):
        def change(r):
            r["native"]["unique_owned_backing_peak_bytes"]=2_000_000_000
            r["command_result"]["stdout"]=json.dumps(r["native"])
            r["resources"]=resource_view(self.protocol,"dex",r["native"])
        self.assertEqual(self.result(self.altered(change))["decision"],"RESOURCE_BUDGET_FAILURE")

    def test_resource_adapter_not_fabricated(self):
        self.assertTrue(self.result(self.altered(lambda r:r["resources"]["values"].update(unique_owned_backing_peak_bytes=0)))["functional_failures"])

    def test_pilot_variance_stops_family_only(self):
        rows=deepcopy(self.rows)
        for row in rows:
            if row["phase"]=="pilot" and row["family"]=="dex" and row["variant"]=="D-Z0":
                window=int(row["window_id"][-2:]);row["native"]["total_ns"]*=1+window
                row["command_result"]["stdout"]=json.dumps(row["native"])
        self.assertEqual(self.result(rows,phase="pilot")["decision"],"STOP_FAMILY_PILOT_PRECISION_INSUFFICIENT")
        self.assertEqual(self.result(rows,phase="pilot",family="image")["decision"],"PILOT_FEASIBLE_FORMAL_ALLOWED")

    def test_formal_trend_is_explicit(self):
        rows=deepcopy(self.rows)
        for row in rows:
            if row["phase"]=="formal" and row["family"]=="dex" and row["variant"]=="D-Z0" and int(row["window_id"][-2:])>=10:
                row["native"]["total_ns"]*=2;row["command_result"]["stdout"]=json.dumps(row["native"])
        r=self.result(rows);self.assertTrue(r["contrasts"]["ordinary"]["formal_trend_warning"])
        self.assertFalse(r["performance_qualified"])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(SuiteTests)
    names=[test.id().split(".")[-1] for test in suite]
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    if args.output:
        def sha(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()
        root=Path(__file__).resolve().parents[1]
        protocol=protocol_fixture();plan=make_plan(protocol)
        receipt={"schema_version":2,"scope":"SYNTHETIC_OFFLINE_TESTS_NO_DEVICE_OR_PRODUCT_EXECUTION",
                 "status":"PASS" if result.wasSuccessful() else "FAIL","test_count":result.testsRun,
                 "test_names":names,"failures":len(result.failures),"errors":len(result.errors),
                 "device_operations":0,"performance_qualified":False,
                 "sources":[{"path":relative,"sha256":sha(root/relative)} for relative in
                            ("tools/paired_suite_v2.py","tools/test_paired_suite_v2.py","tools/paired_analysis_v2.py")],
                 "synthetic_plan_canonical_sha256":digest(plan),"synthetic_plan_tasks":512,
                 "bonferroni_t_df11_four_contrasts":critical_value(),
                 "secondary_exploratory_t_df11":t_quantile(.975,11),
                 "qualification":"Synthetic correctness of protocol/analysis only; no device measurement or independent-window claim."}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open("x",encoding="utf-8",newline="\n") as stream:
            stream.write(json.dumps(receipt,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
        print(json.dumps({"status":receipt["status"],"test_count":result.testsRun,"device_operations":0}))
    return 0 if result.wasSuccessful() else 1


if __name__=="__main__":
    raise SystemExit(main())
