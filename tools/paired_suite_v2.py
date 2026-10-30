#!/usr/bin/env python3
"""Finite four-variant, window-level v2 plan and descriptive analysis.

No ADB, builds, mutable capture or old-data conversion. The same-day design does
not establish independent outer sampling: even favorable simultaneous intervals
never qualify performance or enable BIL. File and canonical identities differ.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import random
import statistics as st

from paired_analysis_v2 import canonical, digest, file_sha, integer, load_journal, number, strict_json, write_new_json

FAMILY_NAMES = ("dex", "image")
PILOT_WINDOWS, FORMAL_WINDOWS = 4, 12
BLOCKS, TASKS_PER_BLOCK = 2, 8
N_CONTRASTS, ALPHA = 4, .05


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def obj(value):
    return value if isinstance(value,dict) else {}


def t_cdf(value, degrees):
    """Integrate the Student density; no scipy/runtime dependency is needed."""
    require(number(value) and integer(degrees, 1), "invalid_t_arguments")
    if value == 0:
        return .5
    sign, end = (1 if value > 0 else -1), abs(value)
    factor = math.exp(math.lgamma((degrees + 1) / 2) - math.lgamma(degrees / 2)) / math.sqrt(degrees * math.pi)
    def density(x):
        return factor * (1 + x*x/degrees) ** (-(degrees+1)/2)
    def integrate(a, b, fa, fm, fb, whole, tolerance, depth):
        middle = (a+b)/2
        left_mid, right_mid = (a+middle)/2, (middle+b)/2
        fl, fr = density(left_mid), density(right_mid)
        left = (middle-a)*(fa+4*fl+fm)/6
        right = (b-middle)*(fm+4*fr+fb)/6
        delta = left+right-whole
        if depth == 0 or abs(delta) <= 15*tolerance:
            return left+right+delta/15
        return integrate(a,middle,fa,fl,fm,left,tolerance/2,depth-1) + integrate(middle,b,fm,fr,fb,right,tolerance/2,depth-1)
    fa, fm, fb = density(0), density(end/2), density(end)
    result = integrate(0,end,fa,fm,fb,end*(fa+4*fm+fb)/6,1e-13,24)
    return .5+sign*result


def t_quantile(probability, degrees):
    require(number(probability) and .5 < probability < 1, "invalid_t_probability")
    low, high = 0., 1.
    while t_cdf(high, degrees) < probability:
        high *= 2
        require(high < 1e6, "t_quantile_bracket_failure")
    for _ in range(64):
        middle = (low+high)/2
        if t_cdf(middle,degrees) < probability:
            low = middle
        else:
            high = middle
    return (low+high)/2


@lru_cache(maxsize=1)
def critical_value():
    return t_quantile(1-ALPHA/(2*N_CONTRASTS), FORMAL_WINDOWS-1)


@lru_cache(maxsize=1)
def exploratory_critical_value():
    return t_quantile(.975, FORMAL_WINDOWS-1)


def default_protocol():
    """Return a template; caller must fill concrete identities before freezing."""
    success = {"success": True, "source_final_released": True,
               "lease_final_correct": True, "closed_backing_bytes": 0,
               "final_resource_state": "CLOSED_ZERO"}
    def variant(name, role, argv, **match):
        return {"executable_role": role, "input_role": "dex_input" if role == "dex_formal" else "image_input",
                "argv": argv, "native_match": {"variant": name, **match}}
    dex_variants = {name: variant(name,"dex_formal",["@input","--variant",name,"--case","normal","--cpu","2"])
                    for name in ("D-C0","D-Z0","D-C1","D-C2")}
    image_variants = {}
    for name, provider, layout in (("I-DF","direct","full"),("I-DR","direct","ring"),
                                    ("I-SR","system","ring"),("I-AR","arena","ring")):
        image_variants[name] = variant(name,"image_formal",["@input","--provider",provider,"--layout",layout,"--frames","3","--cpu","2"],provider=provider,layout=layout)
    resources = {"logical_requested_bytes":"logical_requested_bytes",
                 "unique_owned_backing_peak_bytes":"unique_owned_backing_peak_bytes",
                 "external_source_capacity_bytes":"external_source_capacity_bytes",
                 "retained_before_collect_bytes":"retained_before_collect_bytes",
                 "reserved_final_bytes":"reserved_final_bytes",
                 "metadata_bytes":"memory_metadata_bytes", "rss_pss_bytes":"rss_pss_bytes",
                 "energy_joules":"energy"}
    def contrasts(ordinary, interface, arena, whole):
        return [{"id":name,"candidate":pair[0],"reference":pair[1],"attribution":label}
                for name,pair,label in (("ordinary",ordinary,"ORDINARY_CONSUMER"),
                                       ("interface",interface,"MEMORY_INTERFACE_SAME_WORK"),
                                       ("arena",arena,"ARENA_VS_MEMORY_SYSTEM"),
                                       ("memory_net",whole,"MEMORY_VS_PREDECLARED_ORDINARY_BASELINE"))]
    common = {"schema_version":2,"case":"normal","diagnostic":False,"phases":None}
    return {"schema_version":2,"suite_schema":"FOUR_VARIANT_WINDOW_V1",
            "experiment_id":"MM-v2-four-variant-suite-20261030","attempt_id":None,"estimand_id":"complete_owner_task_v2",
            "state":"DRAFT_NOT_FROZEN","device_execution":"NOT_AUTHORIZED_BY_DRAFT",
            "parent_protocol_file_sha256":None,
            "sampling":{"pilot_windows":4,"formal_windows":12,"blocks_per_family_window":2,
                        "tasks_per_block":8,"per_family_total_task_cap":256,"suite_total_task_cap":512,
                        "minimum_window_start_gap_seconds":120,"order_seed":20261030,
                        "precision_target_log_half_width":.01,"half_shift_warning_log":.01,
                        "familywise_alpha":.05,"simultaneous_contrasts_per_family":4,
                        "pilot_pooled":False,"repeated_pilot":False,"favorable_early_stop":False,
                        "pilot_precision_stop":"Stop affected family only; correctness failure stops entire suite",
                        "descriptive_memory_target":{"minimum_point_gain":.03,"gain_interval_lower_gt":0.,
                            "maximum_source_plus_backing_ratio":1.05,"qualification":"DESCRIPTIVE_ONLY_NOT_PERFORMANCE_PASS"},
                        "outer_inference":"DESCRIPTIVE_SAME_DAY_SHARED_ENVIRONMENT_NOT_ESTABLISHED_INDEPENDENT"},
            "families":{
                "dex":{"variants":dex_variants,"semantic_identity":{**common,"workload":"dex_source_strategy_complete_owner_v2",
                    "source_sha256":"83ac99bc338e8bfc1980e382a4c318871cac0364dc449ae7b0316f601c136ab0",
                    "input_bytes":10540184,"method_count":54147,"result_checksum":12701547145945416225},
                    "required_success":dict(success),"resource_fields":{**resources,"parser_peak_owned_bytes":"parser_peak_owned_bytes"},
                    "resource_limits":{},"contrasts":contrasts(("D-Z0","D-C0"),("D-C1","D-C0"),("D-C2","D-C1"),("D-C2","D-Z0")),
                    "secondary_contrasts":[{"id":"arena_vs_direct_copy","candidate":"D-C2","reference":"D-C0",
                                            "attribution":"SECONDARY_WHOLE_STRATEGY_VS_DIRECT_COPY"}]},
                "image":{"variants":image_variants,"semantic_identity":{**common,"workload":"image_workspace_complete_owner_v2",
                    "source_sha256":None,"input_bytes":None,"width":None,"height":None,"frames":3,
                    "completed_frames":3,"result_checksum":None,"output_hash_fnv64":None,"gray_conversions":None},
                    "required_success":dict(success),"resource_fields":dict(resources),"resource_limits":{},
                    "contrasts":contrasts(("I-DR","I-DF"),("I-SR","I-DR"),("I-AR","I-SR"),("I-AR","I-DR")),
                    "secondary_contrasts":[{"id":"arena_ring_vs_direct_full","candidate":"I-AR","reference":"I-DF",
                                            "attribution":"SECONDARY_WHOLE_STRATEGY_VS_OTHER_ORDINARY_LAYOUT"}]}},
            "environment_limits":{"battery_min_deci_c":0,"battery_max_deci_c":400,
                                  "thermal_status_max":0,"background_cpu_busy_max":.5},
            "freeze":{"device_profile":None,"artifact_closure":None,"functional_receipt_sha256":None,
                      "scheduled_windows":None,"single_task_timeout_seconds":None,
                      "session_wall_cap_seconds":None,"overall_wall_cap_seconds":None,
                      "maximum_environment_waits":None,"cadence":None,"resource_budgets":None},
            "performance_qualified":False,"BIL_auto_enable":False}


def parse_start(text):
    value = datetime.fromisoformat(text.replace("Z","+00:00"))
    require(value.tzinfo is not None, "window_start_must_include_timezone")
    return value.timestamp()


def protocol_errors(protocol, require_frozen=True):
    errors=[]
    def check(test, why):
        if not test:
            errors.append(why)
    check(protocol.get("suite_schema")=="FOUR_VARIANT_WINDOW_V1","wrong_suite_schema")
    for key in ("experiment_id","attempt_id","estimand_id"):
        check(isinstance(protocol.get(key),str) and bool(protocol[key].strip()),"missing_identity:"+key)
    if require_frozen:
        check(protocol.get("state")=="FROZEN_BEFORE_PILOT","protocol_not_frozen")
        check(protocol.get("device_execution")=="AUTHORIZED_CURRENT_BATCH","execution_not_frozen_authorized")
    sampling=protocol.get("sampling",{})
    for key,expected in {"pilot_windows":4,"formal_windows":12,"blocks_per_family_window":2,
                         "tasks_per_block":8,"per_family_total_task_cap":256,"suite_total_task_cap":512,
                         "simultaneous_contrasts_per_family":4,"familywise_alpha":.05,
                         "pilot_pooled":False,"repeated_pilot":False,"favorable_early_stop":False}.items():
        check(type(sampling.get(key)) is type(expected) and sampling[key]==expected,"sampling_contract:"+key)
    check(sampling.get("outer_inference")=="DESCRIPTIVE_SAME_DAY_SHARED_ENVIRONMENT_NOT_ESTABLISHED_INDEPENDENT","unsupported_outer_inference_claim")
    families=protocol.get("families",{})
    check(set(families)==set(FAMILY_NAMES),"requires_exact_dex_image_families")
    for name,family in families.items():
        variants=family.get("variants",{})
        check(len(variants)==4,"requires_four_variants:"+name)
        contrasts=family.get("contrasts",[])
        check(len(contrasts)==4 and len({c.get("id") for c in contrasts})==4,"requires_four_distinct_contrasts:"+name)
        check(all(c.get("candidate") in variants and c.get("reference") in variants and c.get("candidate")!=c.get("reference") for c in contrasts),"invalid_contrast_variant:"+name)
        secondary=family.get("secondary_contrasts",[])
        check(len(secondary)==1 and all(c.get("candidate") in variants and c.get("reference") in variants and c.get("candidate")!=c.get("reference") for c in secondary),"requires_one_secondary_baseline_contrast:"+name)
        check(bool(family.get("semantic_identity")) and all(v is not None for k,v in family.get("semantic_identity",{}).items() if k!="phases"),"unfilled_semantic_identity:"+name)
        check(bool(family.get("resource_limits")),"missing_resource_limits:"+name)
    freeze=protocol.get("freeze",{})
    for key in ("device_profile","artifact_closure","functional_receipt_sha256","resource_budgets"):
        check(bool(freeze.get(key)),"missing_freeze_field:"+key)
    for key in ("single_task_timeout_seconds","session_wall_cap_seconds","overall_wall_cap_seconds","maximum_environment_waits"):
        check(number(freeze.get(key)) and freeze[key]>0,"invalid_freeze_bound:"+key)
    cadence=freeze.get("cadence",{}) or {}
    for key in ("busy_interval_min_ns","busy_interval_max_ns","observation_to_launch_max_ns"):
        check(integer(cadence.get(key),1),"invalid_cadence:"+key)
    check(cadence.get("busy_interval_max_ns",0)>=cadence.get("busy_interval_min_ns",1),"inverted_busy_interval")
    schedules=freeze.get("scheduled_windows")
    check(isinstance(schedules,list) and len(schedules)==16,"requires_16_scheduled_windows")
    if isinstance(schedules,list):
        seen=set(); previous=None
        for index,item in enumerate(schedules):
            phase="pilot" if index<4 else "formal"
            check(item.get("phase")==phase,"window_phase_order")
            key=item.get("window_id")
            check(isinstance(key,str) and key not in seen,"window_id_missing_or_duplicate")
            seen.add(key)
            check(bool(item.get("day_id")) and bool(item.get("environment_id")),"window_hierarchy_missing")
            try:
                stamp=parse_start(item["start_utc"])
                if previous is not None:
                    check(stamp-previous>=sampling.get("minimum_window_start_gap_seconds",120),"window_start_gap_below_frozen_minimum")
                previous=stamp
            except (KeyError,ValueError,TypeError,AttributeError):
                errors.append("invalid_window_start")
    return errors


def make_plan(protocol):
    require(not protocol_errors(protocol,False),"protocol_fields_not_ready:"+",".join(protocol_errors(protocol,False)))
    rng=random.Random(protocol["sampling"]["order_seed"])
    entries=[]
    for index,window in enumerate(protocol["freeze"]["scheduled_windows"]):
        family_order=FAMILY_NAMES if index%2==0 else tuple(reversed(FAMILY_NAMES))
        for family in family_order:
            names=list(protocol["families"][family]["variants"])
            rng.shuffle(names)
            for block in range(BLOCKS):
                sequence=names[block*2:]+names[:block*2]
                order=sequence+list(reversed(sequence))
                for position,variant in enumerate(order):
                    entries.append({"task_id":f"{window['phase']}:{window['window_id']}:{family}:b{block}:p{position}",
                        "family":family,"phase":window["phase"],"window_id":window["window_id"],
                        "session_id":family+"-"+window["window_id"],"block_id":block,"position":position,"variant":variant,
                        "scheduled_window_start_utc":window["start_utc"],"day_id":window["day_id"],"environment_id":window["environment_id"]})
    return {"schema_version":2,"suite_schema":"FOUR_VARIANT_WINDOW_V1","state":"FROZEN_ORDER_BEFORE_PILOT" if protocol.get("state")=="FROZEN_BEFORE_PILOT" else "DRAFT_ORDER_ONLY",
            "experiment_id":protocol["experiment_id"],"attempt_id":protocol["attempt_id"],"estimand_id":protocol["estimand_id"],
            "protocol_canonical_sha256":digest(protocol),"entries":entries,"task_cap":512,
            "statistics":{"unit":"prescheduled_window_mean","contrasts_per_family":4,"familywise_alpha":.05,
                          "formal_df":11,"bonferroni_t_critical":critical_value(),"all_intervals_descriptive":True},
            "rule":"Each mirrored block has two tasks per variant; no pilot pooling, repeats, replacement or favorable stop."}


def freeze_errors(protocol,plan):
    errors=protocol_errors(protocol)
    if plan.get("protocol_canonical_sha256")!=digest(protocol):
        errors.append("protocol_plan_canonical_hash_mismatch")
    if plan.get("state")!="FROZEN_ORDER_BEFORE_PILOT":
        errors.append("plan_not_frozen")
    if not errors:
        expected=make_plan(protocol)
        if canonical(expected)!=canonical(plan):
            errors.append("plan_differs_from_deterministic_frozen_order")
    return errors


def native_errors(protocol,entry,native):
    family=protocol["families"][entry["family"]]
    errors=[]
    for group in (family["semantic_identity"],family["required_success"],family["variants"][entry["variant"]]["native_match"]):
        for field,expected in group.items():
            actual=native.get(field)
            if field not in native or type(actual) is not type(expected) or actual!=expected:
                errors.append("native_contract:"+field)
    if not integer(native.get("total_ns"),1):
        errors.append("invalid_total_ns")
    cpu=protocol["freeze"]["device_profile"].get("selected_cpu")
    if integer(cpu):
        for field in ("cpu_before","cpu_after"):
            if type(native.get(field)) is not int or native[field]!=cpu:
                errors.append("native_affinity:"+field)
    return errors


def resource_view(protocol,family,native):
    values={}; missing={}
    for column,field in protocol["families"][family]["resource_fields"].items():
        values[column]=native.get(field)
        if values[column] is None:
            missing[column]="NATIVE_UNAVAILABLE:"+field if field in native else "NOT_APPLICABLE_OR_MISSING:"+field
    return {"values":values,"unavailable_reasons":missing,
            "scope":"Reported columns may be peak, before_collect or final; never add them as one simultaneous total."}


def resource_failures(protocol,family,variant,native):
    errors=[]
    limits=protocol["families"][family]["resource_limits"].get(variant,{})
    for field,maximum in limits.items():
        value=native.get(field)
        if not number(value) or value<0 or value>maximum:
            errors.append("resource_budget:"+field)
    return errors


def environment_errors(protocol,row):
    errors=[]; env=obj(row.get("environment")); post=obj(row.get("environment_after"))
    limits=protocol["environment_limits"]; cadence=protocol["freeze"]["cadence"]
    if env.get("complete") is not True or env.get("valid") is not True or post.get("complete") is not True or post.get("valid") is not True:
        errors.append("environment_not_explicitly_complete_valid")
    for label,observation in (("before",env),("after",post)):
        battery,thermal=observation.get("battery_deci_c"),observation.get("thermal_status")
        if not integer(battery) or not limits["battery_min_deci_c"]<=battery<=limits["battery_max_deci_c"]:
            errors.append(label+":invalid_battery")
        if not integer(thermal) or thermal>limits["thermal_status_max"]:
            errors.append(label+":invalid_thermal")
    if not number(env.get("background_cpu_busy")) or not 0<=env["background_cpu_busy"]<=limits["background_cpu_busy_max"]:
        errors.append("invalid_background_cpu_busy")
    domain=env.get("clock_domain")
    if domain!="DEVICE_BOOTTIME_PROC_UPTIME_NS":
        errors.append("unknown_environment_clock_domain")
    if post.get("clock_domain")!=domain:
        errors.append("post_environment_clock_domain_mismatch")
    for field in ("busy_begin_monotonic_ns","busy_end_monotonic_ns","thermal_observed_monotonic_ns",
                  "busy_observation_lower_bound_ns","busy_observation_upper_bound_ns"):
        if not integer(env.get(field),1):
            errors.append("invalid_device_time:"+field)
    if all(integer(env.get(k),1) for k in ("busy_begin_monotonic_ns","busy_end_monotonic_ns","busy_observation_lower_bound_ns","busy_observation_upper_bound_ns")):
        observed=env["busy_end_monotonic_ns"]-env["busy_begin_monotonic_ns"]
        lower,upper=env["busy_observation_lower_bound_ns"],env["busy_observation_upper_bound_ns"]
        if not cadence["busy_interval_min_ns"]<=lower<=observed<=upper<=cadence["busy_interval_max_ns"]:
            errors.append("actual_busy_observation_interval_outside_frozen_bounds")
    if not integer(env.get("device_end_monotonic_ns"),1) or not integer(post.get("device_end_monotonic_ns"),1):
        errors.append("missing_device_environment_end")
    elif not (env.get("thermal_observed_monotonic_ns",0)<=env["device_end_monotonic_ns"]<=post.get("thermal_observed_monotonic_ns",0)<=post["device_end_monotonic_ns"]):
        errors.append("device_environment_time_order")
    clocks=obj(row.get("clocks"))
    names=("observation_begin_monotonic_ns","observation_end_monotonic_ns","launch_monotonic_ns","completion_monotonic_ns")
    if not all(integer(clocks.get(k),1) for k in names):
        errors.append("missing_host_monotonic_time")
    else:
        begin,end,launch,complete=(clocks[k] for k in names)
        if not begin<end<=launch<complete:
            errors.append("host_monotonic_time_order")
        if launch-end>cadence["observation_to_launch_max_ns"]:
            errors.append("observation_to_launch_exceeded")
    return errors


def interval(values,coefficient):
    if not values:
        return None
    mean=st.mean(values)
    half=coefficient*st.stdev(values)/math.sqrt(len(values)) if len(values)==FORMAL_WINDOWS else None
    result={"mean_log_ratio":mean,"time_ratio":math.exp(mean),"gain":-math.expm1(mean),
            "log_half_width":half,"log_interval":[mean-half,mean+half] if half is not None else None,
            "gain_interval":[-math.expm1(mean+half),-math.expm1(mean-half)] if half is not None else None}
    if half is None:
        result["interval_unavailable_reason"]="FORMAL_REQUIRES_EXACTLY_12_COMPLETE_VALID_WINDOWS"
    return result


def capture_order_errors(protocol,plan,records,phase):
    """Check physical suite order and actual UTC windows, without sorting rows."""
    expected=[entry for entry in plan["entries"] if entry["phase"]==phase]
    positions={entry["task_id"]:index for index,entry in enumerate(expected)}
    errors=[]; previous=-1; seen_windows=set(); previous_start=None
    cadence=protocol["freeze"]["cadence"]
    for index,row in enumerate(records):
        if row.get("phase") not in ("pilot","formal"):
            errors.append("unrecognized_phase_row:"+str(index))
            continue
        if row.get("phase")!=phase:
            continue
        position=positions.get(row.get("task_id"))
        if position is None or position<=previous:
            errors.append("global_physical_suite_order_or_duplicate:"+str(index))
        if position is not None:
            previous=position
        window=row.get("window_id")
        if window in seen_windows:
            continue
        seen_windows.add(window)
        try:
            actual=parse_start(row["started_at_utc"])
            schedule=parse_start(row["scheduled_window_start_utc"])
            if not 0<=actual-schedule<=cadence.get("window_start_lateness_max_seconds",60):
                errors.append("window_actual_start_outside_schedule:"+str(window))
            if previous_start is not None and actual-previous_start<cadence.get("minimum_actual_window_start_gap_seconds",90):
                errors.append("actual_window_start_gap_too_short:"+str(window))
            previous_start=actual
        except (ValueError,KeyError,TypeError,AttributeError):
            errors.append("missing_actual_window_UTC_start:"+str(window))
    return errors


def summarize_resources(protocol,family,rows):
    summaries={}
    for variant in protocol["families"][family]["variants"]:
        native=[obj(row.get("native")) for row in rows if row.get("variant")==variant]
        columns={}
        for column,source in protocol["families"][family]["resource_fields"].items():
            values=[item[source] for item in native if number(item.get(source)) and item[source]>=0]
            columns[column]={"observed":len(values),"unavailable":len(native)-len(values),
                             "minimum":min(values) if values else None,"maximum":max(values) if values else None}
        simultaneous=[item["external_source_capacity_bytes"]+item["unique_owned_backing_peak_bytes"]
                      for item in native if number(item.get("external_source_capacity_bytes")) and
                      number(item.get("unique_owned_backing_peak_bytes"))]
        summaries[variant]={"native_rows":len(native),"columns":columns,
                            "source_plus_input_or_workspace_unique_capacity_max":max(simultaneous) if simultaneous else None,
                            "sum_scope":"Common preloaded source remains alive through workspace peak; parser, metadata and RSS excluded."}
    return summaries


def contrast_resource_ratio(summaries,candidate,reference):
    def ratio(left,right):
        if left is None or right is None:
            return {"value":None,"unavailable_reason":"MISSING_RESOURCE_OBSERVATION"}
        if right==0:
            return {"value":None,"unavailable_reason":"ZERO_REFERENCE_BACKING_NOT_A_VALID_RATIO_DENOMINATOR"}
        return {"value":left/right,"unavailable_reason":None}
    left,right=summaries[candidate],summaries[reference]
    column="unique_owned_backing_peak_bytes"
    owned=ratio(left["columns"].get(column,{}).get("maximum"),right["columns"].get(column,{}).get("maximum"))
    total=ratio(left["source_plus_input_or_workspace_unique_capacity_max"],right["source_plus_input_or_workspace_unique_capacity_max"])
    return {"owned_backing_peak_ratio":owned,"source_plus_input_or_workspace_capacity_ratio":total,
            "scope":"Requested input/workspace and simultaneous preloaded-source capacity only; excludes parser/metadata/RSS/energy."}


def analyze_family(protocol,plan,records,phase,family,journal_errors=None):
    require(phase in ("pilot","formal") and family in FAMILY_NAMES,"unknown_family_or_phase")
    frozen=freeze_errors(protocol,plan)
    expected=[entry for entry in plan["entries"] if entry["phase"]==phase and entry["family"]==family]
    selected=[row for row in records if row.get("phase")==phase and row.get("family")==family]
    incomplete=list(journal_errors or [])+capture_order_errors(protocol,plan,records,phase)
    functional=[]; resources=[]; seen=set(); groups={}
    if len(selected)!=len(expected):
        incomplete.append("planned_task_count_mismatch")
    plan_hash=digest(plan); previous_end=None; epoch=None
    for index,row in enumerate(selected):
        prefix="row_%d:"%index
        task=row.get("task_id")
        if task in seen:
            incomplete.append(prefix+"duplicate_task_id")
        seen.add(task)
        entry=expected[index] if index<len(expected) else None
        if entry is None or any(row.get(k)!=v for k,v in entry.items()):
            incomplete.append(prefix+"physical_order_or_plan_identity_mismatch")
            continue
        for field in ("experiment_id","attempt_id","estimand_id"):
            if row.get(field)!=protocol[field]:
                functional.append(prefix+"wrong_"+field)
        if row.get("plan_canonical_sha256")!=plan_hash:
            functional.append(prefix+"wrong_plan_canonical_sha256")
        native=obj(row.get("native")); command=obj(row.get("command_result"))
        if command:
            try:
                parsed=strict_json(command["stdout"])
                require(canonical(parsed)==canonical(native),"stdout_native_mismatch")
                require(type(command.get("exit_code")) is int and command["exit_code"]==0 and command.get("timed_out") is False,"command_not_successful")
            except (ValueError,KeyError,TypeError):
                functional.append(prefix+"stdout_or_command_binding_failed")
            functional.extend(prefix+why for why in native_errors(protocol,entry,native))
            resources.extend(prefix+why for why in resource_failures(protocol,family,entry["variant"],native))
            if row.get("resources")!=resource_view(protocol,family,native):
                functional.append(prefix+"native_resource_mapping_mismatch")
        else:
            incomplete.append(prefix+"native_not_executed_or_command_receipt_missing")
        if row.get("success") is not True:
            incomplete.append(prefix+"task_capture_not_complete")
        incomplete.extend(prefix+why for why in environment_errors(protocol,row))
        current_epoch=row.get("host_clock_epoch_id")
        if not isinstance(current_epoch,str) or not current_epoch or (epoch is not None and epoch!=current_epoch):
            incomplete.append(prefix+"host_clock_epoch_missing_or_changed")
        epoch=current_epoch
        clocks=obj(row.get("clocks")); begin=clocks.get("observation_begin_monotonic_ns"); end=clocks.get("completion_monotonic_ns")
        if integer(begin,1) and previous_end is not None and begin<previous_end:
            incomplete.append(prefix+"overlapping_or_reordered_host_tasks")
        if integer(end,1):
            previous_end=end
        groups.setdefault((entry["window_id"],entry["block_id"]),[]).append(row)
    family_spec=protocol["families"][family]; blocks=[]
    all_contrasts=[(c,True) for c in family_spec["contrasts"]]+[(c,False) for c in family_spec["secondary_contrasts"]]
    by_contrast={c["id"]:{} for c,_ in all_contrasts}
    for key,rows in groups.items():
        times={variant:[obj(r.get("native")).get("total_ns") for r in rows if r.get("variant")==variant] for variant in family_spec["variants"]}
        valid=len(rows)==8 and all(len(vals)==2 and all(integer(t,1) for t in vals) for vals in times.values())
        contrasts={}
        if valid:
            logs={v:st.mean(map(math.log,vals)) for v,vals in times.items()}
            for contrast,_ in all_contrasts:
                value=logs[contrast["candidate"]]-logs[contrast["reference"]]
                contrasts[contrast["id"]]=value
                by_contrast[contrast["id"]].setdefault(key[0],[]).append(value)
        blocks.append({"window_id":key[0],"block_id":key[1],"descriptive_log_ratios":contrasts})
    critical=critical_value(); contrast_reports={}; secondary_reports={}; all_feasible=True
    resource_summaries=summarize_resources(protocol,family,selected)
    expected_windows=PILOT_WINDOWS if phase=="pilot" else FORMAL_WINDOWS
    for contrast,primary in all_contrasts:
        windows=by_contrast[contrast["id"]]
        values=[st.mean(vals) for vals in windows.values() if len(vals)==BLOCKS]
        complete=len(values)==expected_windows
        coefficient=critical if primary else exploratory_critical_value()
        estimate=interval(values,coefficient) if phase=="formal" else None
        planned_half=coefficient*st.stdev(values)/math.sqrt(FORMAL_WINDOWS) if phase=="pilot" and complete else None
        feasible=planned_half is not None and planned_half<=protocol["sampling"]["precision_target_log_half_width"]
        all_feasible=all_feasible and feasible if phase=="pilot" and primary else all_feasible
        shift=st.mean(values[6:])-st.mean(values[:6]) if phase=="formal" and complete else None
        trend=shift is not None and abs(shift)>protocol["sampling"]["half_shift_warning_log"]
        target=contrast_reports if primary else secondary_reports
        target[contrast["id"]]={**contrast,"windows_observed":len(values),
            "window_means":[{"window_id":w,"mean_log_ratio":st.mean(vals),"blocks":len(vals)} for w,vals in windows.items()],
            "estimate":estimate,"pilot_planning_log_half_width_for_12_formal_windows":planned_half,
            "pilot_feasible":feasible if phase=="pilot" else None,
            "actual_precision_met":estimate["log_half_width"]<=.01 if estimate and estimate["log_half_width"] is not None else None,
            "formal_half_shift_log":shift,"formal_trend_warning":trend,
            "simultaneous_interval_class":"DESCRIPTIVE_BONFERRONI_FAMILYWISE_95_PERCENT_FOUR_CONTRASTS" if primary else "SECONDARY_EXPLORATORY_UNADJUSTED_95_PERCENT_NO_SIMULTANEOUS_COVERAGE",
            "pilot_precision_screen_applies":primary,
            "resource_comparison":contrast_resource_ratio(resource_summaries,contrast["candidate"],contrast["reference"]),
            "performance_qualified":False}
        if not complete:
            incomplete.append("missing_complete_windows:"+contrast["id"])
    if phase=="formal" and (functional or incomplete or frozen or resources):
        for item in list(contrast_reports.values())+list(secondary_reports.values()):
            estimate=item["estimate"]
            if estimate is not None:
                estimate.update({"log_half_width":None,"log_interval":None,"gain_interval":None})
                estimate.setdefault("interval_unavailable_reason","FORMAL_HAS_FUNCTIONAL_CAPTURE_FREEZE_OR_RESOURCE_FAILURE")
            item["actual_precision_met"]=None
    if functional:
        decision="STOP_ENTIRE_SUITE_CORRECTNESS_OR_LIFETIME_FAILURE"
    elif incomplete or frozen:
        decision="INCOMPLETE"
    elif resources:
        decision="RESOURCE_BUDGET_FAILURE"
    elif phase=="pilot":
        decision="PILOT_FEASIBLE_FORMAL_ALLOWED" if all_feasible else "STOP_FAMILY_PILOT_PRECISION_INSUFFICIENT"
    else:
        decision="INCONCLUSIVE_OUTER_INDEPENDENCE_NOT_ESTABLISHED"
    memory_net=contrast_reports.get("memory_net",{}); estimated=memory_net.get("estimate")
    resource_ratio=obj(memory_net.get("resource_comparison")).get("source_plus_input_or_workspace_capacity_ratio",{}).get("value")
    target=protocol["sampling"]["descriptive_memory_target"]
    descriptive_target={"prespecified":target,"point_gain_gate":estimated["gain"]>=target["minimum_point_gain"] if estimated else None,
        "gain_interval_gate":estimated["gain_interval"][0]>target["gain_interval_lower_gt"] if estimated and estimated["gain_interval"] else None,
        "source_plus_backing_gate":resource_ratio<=target["maximum_source_plus_backing_ratio"] if resource_ratio is not None else None,
        "performance_qualified":False,"does_not_override_precision_trend_or_outer_limits":True}
    return {"schema_version":2,"suite_schema":"FOUR_VARIANT_WINDOW_V1","phase":phase,"family":family,
            "decision":decision,"performance_qualified":False,"BIL_auto_enable":False,
            "protocol_canonical_sha256":digest(protocol),"plan_canonical_sha256":plan_hash,
            "attempted_tasks":len(selected),"planned_tasks":len(expected),
            "functional_failures":functional,"incomplete_reasons":incomplete,"resource_budget_failures":resources,"freeze_errors":frozen,
            "blocks":blocks,"contrasts":contrast_reports,"secondary_contrasts":secondary_reports,
            "resources_by_variant":resource_summaries,"descriptive_memory_target":descriptive_target,
            "statistics":{"critical_value":critical,"degrees_of_freedom":11,"familywise_alpha":.05,
                          "contrasts":4,"primary_unit":"window_mean_of_two_mirrored_blocks","pilot_pooled":False},
            "limitations":["Same-day shared environment does not establish independent outer windows; all intervals descriptive.",
                           "Bonferroni coverage assumes the underlying t model; multiplicity adjustment does not repair dependence.",
                           "Both ordinary baselines are disclosed through primary and secondary contrasts; neither layout or source strategy is assumed faster.",
                           "Input capacity, backing, retained and parser high-water columns have separate scopes; unknown RSS and energy are not zero.",
                           "Ordinary consumer, interface, arena and whole migration contrasts are separate; percentages are not added."]}


def pilot_eligibility(protocol,plan,report,family):
    """Bind a completed pilot report before admitting formal tasks for a family."""
    errors=[]
    for field,value in (("phase","pilot"),("family",family),("protocol_canonical_sha256",digest(protocol)),
                        ("plan_canonical_sha256",digest(plan))):
        if report.get(field)!=value:
            errors.append("pilot_report_identity:"+field)
    if report.get("functional_failures"):
        errors.append("pilot_correctness_failure_stops_entire_suite")
    if report.get("freeze_errors") or report.get("incomplete_reasons") or report.get("resource_budget_failures"):
        errors.append("pilot_incomplete_or_resource_failure")
    if report.get("decision")!="PILOT_FEASIBLE_FORMAL_ALLOWED":
        errors.append("pilot_does_not_admit_formal_family")
    if report.get("attempted_tasks")!=64 or report.get("planned_tasks")!=64:
        errors.append("pilot_task_count_not_64")
    for name in ("ordinary","interface","arena","memory_net"):
        item=obj(report.get("contrasts")).get(name,{})
        if item.get("pilot_feasible") is not True or item.get("windows_observed")!=4:
            errors.append("pilot_precision_or_window_gate:"+name)
    return {"family":family,"formal_allowed":not errors,"reasons":errors,
            "performance_qualified":False,"rule":"One completed pilot; no repeated pilot or precision-driven sample extension."}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol",type=Path)
    parser.add_argument("--template",type=Path)
    parser.add_argument("--plan",type=Path)
    parser.add_argument("--journal",type=Path)
    parser.add_argument("--phase",choices=("pilot","formal"))
    parser.add_argument("--family",choices=FAMILY_NAMES)
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    if args.template:
        write_new_json(args.template,default_protocol()); return
    require(args.protocol is not None,"protocol_required")
    protocol=strict_json(args.protocol.read_text(encoding="utf-8-sig"))
    if args.journal:
        require(args.plan is not None and args.phase and args.family and args.output,"analysis_arguments_required")
        plan=strict_json(args.plan.read_text(encoding="utf-8-sig")); records,errors=load_journal(args.journal)
        report=analyze_family(protocol,plan,records,args.phase,args.family,errors)
        report.update({"protocol_file_sha256":file_sha(args.protocol),"plan_file_sha256":file_sha(args.plan),
                       "journal_file_sha256":file_sha(args.journal),"analysis_source_sha256":file_sha(__file__)})
        write_new_json(args.output,report)
        print(json.dumps({"decision":report["decision"],"family":args.family,"performance_qualified":False}))
    else:
        require(args.output is not None,"output_required_for_plan")
        result=make_plan(protocol); write_new_json(args.output,result)
        print(json.dumps({"state":result["state"],"tasks":len(result["entries"]),"plan_canonical_sha256":digest(result)}))


if __name__=="__main__":
    main()
