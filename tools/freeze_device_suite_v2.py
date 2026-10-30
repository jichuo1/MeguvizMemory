#!/usr/bin/env python3
"""Freeze exact identities and a finite schedule after real device qualification.

No ADB or native execution. Requires a fresh successful 196-case arm64 receipt
and three core safety tests, tied to current artifacts and admission. The draft
proposal is versioned separately from this immutable protocol and plan.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from paired_analysis_v2 import file_sha, strict_json, write_new_json
from paired_suite_v2 import default_protocol, freeze_errors, make_plan

ROOT = Path(__file__).resolve().parents[1]


def require(value, reason):
    if not value:
        raise ValueError(reason)


def read(path):
    return strict_json(Path(path).read_text(encoding="utf-8-sig"))


def freeze(functional_path, admission_path, output, start_delay=240, battery_source="dumpsys_battery",
           window_gap=120, session_cap=110, overall_cap=2400, engineering_retry=None):
    require(120 <= start_delay <= 1800, "start_delay_outside_bounded_range")
    require(battery_source in ("sysfs", "dumpsys_battery"), "unsupported_battery_source")
    require(120 <= window_gap <= 600 and 60 <= session_cap < window_gap,
            "session_cap_must_fit_bounded_window_gap")
    require(2400 <= overall_cap <= 7200, "overall_cap_outside_bounded_range")
    functional, admission = read(functional_path), read(admission_path)
    require(functional.get("state") == "PASS" and functional.get("all_passed") is True,
            "real_functional_qualification_required")
    require(functional.get("scope") == "ARM64_V2_CONSUMERS_FUNCTIONAL_ONLY_NOT_PERFORMANCE",
            "wrong_functional_scope")
    require(functional.get("completed_cases") == {"dex":32, "image_workspace":156, "image_perf":8},
            "incomplete_consumer_cases")
    require(functional.get("core_safety", {}).get("status") == "PASS" and
            functional["core_safety"].get("tests") == 3, "fresh_core_safety_required")
    require(admission.get("state") == "RELEASED_FOR_MEMORY" and
            admission.get("device_serial_sha256") == functional.get("device_serial_sha256"),
            "admission_device_mismatch")
    require(functional["admission"]["sha256"] == file_sha(admission_path), "admission_changed_after_functional")
    now = datetime.now(timezone.utc)
    require(0 <= (now-datetime.fromisoformat(functional["completed_at_utc"])).total_seconds() <= 1800,
            "functional_qualification_too_old_for_this_freeze")
    first = now + timedelta(seconds=start_delay)
    deadline = datetime.fromisoformat(admission["expires_at_utc"])
    require(deadline > first + timedelta(seconds=15*window_gap+session_cap), "admission_does_not_cover_full_schedule")
    profile = dict(functional["device"])
    require("arm64-v8a" in profile["abi_list"].split(",") and profile["api"] >= 27, "unsupported_device")
    require(profile["requested_cpu"] == 2, "this_protocol_selects_CPU2")
    profile.update({"abi":"arm64-v8a", "selected_cpu":2,
                    "device_serial_sha256":functional["device_serial_sha256"]})
    protocol = default_protocol()
    protocol.update({"attempt_id":"new-device-"+now.strftime("%Y%m%dT%H%M%SZ"),
                     "state":"FROZEN_BEFORE_PILOT", "device_execution":"AUTHORIZED_CURRENT_BATCH",
                     "frozen_at_utc":now.isoformat(),
                     "parent_protocol_file_sha256":file_sha(ROOT/"bench/paired_protocol_v2_draft.json")})
    if engineering_retry:
        evidence=read(engineering_retry)
        require(evidence.get("state")=="FIRST_ATTEMPT_INCOMPLETE_ENGINEERING_WALL_CAP" and
                evidence.get("one_engineering_retry_predeclared") is True and
                evidence.get("additional_retry_after_retry1") is False and
                evidence.get("gain_estimates_examined") is False, "not_a_bounded_predeclared_engineering_retry")
        require((window_gap,session_cap,overall_cap)==(240,210,4500), "retry_limits_differ_from_preregistration")
        protocol["experiment_id"] += "-engineering-retry1"
        protocol["attempt_id"] += "-retry1"
        protocol["engineering_retry_preregistration_file_sha256"]=file_sha(engineering_retry)
        protocol["prior_attempt_protocol_file_sha256"]=evidence["original_protocol_sha256"]
        protocol["old_attempt_pooling"]=False
        protocol["additional_engineering_retry_allowed"]=False
    else:
        require((window_gap,session_cap,overall_cap)==(120,110,2400),
                "nondefault_limits_require_explicit_engineering_preregistration")
    protocol["sampling"]["minimum_window_start_gap_seconds"]=window_gap
    image = read(ROOT/"bench/image_perf_v2.json")
    identity = protocol["families"]["image"]["semantic_identity"]
    identity.update({key:image["input"][key] for key in ("source_sha256","input_bytes","width","height")})
    identity.update({key:image["completion"][key] for key in
                     ("frames","completed_frames","result_checksum","output_hash_fnv64","gray_conversions")})
    artifacts = []
    for role, relative, name, executable in (
        ("library","install/android-arm64-v8a/lib/libmgmem.so","libmgmem.so",False),
        ("dex_formal","build/dex-borrow-v2-arm64/mgmem_dex_paired_v2","mgmem_dex_paired_v2",True),
        ("image_formal","build/image-perf-v2-arm64/mgmem_image_perf","mgmem_image_perf",True),
        ("dex_input","artifacts/local/perf-inputs/bilibili-9.17.0-classes.dex","input.dex",False),
        ("image_input","build/image-perf-v2-checks/1920x1080.ppm","input.ppm",False)):
        path = ROOT/relative
        actual = file_sha(path)
        matched = [item for item in functional["deployment_manifest"] if item["sha256"] == actual]
        require(matched, "artifact_not_qualified_by_functional_receipt:"+role)
        artifacts.append({"role":role,"path":relative,"name":name,"sha256":actual,"executable":executable})
    for family, variants in protocol["families"].items():
        for variant in variants["variants"]:
            if family == "dex":
                limits={"unique_owned_backing_peak_bytes":0 if variant=="D-Z0" else 10540184,
                        "external_source_capacity_bytes":21080368,"parser_peak_owned_bytes":268435456,
                        "reserved_final_bytes":0}
            else:
                limits={"unique_owned_backing_peak_bytes":10368000 if variant=="I-DF" else
                        8359936 if variant=="I-AR" else 8300160,
                        "external_source_capacity_bytes":12441634,"steady_state_provider_allocations":0,
                        "reserved_final_bytes":0}
            variants["resource_limits"][variant]=limits
    scheduled=[]
    for index in range(16):
        phase="pilot" if index<4 else "formal"
        scheduled.append({"phase":phase,"window_id":f"{phase}-w{index if index<4 else index-4:02d}",
                          "start_utc":(first+timedelta(seconds=index*window_gap)).isoformat(),
                          "day_id":first.astimezone(timezone(timedelta(hours=8))).date().isoformat(),
                          "environment_id":"new-device-shared-day-environment"})
    sources=["tools/device_capture_v2.py","tools/paired_suite_v2.py","tools/paired_analysis_v2.py",
             "tools/run_arm64_validation.py","tools/run_paired_device.py","tools/run_device_suite_v2.py",
             "tools/freeze_device_suite_v2.py","tools/validate_arm64_consumers_v2.py",
             "consumers/image/perf.cpp","consumers/image/CMakeLists.txt","consumers/dex/paired_v2.cpp",
             "consumers/dex/CMakeLists.txt"]
    relative_functional=Path(functional_path).resolve().relative_to(ROOT).as_posix()
    protocol["freeze"].update({"device_profile":profile,"artifact_closure":artifacts,
        "functional_receipt_sha256":file_sha(functional_path),
        "functional_receipts":[{"path":relative_functional,"sha256":file_sha(functional_path),
                               "expected":{"state":"PASS","all_passed":True}}],
        "source_closure":[{"path":path,"sha256":file_sha(ROOT/path)} for path in sources],
        "host_clock_epoch_id":"memory-v2-"+now.strftime("%Y%m%dT%H%M%SZ"),
        "scheduled_windows":scheduled,"single_task_timeout_seconds":30,
        "session_wall_cap_seconds":session_cap,"overall_wall_cap_seconds":overall_cap,
        "maximum_environment_waits":5,
        "cadence":{"battery_source":battery_source,"background_window_seconds":.4,"busy_interval_min_ns":300000000,
                   "busy_interval_max_ns":1500000000,"observation_to_launch_max_ns":2000000000,
                   "cooldown_seconds":1,"window_start_lateness_max_seconds":45,
                   "minimum_actual_window_start_gap_seconds":90},
        "resource_budgets":{"scope":"NATIVE_REQUESTED_INPUT_WORKSPACE_AND_PARSER_ACCOUNTS_ONLY",
                            "metadata_RSS_PSS_energy":"UNMEASURED_NOT_ZERO_OR_NO_REGRESSION",
                            "limits":"families.<family>.resource_limits"}})
    plan=make_plan(protocol)
    require(not freeze_errors(protocol,plan), "frozen_protocol_validation_failed:"+str(freeze_errors(protocol,plan)))
    output=Path(output).resolve()
    require(output.is_relative_to(ROOT) and not output.exists(), "output_must_be_new_inside_workspace")
    output.mkdir(parents=True)
    write_new_json(output/"protocol.json",protocol)
    write_new_json(output/"plan.json",plan)
    return {"state":protocol["state"],"planned_tasks":len(plan["entries"]),
            "first_window_utc":first.isoformat(),"last_window_utc":scheduled[-1]["start_utc"],
            "protocol_file_sha256":file_sha(output/"protocol.json"),"performance_qualified":False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--functional-receipt",type=Path,required=True)
    parser.add_argument("--admission",type=Path,required=True)
    parser.add_argument("--out-dir",type=Path,required=True)
    parser.add_argument("--start-delay",type=int,default=240)
    parser.add_argument("--battery-source",choices=("sysfs","dumpsys_battery"),default="dumpsys_battery")
    parser.add_argument("--window-gap",type=int,default=120)
    parser.add_argument("--session-cap",type=int,default=110)
    parser.add_argument("--overall-cap",type=int,default=2400)
    parser.add_argument("--engineering-retry",type=Path)
    args=parser.parse_args()
    print(json.dumps(freeze(args.functional_receipt,args.admission,args.out_dir,args.start_delay,args.battery_source,
                            args.window_gap,args.session_cap,args.overall_cap,args.engineering_retry)))


if __name__=="__main__":
    main()
