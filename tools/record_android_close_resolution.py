#!/usr/bin/env python3
"""Verify Android close investigation evidence and assemble a local delivery.

No ADB, testing, source-lock generation, dependency download or publication.
Missing/failed Android long rounds remain NOT_QUALIFIED. Input identifiers are
used only for a private in-memory scan; no raw serial, key or log is published.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'artifacts/android-close-investigation-20261030'
PACKAGE = 'io.github.jichuo1.meguviz.memory.pressureprobe.r8'
ROUND_PLANS = ('freezer-control-plan.json', 'freezer-bootstrap-plan.json',
               'retry-smoke-plan.json', 'retry-long-plan.json')
CONTROL_ROUNDS = (('freezer-control-background/receipt.json', 'freezer-control-plan.json', False),
                  ('freezer-control-foreground/receipt.json', 'freezer-control-plan.json', True),
                  ('freezer-bootstrap-background/receipt.json', 'freezer-bootstrap-plan.json', False),
                  ('freezer-bootstrap-foreground/receipt.json', 'freezer-bootstrap-plan.json', True))
NEW_HOST_TESTS = {'auto-attach-recovery', 'auto-java-recovery', 'auto-concurrent-poll', 'auto-error-quarantine'}
PUBLIC_CAPTURE_GOLDENS = {
    'tests/fixtures/continuation-validation/android-api34.stdout.txt',
    'tests/fixtures/continuation-validation/dex-arm64.stdout.txt',
}


def under_workspace(path):
    value = Path(path)
    if not value.is_absolute():
        value = ROOT / value
    value = value.resolve()
    require(value.is_relative_to(ROOT), 'distribution_path_outside_workspace')
    return value


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def relative(path):
    return path.resolve().relative_to(ROOT).as_posix()


def identity(path):
    path = path.resolve()
    require(path.is_relative_to(ROOT) and path.is_file(), 'identity_path_not_file_or_outside_workspace')
    return {'path': relative(path), 'bytes': path.stat().st_size, 'sha256': sha(path)}


def checked(item):
    path = (ROOT / item['path']).resolve()
    require(path.is_relative_to(ROOT) and path.is_file(), 'missing_file:' + str(item['path']))
    require(sha(path) == item['sha256'], 'hash_mismatch:' + str(item['path']))
    if 'bytes' in item:
        require(path.stat().st_size == item['bytes'], 'byte_count_mismatch:' + str(item['path']))
    return path


def aliases():
    """Only source-specific, SHA-verified artifact snapshots qualify as aliases."""
    records = []
    pre = BASE / 'source-pre-retry/manifest.json'
    if pre.is_file():
        value = read(pre)
        require(value.get('state') == 'EXACT_PRE_RETRY_SOURCE_SNAPSHOT', 'invalid_pre_retry_manifest')
        for item in value['files']:
            records.append({'original': item['source'], 'snapshot': item['snapshot'], 'sha256': item['sha256']})
    for path in sorted(BASE.glob('source-*/*alias*.json')):
        item = read(path)
        original = item.get('original_path', item.get('path'))
        require(original and all(k in item for k in ('snapshot', 'sha256')), 'invalid_alias:' + relative(path))
        records.append({'original': original, 'snapshot': item['snapshot'], 'sha256': item['sha256']})
    # These named snapshots record the first pre-bootstrap diagnostic source.
    fixed = {
        'adapters/android/sample/AndroidManifest-r8.xml': 'AndroidManifest-r8-before-bootstrap.xml.txt',
        'adapters/android/sample/PressureR8ForegroundActivity.java': 'PressureR8ForegroundActivity-before-bootstrap.java.txt',
        'adapters/android/sample/PressureR8Instrumentation.java': 'PressureR8Instrumentation-before-bootstrap.java.txt',
        'tools/build_pressure_r8.py': 'build_pressure_r8-before-bootstrap.py.txt',
    }
    for original, filename in fixed.items():
        path = BASE / 'source-baseline' / filename
        if path.is_file():
            records.append({'original': original, 'snapshot': relative(path), 'sha256': sha(path)})
    # Host 22-test receipt also locked this documentation snapshot. Its later
    # freezer/poll clarification is non-compiled; retain the exact tested-era
    # bytes without requiring a native recompile or calling new docs old proof.
    doc = BASE / 'source-pre-freezer-doc/ANDROID_PRESSURE.md.txt'
    if doc.is_file():
        old_doc_sha = 'e1d87cf4caa756e23cb774d83c4fb7f454dee86e4c3759658bdddb54d131321f'
        require(sha(doc) == old_doc_sha, 'pre_freezer_document_snapshot_mismatch')
        records.append({'original': 'docs/ANDROID_PRESSURE.md', 'snapshot': relative(doc), 'sha256': old_doc_sha})
    indexed = {}
    for item in records:
        path = (ROOT / item['snapshot']).resolve()
        require(path.is_relative_to(BASE) and path.is_file() and sha(path) == item['sha256'], 'invalid_alias_bytes')
        key = (item['original'], item['sha256'])
        if key in indexed:
            require(sha(indexed[key]) == sha(path), 'ambiguous_alias_bytes')
        indexed[key] = path
    return indexed


def checked_source(item, lookup):
    """A changed source may only resolve to its exact source-specific snapshot."""
    current = (ROOT / item['path']).resolve()
    require(current.is_relative_to(ROOT), 'source_path_outside_workspace')
    candidate = current if current.is_file() and sha(current) == item['sha256'] else lookup.get((item['path'], item['sha256']))
    require(candidate is not None and candidate.is_file() and sha(candidate) == item['sha256'], 'missing_exact_source_alias:' + item['path'])
    if 'bytes' in item:
        require(candidate.stat().st_size == item['bytes'], 'source_byte_count_mismatch:' + item['path'])
    return candidate


def verify_plans(extra_names=()):
    lookup = aliases()
    plans = {}
    names = set(ROUND_PLANS)
    names.update(path.name for path in BASE.glob('*-plan.json'))
    names.update(extra_names)
    for name in sorted(names):
        path = (BASE / name).resolve()
        require(path.is_relative_to(BASE), 'plan_outside_stage_evidence')
        if not path.is_file():
            plans[name] = {'state': 'MISSING_NOT_QUALIFIED'}
            continue
        value = read(path)
        require(value.get('state') == 'FROZEN_BEFORE_DEVICE_EXECUTION', 'invalid_frozen_plan:' + name)
        entries = value.get('files')
        require(isinstance(entries, list) and entries, 'empty_frozen_plan:' + name)
        seen = set()
        verified = []
        for item in entries:
            original = item['path']
            require(original not in seen, 'duplicate_frozen_file:' + original)
            seen.add(original)
            candidate = (ROOT / original).resolve()
            require(candidate.is_relative_to(ROOT), 'frozen_path_outside_workspace')
            if not candidate.is_file() or sha(candidate) != item['sha256']:
                candidate = lookup.get((original, item['sha256']))
                require(candidate is not None, 'missing_exact_frozen_source_alias:' + original)
            require(sha(candidate) == item['sha256'] and candidate.stat().st_size == item['bytes'], 'frozen_file_identity_mismatch:' + original)
            verified.append({'frozen_path': original, 'verified_bytes': identity(candidate)})
        expected_hashes = {item['sha256'] for item in entries}
        for key in ('build_receipt_sha256', 'APK_sha256', 'native_source_sha256', 'native_library_sha256'):
            require(value.get(key) in expected_hashes, 'plan_declared_identity_not_in_verified_files:' + name + ':' + key)
        plans[name] = {'state': 'VERIFIED', 'plan': identity(path), 'verified_files': verified,
                       'purpose': value.get('purpose'), 'minimum_ms': value.get('minimum_ms')}
    return plans


def verify_baseline():
    path = BASE / 'baseline-freezer.json'
    value = read(path)
    require(value.get('state') == 'OBSERVED' and value.get('freeze_controller_modified') is False and
            value.get('system_policy_modified') is False, 'baseline_freezer_scope')
    observations = value.get('observations')
    require(isinstance(observations, list) and len(observations) == 2, 'two_baseline_observations_required')
    for observation in observations:
        require(observation.get('cgroup_freeze') == '1' and 'frozen 1' in observation.get('cgroup_events', []) and
                observation.get('observed_thread_count') == 20 and observation.get('freezer_wait_thread_count') == 20 and
                observation.get('wait_channels') == {'do_freezer_trap': 20}, 'baseline_freezer_wait_mismatch')
    lldb_path = BASE / 'baseline-lldb-summary.json'
    lldb = read(lldb_path)
    require(lldb.get('state') == 'NO_THREAD_STACK_CAPTURED' and lldb.get('initial_target_still_traced') is True and
            lldb.get('manual_exact_own_server_only_TERM') is True and
            lldb.get('target_tracer_zero_and_running_state_verified') is True, 'baseline_LLDB_recovery_scope')
    return {'state': 'TWO_OWN_CGROUP_FREEZE_OBSERVATIONS_20_OF_20_THREADS', 'receipt': identity(path),
            'lldb': {'receipt': identity(lldb_path), 'state': 'NO_THREAD_STACK_CAPTURED',
                     'manual_own_server_detach': 'REPORTED_TRACER_ZERO_NOT_FREEZER_RELEASE'},
            'causation': 'OBSERVED_ENVIRONMENT_IN_THIS_REPRODUCTION_NOT_ACTOR_OR_ALL_HISTORICAL_STALLS'}


def verify_host():
    summary_path = BASE / 'retry-host/summary.json'
    summary = read(summary_path)
    require(summary.get('state') == 'PASS_RELEASE_AND_ASAN_SCOPED_HOST_ONLY', 'host_summary_not_pass')
    lookup = aliases()
    for source in summary.get('source_identity', []):
        checked_source(source, lookup)
    profiles = summary.get('profiles', [])
    require(len(profiles) == 2 and {p.get('address_sanitizer_enabled') for p in profiles} == {True, False}, 'both_host_profiles_required')
    for profile in profiles:
        require(profile.get('CTest_count') == 22 and profile.get('tests_passed') == 22, 'host_CTest_not_22_pass')
        names = set(profile.get('tests', []))
        require(all('jni_lock_' + name in names for name in NEW_HOST_TESTS), 'new_retry_tests_missing')
        checked(profile['public_log'])
    require(set(summary.get('new_tests', [])) == NEW_HOST_TESTS, 'retry_test_list_mismatch')
    red_path = checked(summary['red_counterexample'])
    red = read(red_path)
    require(red.get('state') == 'EXPECTED_RED_AUTOMATIC_CLEANUP_MISSING' and red.get('test_exit_code') == 1, 'red_counterexample_missing')
    require(red['result'].get('observed_auto_cleanup') is False and red['result'].get('manual_poll_then_refs_released') is True and
            red['result'].get('foreign_domain_remained_open') is True and red['result'].get('final_domain_closed_zero') is True and
            red['result'].get('real_ART') is False, 'red_counterexample_scope')
    saved = BASE / 'retry-host/reaper_autoclose.red.cpp.txt'
    original = next(x for x in red['inputs'] if x['path'].endswith('/reaper_autoclose.cpp'))
    require(saved.is_file() and sha(saved) == original['sha256'], 'red_counterexample_source_drift')
    checked(summary['pre_retry_exact_source_snapshot'])
    return {'state': 'PASS_SCOPED_HOST_RELEASE_22_ASAN_22', 'receipt': identity(summary_path),
            'red_counterexample': identity(red_path), 'new_tests': sorted(NEW_HOST_TESTS),
            'limits': summary.get('limits', [])}


def verify_distribution_host(path, expected_source_sha, lookup):
    if path is None or not path.is_file():
        return {'state': 'UNAVAILABLE_NOT_QUALIFIED', 'qualified': False}
    value = read(path)
    require(value.get('state') == 'PASS_SCOPED_HOST_INJECTED_FAILURES', 'distribution_host_summary_not_pass')
    sources = value.get('current_source_identity')
    require(isinstance(sources, list) and sources, 'distribution_host_source_identity_missing')
    for item in sources:
        checked_source(item, lookup)
    jni = [item for item in sources if item.get('path') == 'adapters/android/native/pressure_jni.cpp']
    require(len(jni) == 1 and jni[0]['sha256'] == expected_source_sha, 'distribution_host_JNI_source_mismatch')
    profiles = value.get('profiles', [])
    require(len(profiles) == 2 and {p.get('ASan') for p in profiles} == {True, False}, 'distribution_host_profiles_missing')
    for profile in profiles:
        require(profile.get('tests_passed') == 29, 'distribution_host_29_tests_required')
        checked(profile['sanitized_log'])
        checked(profile['test_binary']); checked(profile['core_binary'])
    examples = value.get('counterexamples', [])
    require(len(examples) == 7 and all(type(item.get('before', {}).get('exit_code')) is int and
            item['before']['exit_code'] != 0 and type(item.get('after', {}).get('exit_code')) is int and
            item['after']['exit_code'] == 0 for item in examples), 'distribution_old_new_failure_matrix')
    names = {item.get('case') for item in examples}
    expected = {'binding-exception-matrix'} | {f'snapshot-{kind}-{owner}'
                for kind in ('error', 'state', 'header') for owner in ('owned', 'external')}
    require(names == expected, 'distribution_failure_cases_missing')
    require('12' in value.get('binding_matrix', '') and value.get('physical_device') == 'NOT_EXECUTED_IN_THIS_HOST_TEST',
            'distribution_failure_matrix_scope')
    return {'state': 'PASS_SCOPED_HOST_RELEASE_29_ASAN_29', 'qualified': True, 'receipt': identity(path),
            'error_injection': 'SIX_SNAPSHOT_FAULT_CASES_ISOLATED_NOT_PRODUCTION_NORMAL_ZERO',
            'binding_matrix': '12_INJECTED_PENDING_JNI_EXCEPTION_CASES',
            'physical_device_qualification': False}


def round_result(name, plan_name, plans, required_minimum_ms=0):
    path, plan_path = BASE / name, BASE / plan_name
    if not path.is_file():
        return {'state': 'MISSING_NOT_QUALIFIED', 'receipt_path': relative(path), 'qualified': False}
    receipt = read(path)
    result = {'state': receipt.get('state', 'UNKNOWN_NOT_QUALIFIED'), 'receipt': identity(path),
              'stop_reason': receipt.get('stop_reason') or receipt.get('reason'), 'qualified': False}
    observed = receipt.get('cgroup_samples', [])
    if isinstance(observed, list):
        result['freezer_samples'] = len(observed)
        result['observed_frozen'] = any(s.get('mode') == '1' or s.get('frozen') == '1' for s in observed if isinstance(s, dict))
    plan = read(plan_path) if plan_path.is_file() else None
    if plan is None or plans.get(plan_name, {}).get('state') != 'VERIFIED':
        result['qualification_reason'] = 'MISSING_VERIFIED_PLAN'
        return result
    if receipt.get('plan_sha256') != sha(plan_path):
        result['qualification_reason'] = 'RECEIPT_PLAN_IDENTITY_MISMATCH'
        return result
    if receipt.get('state') != 'PASS_OBSERVED_UNFROZEN_SAMPLES' or receipt.get('normal_close_proven') is not True:
        result['qualification_reason'] = 'ROUND_NOT_COMPLETED_OR_NORMAL_CLOSE_NOT_PROVEN'
        return result
    profile = receipt.get('profile') if isinstance(receipt.get('profile'), dict) else {}
    report = receipt.get('android_report') if isinstance(receipt.get('android_report'), dict) else {}
    samples = receipt.get('cgroup_samples', [])
    reasons = []
    def check(condition, reason):
        if not condition: reasons.append(reason)
    check(profile.get('minimum_ms') == plan.get('minimum_ms') and profile.get('minimum_iterations') == plan.get('iterations'), 'PROFILE_DIFFERS_FROM_PLAN')
    check(type(plan.get('minimum_ms')) is int and plan['minimum_ms'] >= required_minimum_ms, 'MINIMUM_DURATION_TOO_SHORT')
    check(receipt.get('device', {}).get('api') == 33 and receipt.get('device', {}).get('abi') == 'arm64-v8a', 'DEVICE_SCOPE')
    actual_duration, actual_iterations = report.get('actual_duration_ms'), report.get('iterations_completed')
    check(report.get('status') == 'PASS' and type(actual_duration) is int and type(actual_iterations) is int and
          type(plan.get('minimum_ms')) is int and type(plan.get('iterations')) is int and
          actual_duration >= plan['minimum_ms'] and actual_iterations >= plan['iterations'], 'INCOMPLETE_WORK')
    check(report.get('final_backing_bytes_each_domain') == 0 and report.get('fire_and_forget_state') == 'PASS_REGISTRATION_REMOVED_WITHOUT_CALLER_POLL', 'CLOSE_OR_AUTOCLEANUP')
    check(report.get('performance_qualified') is False and report.get('BIL_integrated') is False and
          report.get('actual_low_memory_event') is False, 'QUALIFICATION_SCOPE')
    check(profile.get('foreground_requested') is True and profile.get('prestart_activity') is True and
          report.get('foreground_prestarted') is True and report.get('foreground_resumed') is True and
          report.get('foreground_finished') is True and report.get('foreground_pause_count_before_finish') == 0, 'FOREGROUND_BOOTSTRAP')
    check(receipt.get('APK', {}).get('sha256') == plan.get('APK_sha256') and
          receipt.get('APK', {}).get('build_receipt_sha256') == plan.get('build_receipt_sha256'), 'APK_OR_BUILD_IDENTITY')
    check(isinstance(samples, list) and len(samples) >= 2, 'MISSING_FREEZER_SAMPLES')
    if isinstance(samples, list) and samples:
        check(all(s.get('mode') == '0' and s.get('frozen') == '0' for s in samples), 'FROZEN_OR_UNKNOWN_SAMPLE')
        check(len({s.get('cgroup_path_sha256') for s in samples}) == 1 and samples[0].get('cgroup_path_sha256') is not None, 'CGROUP_IDENTITY_CHANGED')
        first_elapsed = samples[0].get('elapsed_seconds')
        last_elapsed = samples[-1].get('elapsed_seconds')
        check(type(first_elapsed) in (int, float) and 0 <= first_elapsed <= 10 and
              all(type(s.get('gap_seconds')) in (int, float) and 0 <= s['gap_seconds'] <= 10 for s in samples[1:]), 'FREEZER_OBSERVATION_GAP')
        check(type(last_elapsed) in (int, float) and type(actual_duration) is int and
              last_elapsed >= max(0, actual_duration / 1000 - 10), 'FREEZER_END_GAP')
    gate_names = {g.get('name') for g in receipt.get('gates', []) if isinstance(g, dict) and g.get('result') == 'PASS_OBSERVATIONAL_ONLY'}
    check({'initial', 'before-instrumentation', 'after'}.issubset(gate_names), 'DEVICE_ADMISSION_OR_RELEASE_GATE')
    result.update(qualified=not reasons, qualification_reason='PASS_OBSERVED_UNFROZEN_SAMPLES' if not reasons else 'INVALID_PASS_NOT_QUALIFIED',
                  reasons=reasons, actual_duration_ms=report.get('actual_duration_ms'),
                  iterations_completed=report.get('iterations_completed'),
                  freezer_samples=len(samples) if isinstance(samples, list) else None)
    return result


def bind_distribution_round(result, receipt_name, plan_name, build_file, apk, *, reason_required):
    """A completed old round cannot qualify a different installed APK."""
    if not result.get('qualified'):
        return result
    receipt_path, plan_path = BASE / receipt_name, BASE / plan_name
    receipt, plan = read(receipt_path), read(plan_path)
    report = receipt.get('android_report') if isinstance(receipt.get('android_report'), dict) else {}
    problems = []
    if (receipt.get('APK', {}).get('sha256') != sha(apk) or
            receipt.get('APK', {}).get('build_receipt_sha256') != sha(build_file) or
            plan.get('APK_sha256') != sha(apk) or plan.get('build_receipt_sha256') != sha(build_file)):
        problems.append('DISTRIBUTION_APK_OR_BUILD_IDENTITY_MISMATCH')
    if reason_required and (plan.get('reason_checks_required') is not True or
                            report.get('logical_unavailable_reason_checks') != 'PASS'):
        problems.append('LOGICAL_UNAVAILABLE_REASON_CHECKS_NOT_PROVEN')
    result['distribution_APK_sha256'] = sha(apk)
    result['reason_checks_required'] = reason_required
    if problems:
        result.update(qualified=False, qualification_reason='DISTRIBUTION_GATES_NOT_QUALIFIED')
        result.setdefault('reasons', []).extend(problems)
    return result


def verify_controls(plans):
    result = []
    for receipt_name, plan_name, foreground in CONTROL_ROUNDS:
        path = BASE / receipt_name
        if not path.is_file() or plans[plan_name]['state'] != 'VERIFIED':
            result.append({'receipt_path': relative(path), 'state': 'MISSING_NOT_QUALIFIED', 'foreground': foreground})
            continue
        value, plan = read(path), read(BASE / plan_name)
        require(value.get('plan_sha256') == sha(BASE / plan_name) and
                value.get('profile', {}).get('foreground_requested') is foreground and
                value.get('APK', {}).get('sha256') == plan.get('APK_sha256'), 'control_identity_mismatch')
        samples = value.get('cgroup_samples', [])
        observed_frozen = any(s.get('mode') == '1' or s.get('frozen') == '1' for s in samples)
        if value.get('state') == 'ENVIRONMENT_FROZEN_INCOMPLETE':
            require(observed_frozen and value.get('normal_close_proven') is False, 'frozen_control_scope')
        elif value.get('state') == 'PASS_OBSERVED_UNFROZEN_SAMPLES':
            require(not observed_frozen and value.get('normal_close_proven') is True, 'unfrozen_control_scope')
        else:
            raise RuntimeError('unexpected_control_state:' + str(value.get('state')))
        result.append({'receipt': identity(path), 'state': value['state'], 'foreground': foreground,
                       'prestart_activity': value.get('profile', {}).get('prestart_activity', False),
                       'observed_frozen': observed_frozen, 'samples': len(samples),
                       'normal_close_proven': value.get('normal_close_proven')})
    return {'rounds': result, 'causation': 'SAME_APK_OBSERVATIONAL_ASSOCIATION_ONLY; BOOTSTRAP AND RESTART ALSO DIFFER',
            'scope': 'TWO_CONTROL_PAIRS_FOUR_SHORT_WINDOWS_NOT_PERFORMANCE_OR_ALL_DEVICES'}


def verify_api34_optional(x86_build):
    """Separate functional smoke and injected fail-stop, never long or causation."""
    build_path = BASE / 'retry-r8-x86_64-build.json'
    apk_items = [item for item in x86_build.get('artifacts', []) if item['path'].endswith('.apk')]
    require(len(apk_items) == 1, 'one_x86_diagnostic_APK_required')
    expected_apk, expected_build = apk_items[0]['sha256'], sha(build_path)
    results = {}
    values = {}
    for label, name in (('guard', 'api34-retry-guard-resume2/receipt.json'),
                        ('smoke', 'api34-retry-smoke-resume2/receipt.json')):
        path = BASE / name
        if not path.is_file():
            results[label] = {'state': 'UNAVAILABLE_NOT_QUALIFIED', 'receipt_path': relative(path), 'qualified': False}
            continue
        value = read(path)
        require(isinstance(value, dict), 'invalid_API34_receipt_type:' + label)
        values[label] = value
        reasons = []
        def check(condition, reason):
            if not condition: reasons.append(reason)
        device = value.get('device') if isinstance(value.get('device'), dict) else {}
        apk = value.get('APK') if isinstance(value.get('APK'), dict) else {}
        check(type(device.get('api')) is int and device['api'] == 34 and device.get('abi') == 'x86_64' and
              device.get('page_bytes') == 4096, 'DEVICE_SCOPE')
        check(apk.get('package') == PACKAGE and apk.get('apk_sha256') == expected_apk and
              apk.get('build_receipt_sha256') == expected_build, 'APK_OR_BUILD_IDENTITY')
        check(value.get('performance') == 'NOT_MEASURED', 'PERFORMANCE_SCOPE')
        if label == 'guard':
            check(value.get('state') == 'PASS_EXPECTED_FAIL_STOP_UNKNOWN' and
                  value.get('normal_domain_close_qualified') is False and value.get('unsafe_cleanup') is True and
                  value.get('boundary_outcome') == 'UNKNOWN_RUNNING_WORK' and
                  value.get('native_resource_state') == 'UNKNOWN_NOT_FORCED_FREE' and value.get('no_forced_free') is True,
                  'FAIL_STOP_OR_UNKNOWN_SCOPE')
            check(value.get('reason') == 'STALL_PHASE_TIMEOUT' and value.get('stack_status') == 'CAPTURED_JAVA_THREADS_ONLY' and
                  type(value.get('observed_thread_count')) is int and value['observed_thread_count'] > 0, 'INJECTED_JAVA_STACK_SCOPE')
        else:
            report = value.get('instrumentation_report') if isinstance(value.get('instrumentation_report'), dict) else {}
            duration, count, minimum, requested = (report.get('actual_duration_ms'), report.get('iterations_completed'),
                                                   report.get('minimum_duration_ms'), report.get('iterations_requested'))
            check(value.get('state') == 'PASS_SCOPED_API34_EMULATOR_FUNCTIONAL' and report.get('status') == 'PASS' and
                  all(type(v) is int for v in (duration, count, minimum, requested)) and
                  duration >= minimum >= 0 and count >= requested > 0, 'COMPLETE_SHORT_WORK')
            check(report.get('api') == 34 and report.get('final_backing_bytes_each_domain') == 0 and
                  report.get('fire_and_forget_state') == 'PASS_REGISTRATION_REMOVED_WITHOUT_CALLER_POLL' and
                  report.get('phase') == 'complete', 'SHORT_NORMAL_CLOSE_AND_AUTO_CLEANUP')
            check(report.get('performance_qualified') is False and report.get('BIL_integrated') is False and
                  report.get('actual_low_memory_event') is False, 'NO_PERFORMANCE_OR_OOM_PROMOTION')
            samples = value.get('cgroup_samples')
            check(isinstance(samples, list) and len(samples) >= 2 and all(isinstance(sample, dict) and
                  sample.get('state') == 'OBSERVED' and sample.get('cgroup_freeze') == 0 and
                  sample.get('cgroup_events_frozen') == 0 and sample.get('freeze_controller_modified') is False
                  for sample in samples), 'SHORT_CGROUP_SAMPLES')
            check(isinstance(value.get('postflight_gate'), dict) and
                  value['postflight_gate'].get('result') == 'PASS_OBSERVATIONAL_ONLY', 'SHORT_POSTFLIGHT')
            check(value.get('guard_run_id_differs') is True, 'SMOKE_AND_GUARD_MUST_BE_INDEPENDENT')
        results[label] = {'receipt': identity(path), 'observed_state': value.get('state'),
                          'state': ('PASS_EXPECTED_INJECTION_NOT_NORMAL_CLOSE' if label == 'guard' else 'PASS_SCOPED_SHORT_NOT_LONG')
                                   if not reasons else 'INVALID_OR_FAILED_NOT_QUALIFIED',
                          'qualified': not reasons, 'reasons': reasons}
        if label == 'guard':
            results[label]['normal_domain_close_qualified'] = False
        else:
            results[label]['android_long_qualified'] = False
    if 'guard' in values and 'smoke' in values:
        independent = (values['guard'].get('own_run_id_sha256') is not None and
                       values['guard'].get('own_run_id_sha256') != values['smoke'].get('own_run_id_sha256'))
        if not independent:
            results['smoke'].update(state='INVALID_OR_FAILED_NOT_QUALIFIED', qualified=False)
            results['smoke']['reasons'].append('SAME_OR_UNKNOWN_RUN_ID')
    return {'guard': results['guard'], 'smoke': results['smoke'],
            'limits': 'OWN_JAVA_STACK_SUMMARY_AND_SHORT_API34_FUNCTIONAL_ONLY; NOT_ANDROID_LONG_OR_API33_CAUSATION'}


def verify_api34_redaction():
    """Check local original bytes but package only explicit redacted copies."""
    path = BASE / 'api34-retry-guard/source-redaction.json'
    require(path.is_file(), 'API34_source_redaction_receipt_missing')
    value = read(path)
    require(value.get('scope') == 'RETAIN_EXACT_LOCAL_BYTES_REDACT_PUBLIC_REVIEW_COPIES', 'API34_source_redaction_scope')
    items = value.get('files')
    require(isinstance(items, list) and len(items) == 3, 'API34_source_redaction_file_count')
    expected = {'tools/verify_api34_retry.py',
                'artifacts/android-close-investigation-20261030/api34-retry-guard/executed-runner-v1.txt',
                'artifacts/android-close-investigation-20261030/api34-retry-guard/executed-runner-resume-v2.txt'}
    require({item.get('historical_name') for item in items} == expected, 'API34_source_redaction_names')
    reviews = []
    helper_hash = None
    private_root = (ROOT / 'build/android-close-private-api34').resolve()
    for item in items:
        original = private_root / (Path(item['historical_name']).name + '.original')
        require(original.resolve().is_relative_to(private_root) and original.is_file() and
                sha(original) == item.get('executed_sha256') and item.get('exact_original') == 'LOCAL_IGNORED_ONLY',
                'API34_original_bytes_not_locally_verified')
        if item['historical_name'] == 'tools/verify_api34_retry.py':
            helper_hash = item['executed_sha256']
            require(item.get('current_tool') == 'SERIAL_INPUT_NOW_EXPLICIT', 'API34_helper_redaction_scope')
        else:
            review = checked({'path': item['public_review_copy'], 'sha256': item['review_copy_sha256']})
            require(review.is_relative_to(BASE) and item.get('review_copy_executed') is False,
                    'API34_redacted_review_copy_cannot_be_claimed_executed')
            reviews.append(identity(review))
    return {'receipt': identity(path), 'executed_helper_sha256': helper_hash,
            'originals': 'EXACT_HASH_VERIFIED_IN_IGNORED_BUILD_NOT_PACKAGED',
            'public_review_copies': reviews, 'review_copies_executed': False}


def verify_distribution_api34_optional(build_file, apk, redaction):
    path = BASE / 'api34-distribution-smoke/receipt.json'
    postflight_path = BASE / 'api34-distribution-postflight.json'
    if not path.is_file() or not postflight_path.is_file():
        return {'state': 'UNAVAILABLE_NOT_QUALIFIED', 'qualified': False, 'android_long_qualified': False}
    value, postflight = read(path), read(postflight_path)
    reasons = []
    def check(condition, reason):
        if not condition: reasons.append(reason)
    device = value.get('device') if isinstance(value.get('device'), dict) else {}
    apk_info = value.get('APK') if isinstance(value.get('APK'), dict) else {}
    report = value.get('instrumentation_report') if isinstance(value.get('instrumentation_report'), dict) else {}
    samples = value.get('cgroup_samples')
    actual_duration, actual_count = report.get('actual_duration_ms'), report.get('iterations_completed')
    minimum, requested = report.get('minimum_duration_ms'), report.get('iterations_requested')
    check(value.get('state') == 'PASS_SCOPED_API34_FINAL_SANITIZED_R8_FUNCTIONAL' and
          device.get('api') == 34 and device.get('abi') == 'x86_64' and device.get('page_bytes') == 4096, 'DEVICE_SCOPE')
    check(apk_info.get('package') == PACKAGE and apk_info.get('apk_sha256') == sha(apk) and
          apk_info.get('build_receipt_sha256') == sha(build_file), 'APK_OR_BUILD_IDENTITY')
    check(value.get('helper_sha256') == redaction['executed_helper_sha256'], 'EXECUTED_HELPER_IDENTITY')
    check(report.get('status') == 'PASS' and report.get('phase') == 'complete' and
          all(type(v) is int for v in (actual_duration, actual_count, minimum, requested)) and
          actual_duration >= minimum > 0 and actual_count >= requested > 0, 'INCOMPLETE_SHORT_WORK')
    check(report.get('logical_unavailable_reason_checks') == 'PASS' and
          report.get('fire_and_forget_state') == 'PASS_REGISTRATION_REMOVED_WITHOUT_CALLER_POLL' and
          report.get('final_backing_bytes_each_domain') == 0, 'REASON_AUTOCLEANUP_OR_NORMAL_CLOSE')
    check(report.get('performance_qualified') is False and report.get('BIL_integrated') is False and
          report.get('actual_low_memory_event') is False and value.get('performance') == 'NOT_MEASURED', 'PERFORMANCE_OR_OOM_SCOPE')
    check(isinstance(samples, list) and len(samples) >= 2 and all(isinstance(sample, dict) and
          sample.get('state') == 'OBSERVED' and sample.get('cgroup_freeze') == 0 and
          sample.get('cgroup_events_frozen') == 0 and sample.get('freeze_controller_modified') is False
          for sample in samples), 'SHORT_CGROUP_SAMPLES')
    check(postflight.get('state') == 'PASS_OWN_AVD_STOP_CONFIRMED' and
          postflight.get('source_receipt_sha256') == sha(path) and postflight.get('installed_APK_sha256') == sha(apk) and
          postflight.get('boot_identity') == 'UNCHANGED' and postflight.get('other_device_or_process_stopped') is False and
          postflight.get('app_data_cleared_or_uninstalled') is False and
          isinstance(postflight.get('final_idle_gate'), dict) and
          postflight['final_idle_gate'].get('result') == 'PASS_OBSERVATIONAL_ONLY', 'INSTALLED_BYTE_IDENTITY_OR_POSTFLIGHT')
    return {'state': 'PASS_SCOPED_API34_FINAL_SHORT_NOT_LONG' if not reasons else 'INVALID_OR_FAILED_NOT_QUALIFIED',
            'qualified': not reasons, 'android_long_qualified': False, 'reasons': reasons,
            'receipt': identity(path), 'postflight': identity(postflight_path),
            'iterations_completed': actual_count, 'actual_duration_ms': actual_duration,
            'APK_sha256': sha(apk), 'scope': 'X86_64_EMULATOR_FUNCTIONAL_ONLY_NOT_ARM64_OR_PERFORMANCE'}


def verify_distribution_postflight(long_receipt_name, build_file, apk, aar, expected_cert):
    """Installed bytes and postflight are distinct from the sampled long loop."""
    path = BASE / 'distribution-postflight.json'
    long_path = BASE / long_receipt_name
    if not path.is_file() or not long_path.is_file():
        return {'state': 'UNAVAILABLE_NOT_QUALIFIED', 'qualified': False, 'device_UI_recovery': 'UNKNOWN'}
    value, long = read(path), read(long_path)
    require(isinstance(value, dict) and isinstance(long, dict), 'postflight_receipt_type')
    with zipfile.ZipFile(aar) as archive:
        expected_jni = hashlib.sha256(archive.read('jni/arm64-v8a/libmgmem_pressure_jni.so')).hexdigest()
        expected_core = hashlib.sha256(archive.read('jni/arm64-v8a/libmgmem.so')).hexdigest()
    reasons = []
    def check(condition, reason):
        if not condition: reasons.append(reason)
    check(value.get('state') == 'PASS_INSTALLED_BYTE_IDENTITY_AND_POSTFLIGHT', 'POSTFLIGHT_STATE')
    check(value.get('long_receipt_sha256') == sha(long_path) and value.get('build_receipt_sha256') == sha(build_file), 'POSTFLIGHT_RECEIPT_BINDING')
    installed_apk_and_cert = value.get('installed_APK_sha256') == sha(apk) and value.get('certificate_sha256') == expected_cert
    installed_members = value.get('jni_member_sha256') == expected_jni and value.get('core_member_sha256') == expected_core
    installed_verified = (value.get('installed_byte_identity_verified') is True and installed_apk_and_cert and installed_members and
                          value.get('long_receipt_sha256') == sha(long_path) and value.get('build_receipt_sha256') == sha(build_file) and
                          value.get('device_serial_sha256') is not None and value.get('device_serial_sha256') == long.get('device_serial_sha256') and
                          value.get('boot_id_sha256') is not None and value.get('boot_id_sha256') == long.get('boot_id_sha256'))
    check(installed_apk_and_cert, 'INSTALLED_APK_OR_CERTIFICATE')
    check(installed_members, 'INSTALLED_NATIVE_MEMBERS')
    check(value.get('installed_byte_identity_verified') is True and value.get('protocol_and_lifecycle_pass') is True,
          'POSTFLIGHT_VERIFICATION_STEPS_NOT_COMPLETED')
    check(value.get('device_serial_sha256') == long.get('device_serial_sha256') and
          value.get('boot_id_sha256') == long.get('boot_id_sha256') and
          value.get('device_serial_sha256') is not None and value.get('boot_id_sha256') is not None, 'POSTFLIGHT_DEVICE_OR_BOOT')
    for name in ('before_gate', 'after_gate'):
        gate = value.get(name)
        check(isinstance(gate, dict) and gate.get('result') == 'PASS_OBSERVATIONAL_ONLY' and
              gate.get('matching_tests') == 0 and gate.get('active_instrumentation') == 0, 'POSTFLIGHT_' + name.upper())
    activity = value.get('own_activity_resumed', 'UNKNOWN')
    check(type(activity) is bool or activity == 'UNKNOWN', 'POSTFLIGHT_ACTIVITY_TYPE')
    check(activity is not True, 'OWN_ACTIVITY_STILL_RESUMED')
    return {'state': 'PASS_INSTALLED_BYTE_IDENTITY_AND_POSTFLIGHT' if not reasons else 'INVALID_OR_BLOCKED_NOT_QUALIFIED',
            'qualified': not reasons, 'reasons': reasons, 'receipt': identity(path),
            'installed_byte_identity_verified': bool(installed_verified),
            'device_UI_recovery': 'UNKNOWN' if activity == 'UNKNOWN' else ('ACTIVITY_NOT_RESUMED' if activity is False else 'OWN_ACTIVITY_STILL_RESUMED'),
            'scope': 'OBSERVATIONAL_PROCESS_GATES_NO_SHARED_LOCK; ACTIVITY_UNKNOWN_DOES_NOT_PROVE_UI_RESTORED'}


def verify_source_lock():
    path = BASE / 'SOURCE_LOCK.json'
    require(path.is_file(), 'independent_stage_source_lock_missing')
    value = read(path)
    entries = value.get('files')
    require(isinstance(entries, list) and entries, 'invalid_stage_source_lock')
    canonical = json.dumps(entries, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')
    require(value.get('file_count') == len(entries) and value.get('source_set_sha256') == hashlib.sha256(canonical).hexdigest(), 'stage_source_lock_summary_mismatch')
    for item in entries:
        require(item['root'] in ('MeguvizMemory', 'DexMeguviz'), 'unknown_source_root')
        prefix = ROOT if item['root'] == 'MeguvizMemory' else ROOT / 'build/locked-inputs/DexMeguviz'
        file = (prefix / item['path']).resolve()
        require(file.is_relative_to(prefix.resolve()) and file.is_file() and sha(file) == item['sha256'], 'stage_source_lock_drift:' + item['path'])
    return {'kind': 'SOURCE_HASH_BUILD_NOT_DELIVERY_COMMIT_BUILD', 'source_lock': identity(path),
            'source_set_sha256': value['source_set_sha256']}


def safe_public(name):
    path = Path(name)
    lower = name.lower().replace('\\', '/')
    parts = {part.lower() for part in path.parts}
    return (not parts.intersection({'commands', '.git', '.codex', '.agents', '__pycache__', 'raw', 'private'}) and
            not path.name.lower().startswith(('admission', 'authorization', 'command-logs', '.env')) and
            not any(word in path.name.lower() for word in ('keystore', 'private-key', 'access-token')) and
            path.suffix.lower() not in {'.key', '.pem', '.jks', '.keystore', '.p12', '.pfx', '.apk', '.dex'} and
            (name in PUBLIC_CAPTURE_GOLDENS or not lower.endswith(('.stdout', '.stderr', '.stdout.txt', '.stderr.txt'))))


def verify_public_capture_goldens(entries):
    root = ROOT / 'tests/fixtures/continuation-validation'
    provenance = read(root / 'provenance.json')
    require(provenance.get('schema_version') == 1 and provenance.get('synthetic_success_records') is False and
            provenance.get('new_device_execution') is False, 'invalid_capture_fixture_provenance')
    items = provenance.get('fixtures')
    require(isinstance(items, list) and len(items) == 4, 'four_captured_fixture_records_required')
    expected = {'android-api34.stdout.txt', 'dex-arm64.stdout.txt',
                'image-system.derived.json', 'image-arena.derived.json'}
    require({item.get('file') for item in items} == expected, 'captured_fixture_set_mismatch')
    for item in items:
        filename = item['file']
        require(Path(filename).name == filename, 'captured_fixture_not_leaf')
        path = root / filename
        require(path.is_file() and path.stat().st_size == item['bytes'] and sha(path) == item['sha256'], 'captured_fixture_identity:' + filename)
        if item.get('representation') == 'EXACT_CAPTURE_BYTES':
            require(item.get('original_output_sha256') == item['sha256'], 'captured_stdout_not_exact:' + filename)
        else:
            require(item.get('representation') == 'DERIVED_FROM_ACTUAL_POSTREVIEW_RECEIPT', 'unknown_captured_fixture_representation')
        require('source/tests/fixtures/continuation-validation/' + filename in entries, 'captured_fixture_missing_from_bundle:' + filename)
    require('source/tests/fixtures/continuation-validation/provenance.json' in entries, 'capture_fixture_provenance_missing_from_bundle')
    return {'scope': 'FOUR_HASH_LOCKED_PUBLIC_CAPTURES_NOT_NEW_DEVICE_EVIDENCE',
            'manifest': identity(root / 'provenance.json'), 'fixtures': [identity(root / item['file']) for item in items]}


def scan_bytes(name, data, forbidden):
    for token in forbidden:
        variants = [token.lower()]
        try:
            text = token.decode('utf-8').lower()
            variants += [text.encode('utf-16le'), text.encode('utf-16be')]
        except UnicodeError:
            pass
        if any(variant in data.lower() for variant in variants):
            raise RuntimeError('private_identifier_detected_in:' + name)
    if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', data) or re.search(rb'\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{15,}\b', data):
        raise RuntimeError('credential_pattern_detected_in:' + name)


def inspect_archive(path, forbidden, *, allow_apk=False):
    def inspect(archive, label, depth, diagnostic_apk):
        require(depth <= 3, 'archive_nesting_limit:' + label)
        names = archive.namelist()
        require(len(names) == len(set(names)) and archive.testzip() is None, 'archive_duplicate_or_corrupt:' + label)
        for item in archive.infolist():
            normalized = item.filename.replace('\\', '/')
            require(not normalized.startswith('/') and '..' not in normalized.split('/') and not re.match(r'^[A-Za-z]:', normalized), 'unsafe_archive_member')
            require(item.file_size <= 128 * 1024 * 1024, 'archive_member_too_large:' + normalized)
            lower = normalized.lower()
            require(not lower.endswith(('.apk', '.key', '.pem', '.keystore', '.jks', '.p12', '.pfx')), 'private_archive_member:' + normalized)
            if lower.endswith('.dex'):
                require(diagnostic_apk and depth == 0 and normalized == 'classes.dex', 'private_dex_archive_member:' + normalized)
            if item.is_dir():
                continue
            data = archive.read(item)
            scan_bytes(label + ':' + normalized, data, forbidden)
            if lower.endswith(('.jar', '.zip', '.aar')):
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as nested:
                        inspect(nested, label + ':' + normalized, depth + 1, False)
                except zipfile.BadZipFile as error:
                    raise RuntimeError('invalid_nested_archive:' + normalized) from error
    with zipfile.ZipFile(path) as archive:
        inspect(archive, path.name, 0, allow_apk)


def aapt_package(aapt, apk):
    try:
        done = subprocess.run([str(aapt), 'dump', 'badging', str(apk)], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('aapt2_verification_unavailable') from error
    require(done.returncode == 0, 'aapt2_badging_failed')
    matches = re.findall(r"(?m)^package: name='([^']+)'", done.stdout.decode('utf-8', 'replace'))
    require(matches == [PACKAGE], 'unexpected_diagnostic_APK_package')
    return {'package': PACKAGE, 'badging_sha256': hashlib.sha256(done.stdout).hexdigest()}


def verify_signature(java, signer, apk, expected):
    try:
        done = subprocess.run([str(java), '-jar', str(signer), 'verify', '--print-certs', str(apk)],
                              capture_output=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('APK_signature_verification_unavailable') from error
    require(done.returncode == 0, 'APK_signature_verification_failed')
    text = done.stdout.decode('utf-8', 'replace')
    certificates = re.findall(r'certificate SHA-256 digest:\s*([0-9a-fA-F]{64})', text)
    require(len(certificates) == 1 and certificates[0].lower() == expected.lower(), 'APK_signer_identity_mismatch')
    return {'state': 'PASS_SINGLE_EXPECTED_CERTIFICATE', 'certificate_sha256': certificates[0].lower(),
            'verification_stdout_sha256': hashlib.sha256(done.stdout).hexdigest()}


def elf_load_contract(data):
    """Compare all loadable bytes while masking only ELF section-table fields."""
    require(len(data) >= 64 and data[:6] == b'\x7fELF\x02\x01', 'expected_ELF64_little_endian')
    machine = struct.unpack_from('<H', data, 18)[0]
    phoff = struct.unpack_from('<Q', data, 32)[0]
    entsize, count = struct.unpack_from('<HH', data, 54)
    require(entsize == 56 and 0 < count < 256 and phoff + entsize * count <= len(data), 'ELF_program_header_bounds')
    headers, segments = [], []
    for index in range(count):
        kind, flags, offset, va, pa, file_size, memory_size, alignment = struct.unpack_from('<IIQQQQQQ', data, phoff + index * entsize)
        if kind != 1:
            continue
        require(offset + file_size <= len(data), 'ELF_load_segment_bounds')
        content = bytearray(data[offset:offset + file_size])
        for begin, end in ((40, 48), (58, 64)):
            for position in range(max(begin, offset), min(end, offset + file_size)):
                content[position - offset] = 0
        headers.append([flags, offset, va, pa, file_size, memory_size, alignment])
        segments.append(hashlib.sha256(content).hexdigest())
    require(headers, 'ELF_has_no_load_segment')
    return {'machine': machine, 'load_headers': headers, 'load_sha256_with_section_header_fields_masked': segments}


def tool_stdout(command):
    try:
        result = subprocess.run([str(item) for item in command], capture_output=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('binary_review_tool_unavailable') from error
    require(result.returncode == 0, 'binary_review_tool_failed:' + Path(command[0]).name)
    return result.stdout.decode('utf-8', 'replace').replace('\r\n', '\n')


def symbol_fingerprint(tool, path, dynamic):
    args = [tool, '--dyn-syms', '--wide', path] if dynamic else [tool, '--defined-only', '--extern-only', '--format=posix', path]
    output = tool_stdout(args).splitlines()
    lines = sorted(line.strip() for line in output if line.strip() and not line.startswith(('File:', 'Archive:')) and not line.rstrip().endswith(':'))
    return hashlib.sha256('\n'.join(lines).encode('utf-8')).hexdigest()


def coff_nondebug_payloads(readobj, path):
    text = tool_stdout([readobj, '--sections', '--section-data', path])
    blocks = re.findall(r'(?ms)^  Section \{\n(.*?)^  \}', text)
    rows = []
    for block in blocks:
        match = re.search(r'(?m)^    Name: (\S+)', block)
        require(match is not None, 'COFF_section_name_missing')
        name = match.group(1)
        if name.startswith('.debug'):
            continue
        size_match = re.search(r'(?m)^    RawDataSize: (\d+)', block)
        require(size_match is not None, 'COFF_section_size_missing')
        size = int(size_match.group(1))
        section = re.search(r'(?ms)^    SectionData \(\n(.*?)^    \)', block)
        if section:
            pieces = [''.join(re.findall(r'\b[0-9A-F]{2,8}\b', line.split('|')[0].split(':', 1)[1]))
                      for line in section.group(1).splitlines() if ':' in line]
            payload = bytes.fromhex(''.join(pieces))
            require(len(payload) == size, 'COFF_section_size_mismatch')
        else:
            payload = b''
        rows.append([name, size, hashlib.sha256(payload).hexdigest()])
    require(rows, 'COFF_non_debug_sections_missing')
    return rows


def archive_alloc_sections(readobj, path, kind):
    text = tool_stdout([readobj, '--sections', '--section-data', path])
    blocks = re.findall(r'(?ms)^  Section \{\n(.*?)^  \}', text)
    rows = []
    for block in blocks:
        if kind == 'ELF' and 'SHF_ALLOC' not in block:
            continue
        match = re.search(r'(?m)^    Name: (\S+)', block)
        if not match:
            continue
        name = match.group(1)
        if kind == 'COFF' and name.startswith('.debug'):
            continue
        size_match = re.search(r'(?m)^    ' + ('Size' if kind == 'ELF' else 'RawDataSize') + r': (\d+)', block)
        require(size_match is not None, 'archive_section_size_missing')
        size = int(size_match.group(1))
        section = re.search(r'(?ms)^    SectionData \(\n(.*?)^    \)', block)
        pieces = ([''.join(re.findall(r'\b[0-9A-F]{2,8}\b', line.split('|')[0].split(':', 1)[1]))
                   for line in section.group(1).splitlines() if ':' in line] if section else [])
        payload = bytes.fromhex(''.join(pieces))
        require(not payload or len(payload) == size, 'archive_section_payload_size')
        payload_sha = hashlib.sha256(payload).hexdigest()
        if kind == 'COFF':
            rows.append([name, size, payload_sha])
        else:
            section_type = re.search(r'(?m)^    Type: (\S+)', block)
            flags = re.search(r'(?m)^    Flags \[ \((0x[0-9A-Fa-f]+)\)', block)
            alignment = re.search(r'(?m)^    AddressAlignment: (\d+)', block)
            require(section_type and flags and alignment, 'ELF_archive_section_metadata')
            rows.append([name, section_type.group(1), flags.group(1), size, int(alignment.group(1)), payload_sha])
    require(rows, 'archive_sections_missing')
    return rows


def archive_relocations(readobj, path, kind):
    text = tool_stdout([readobj, '--relocations', path])
    groups = re.findall(r'(?ms)^  Section \(\d+\) (\S+) \{\n(.*?)^  \}', text)
    rows = []
    for name, content in groups:
        if (name.startswith('.debug') if kind == 'COFF' else '.debug' in name):
            continue
        lines = [re.sub(r'\s+\(\d+\)\s*$', '', line.strip()) if kind == 'COFF' else line.strip()
                 for line in content.splitlines() if line.strip()]
        rows.append([name, lines])
    require(rows, 'archive_relocations_missing')
    return rows


def archive_semantic_digest(value):
    return hashlib.sha256(json.dumps(value, separators=(',', ':')).encode('utf-8')).hexdigest()


def verify_static_archive_semantics(proof, tools):
    base = BASE / 'shareable-sdk-v2'
    independent_path = base / 'archive-independent-reverification.json'
    coff_path = base / 'coff-relocations.json'
    android_path = base / 'android-static-sections-relocations.json'
    require(all(path.is_file() for path in (independent_path, coff_path, android_path)), 'archive_semantics_receipts_missing')
    independent, coff, android = read(independent_path), read(coff_path), read(android_path)
    require(independent.get('state') == 'PASS_INDEPENDENT_ARCHIVE_RECOMPUTATION' and
            coff.get('state') == android.get('state') == 'PASS' and
            independent.get('readobj_sha256') == coff.get('LLVM_readobj_sha256') == android.get('LLVM_readobj_sha256') == sha(tools['readobj']),
            'archive_semantics_receipt_or_tool_mismatch')
    results = []
    for sdk_label, evidence_label, kind, suffix in (
            ('windows-x64', 'windows-x64', 'COFF', '/lib/mgmem_pressure.lib'),
            ('android-arm64-v8a', 'arm64-v8a', 'ELF', '/lib/libmgmem_pressure.a'),
            ('android-x86_64', 'x86_64', 'ELF', '/lib/libmgmem_pressure.a')):
        matches = [item for item in proof['profiles'][sdk_label]['binary_transformations']
                   if item.get('source', {}).get('path', '').endswith(suffix)]
        require(len(matches) == 1, 'one_pressure_archive_transform_required:' + sdk_label)
        source, candidate = checked(matches[0]['source']), checked(matches[0]['candidate'])
        source_sections, candidate_sections = archive_alloc_sections(tools['readobj'], source, kind), archive_alloc_sections(tools['readobj'], candidate, kind)
        source_relocations, candidate_relocations = archive_relocations(tools['readobj'], source, kind), archive_relocations(tools['readobj'], candidate, kind)
        require(source_sections == candidate_sections and source_relocations == candidate_relocations, 'archive_section_or_relocation_mismatch:' + sdk_label)
        expected = independent['profiles'][evidence_label]
        require(expected.get('original_sha256') == sha(source) and expected.get('candidate_sha256') == sha(candidate) and
                expected.get('section_count') == len(source_sections) and expected.get('section_semantics_sha256') == archive_semantic_digest(source_sections) and
                expected.get('relocation_groups') == len(source_relocations) and
                expected.get('relocation_count') == sum(len(group[1]) for group in source_relocations) and
                expected.get('relocation_semantics_sha256') == archive_semantic_digest(source_relocations), 'archive_independent_proof_mismatch:' + sdk_label)
        if kind == 'COFF':
            require(coff.get('original_sha256') == sha(source) and coff.get('candidate_sha256') == sha(candidate) and
                    coff.get('normalized_relocations_sha256') == expected['relocation_semantics_sha256'], 'COFF_relocation_receipt_mismatch')
        else:
            other = android['profiles'][evidence_label]
            require(other.get('original_sha256') == sha(source) and other.get('candidate_sha256') == sha(candidate) and
                    other.get('allocatable_sections_sha256') == expected['section_semantics_sha256'] and
                    other.get('non_debug_relocations_sha256') == expected['relocation_semantics_sha256'], 'ELF_archive_relocation_receipt_mismatch:' + sdk_label)
        results.append({'profile': sdk_label, 'section_count': len(source_sections),
                        'relocations': sum(len(group[1]) for group in source_relocations),
                        'section_sha256': expected['section_semantics_sha256'],
                        'relocations_sha256': expected['relocation_semantics_sha256']})
    return {'state': 'PASS_ARCHIVE_SECTIONS_AND_RELOCATIONS_RECOMPUTED', 'profiles': results,
            'receipts': [identity(independent_path), identity(coff_path), identity(android_path)],
            'scope': 'LINK_CONTENT_ONLY_NOT_DEVICE_OR_PERFORMANCE_QUALIFICATION'}


def verify_transform(item, tools):
    """Independently recompute the bounded binary invariants in the strip proof."""
    source, candidate = checked(item['source']), checked(item['candidate'])
    original, transformed = source.read_bytes(), candidate.read_bytes()
    require(type(item.get('same_bytes')) is bool and item['same_bytes'] == (original == transformed), 'transform_same_bytes_mismatch')
    kind = item.get('format')
    if kind == 'ELF':
        left, right = elf_load_contract(original), elf_load_contract(transformed)
        require(left == right == item.get('ELF_PT_LOAD'), 'ELF_load_contract_mismatch')
        a = symbol_fingerprint(tools['readelf'], source, True)
        b = symbol_fingerprint(tools['readelf'], candidate, True)
        require(a == b == item.get('dynamic_symbols_sha256'), 'ELF_dynamic_symbols_mismatch')
    elif kind == 'ARCHIVE':
        a = symbol_fingerprint(tools['nm'], source, False)
        b = symbol_fingerprint(tools['nm'], candidate, False)
        require(a == b == item.get('defined_external_symbols_sha256'), 'archive_exported_symbols_mismatch')
        if 'coff_non_debug_payloads_sha256' in item:
            left, right = coff_nondebug_payloads(tools['readobj'], source), coff_nondebug_payloads(tools['readobj'], candidate)
            fingerprint = hashlib.sha256(json.dumps(left, separators=(',', ':')).encode('utf-8')).hexdigest()
            require(left == right and len(left) == item.get('coff_non_debug_section_count') and
                    fingerprint == item['coff_non_debug_payloads_sha256'], 'COFF_non_debug_payload_mismatch')
    elif kind == 'PE':
        require(original == transformed and item['same_bytes'] is True, 'PE_image_changed')
    else:
        raise RuntimeError('unsupported_transform_format:' + str(kind))
    return source, candidate


def verify_sdk_proof(path, selected_sdks, tools):
    proof = read(path)
    require(isinstance(proof.get('state'), str) and proof['state'].startswith('PASS') and proof.get('sources_unchanged') is True,
            'sanitized_SDK_proof_not_pass')
    profiles = proof.get('profiles')
    require(isinstance(profiles, dict), 'sanitized_SDK_profiles_missing')
    records = []
    for label in ('windows-x64', 'android-arm64-v8a', 'android-x86_64'):
        profile = profiles.get(label)
        require(isinstance(profile, dict) and profile.get('binary_transformations'), 'sanitized_SDK_profile_missing:' + label)
        proof_zip = checked(profile['sdk_zip'])
        selected = selected_sdks[label]
        require(selected.is_file() and sha(selected) == sha(proof_zip), 'selected_SDK_does_not_match_proof:' + label)
        prefix = None
        for transformation in profile['binary_transformations']:
            _, candidate = verify_transform(transformation, tools)
            marker = '/sdk/' + label + '/'
            name = relative(candidate)
            require(marker in name, 'SDK_candidate_prefix_missing')
            current = (ROOT / name.split(marker, 1)[0] / 'sdk' / label).resolve()
            require(current.is_relative_to(ROOT) and candidate.is_relative_to(current), 'SDK_candidate_prefix_unsafe')
            if prefix is None:
                prefix = current
            require(prefix == current, 'SDK_candidate_prefix_changed')
        all_files = {path.relative_to(prefix).as_posix(): path for path in prefix.rglob('*') if path.is_file() and not path.is_symlink()}
        with zipfile.ZipFile(selected) as archive:
            require(set(archive.namelist()) == set(all_files), 'SDK_ZIP_file_closure:' + label)
            for name, file in all_files.items():
                require(archive.read(name) == file.read_bytes(), 'SDK_ZIP_file_drift:' + label + ':' + name)
        records.append({'profile': label, 'sdk': identity(selected), 'transformations': len(profile['binary_transformations'])})
    if proof.get('windows_consumer'):
        consumer = proof['windows_consumer']
        receipt = checked({'path': consumer['receipt'], 'sha256': consumer['sha256']})
        observed = read(receipt)
        require(observed.get('all_passed') is True and observed.get('passed') == 2 and
                consumer.get('installed_only') is True, 'sanitized_windows_consumer_not_pass')
    static_archive_proof = verify_static_archive_semantics(proof, tools)
    return {'receipt': identity(path), 'state': proof['state'], 'SDKs': records,
            'static_archive_semantics': static_archive_proof,
            'scope': 'LOADABLE_BYTES_SYMBOLS_NONDEBUG_SECTIONS_CHECKED_RUNTIME_QUALIFICATION_SEPARATE'}


def verify_jni_proofs(paths, aars, tools, expected_source_sha):
    require(len(paths) == 2, 'two_ABI_JNI_strip_proofs_required')
    records = {}
    for path in paths:
        proof = read(path)
        abi = proof.get('ABI')
        require(abi in ('arm64-v8a', 'x86_64') and abi not in records, 'duplicate_or_unknown_JNI_proof_ABI')
        require(isinstance(proof.get('state'), str) and proof['state'].startswith('PASS') and
                proof.get('production_input_unchanged') is True and proof.get('JNI_source_sha256') == expected_source_sha,
                'JNI_strip_proof_source_or_status_mismatch')
        transformation = proof.get('transformation')
        require(isinstance(transformation, dict) and transformation.get('format') == 'ELF', 'JNI_ELF_transform_missing')
        source, candidate = verify_transform(transformation, tools)
        expected_machine = 183 if abi == 'arm64-v8a' else 62
        require(transformation['ELF_PT_LOAD'].get('machine') == expected_machine, 'JNI_ELF_wrong_ABI')
        aar = next(path for selected_abi, path in aars if selected_abi == abi)
        with zipfile.ZipFile(aar) as archive:
            require(hashlib.sha256(archive.read('jni/' + abi + '/libmgmem_pressure_jni.so')).hexdigest() == sha(candidate),
                    'distribution_AAR_JNI_differs_from_strip_proof')
        records[abi] = {'receipt': identity(path), 'unstripped': identity(source), 'distributed': identity(candidate),
                        'source_sha256': expected_source_sha, 'prior_long_result_transferred': False}
    require(set(records) == {'arm64-v8a', 'x86_64'}, 'both_JNI_ABIs_required')
    return {'state': 'PASS_ELF_LOAD_AND_DYNAMIC_SYMBOLS_RECOMPUTED', 'ABIs': records,
            'limits': 'NATIVE_LOADER_DEVICE_AND_LONG_EVIDENCE_ARE_SEPARATE'}


def verify_member_proof(path, aars, apks, builds):
    proof = read(path)
    require(proof.get('state') == 'PASS_AAR_APK_MEMBER_IDENTITY_AND_RECURSIVE_PATH_SCAN', 'distribution_member_proof_not_pass')
    require(proof.get('runtime_qualification') == 'NOT_INFERRED_FROM_THIS_READ_ONLY_CHECK' and
            proof.get('prior_15_minute_byte_identity') == 'NOT_CLAIMED', 'distribution_member_proof_scope')
    profiles = proof.get('profiles')
    require(isinstance(profiles, dict) and set(profiles) == {'arm64-v8a', 'x86_64'}, 'distribution_member_ABI_set')
    rows = []
    for index, abi in enumerate(('arm64-v8a', 'x86_64')):
        profile = profiles[abi]
        proof_aar, proof_apk = checked(profile['AAR']), checked(profile['APK'])
        aar = next(path for selected_abi, path in aars if selected_abi == abi)
        apk = next(path for selected_abi, path, _ in apks if selected_abi == abi)
        require(sha(proof_aar) == sha(aar) and sha(proof_apk) == sha(apk) and
                profile.get('source_build_receipt_sha256') == sha(builds[index]), 'distribution_member_artifact_or_build_mismatch')
        with zipfile.ZipFile(aar) as archive:
            for member, expected in (('classes.jar', proof.get('shared_classes_jar_sha256')),
                                     ('proguard.txt', proof.get('shared_consumer_rules_sha256')),
                                     ('META-INF/LICENSES/llvm-NOTICE.txt', proof.get('LLVM_notice_sha256')),
                                     ('jni/' + abi + '/libmgmem.so', profile.get('core_sha256')),
                                     ('jni/' + abi + '/libmgmem_pressure_jni.so', profile.get('JNI_sha256'))):
                require(hashlib.sha256(archive.read(member)).hexdigest() == expected, 'distribution_member_digest_mismatch:' + abi + ':' + member)
        rows.append({'abi': abi, 'AAR': identity(aar), 'APK': identity(apk)})
    return {'receipt': identity(path), 'state': proof['state'], 'ABIs': rows,
            'scope': 'PACKAGE_MEMBERS_ONLY_NOT_DEVICE_RUNTIME_OR_TRANSFERRED_LONG_RESULT'}


def stage_copy(source, target):
    if target.exists():
        require(target.is_file() and sha(source) == sha(target), 'refuse_artifact_overwrite:' + target.name)
    else:
        with target.open('xb') as out, source.open('rb') as inp:
            shutil.copyfileobj(inp, out)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--aapt2', type=Path, required=True, help='Existing Android SDK aapt2 binary')
    parser.add_argument('--private-identifiers-file', type=Path, required=True,
                        help='Ignored build/ file listing device serials or other literal values that may never be published')
    parser.add_argument('--java-home', type=Path, help='Existing JDK; otherwise use java on PATH')
    parser.add_argument('--apksigner-jar', type=Path, help='Defaults to lib/apksigner.jar beside aapt2')
    parser.add_argument('--ndk-prebuilt', type=Path, help='Existing NDK prebuilt root for independent strip-proof checks')
    parser.add_argument('--distribution-arm-build-receipt', type=Path, default=BASE / 'retry-r8-arm64-build.json')
    parser.add_argument('--distribution-x86-build-receipt', type=Path, default=BASE / 'retry-r8-x86_64-build.json')
    parser.add_argument('--distribution-native-build-receipt', type=Path, default=BASE / 'retry-arm64-aar-build.json')
    parser.add_argument('--distribution-native-notice-receipt', type=Path, default=BASE / 'retry-arm64-notice.json')
    parser.add_argument('--distribution-windows-sdk', type=Path, default=ROOT / 'dist/MeguvizMemory-0.2.0-Windows-x64.zip')
    parser.add_argument('--distribution-arm64-sdk', type=Path, default=ROOT / 'dist/MeguvizMemory-0.2.0-Android-arm64-v8a.zip')
    parser.add_argument('--distribution-x86-sdk', type=Path, default=ROOT / 'dist/MeguvizMemory-0.2.0-Android-x86_64-Closeout-20261030.zip')
    parser.add_argument('--distribution-proof', type=Path, help='Sanitized SDK transformation receipt; required for changed SDKs')
    parser.add_argument('--distribution-jni-proof', type=Path, action='append', default=[],
                        help='Repeat for each ABI sanitized JNI transformation receipt; required for changed distribution binaries')
    parser.add_argument('--distribution-member-proof', type=Path, help='Final AAR/APK member closure and path-scan receipt')
    parser.add_argument('--distribution-component-security', type=Path,
                        help='Compiled Activity/permission receipt; defaults to final receipt for changed APKs')
    parser.add_argument('--distribution-host-summary', type=Path, help='Independent host/ASan proof for the distributed JNI source')
    parser.add_argument('--distribution-smoke-receipt', default='distribution-smoke/receipt.json', help='Relative to this stage evidence directory')
    parser.add_argument('--distribution-smoke-plan', default='distribution-smoke-plan.json', help='Relative to this stage evidence directory')
    parser.add_argument('--distribution-long-receipt', default='distribution-long/receipt.json', help='Relative to this stage evidence directory')
    parser.add_argument('--distribution-long-plan', default='distribution-long-plan.json', help='Relative to this stage evidence directory')
    parser.add_argument('--distribution-revision', default='retryfix2', help='Bounded non-secret installation revision label')
    parser.add_argument('--verify-only', action='store_true', help='Read-only proof/privacy preflight; do not create AAR copies, manifest, bundle or sidecar')
    parser.add_argument('--defer-distribution-long', action='store_true',
                        help='Preview only: do not open any file in the running long-round directory')
    parser.add_argument('--bundle-name', default='MeguvizMemory-0.2.0-Android-Close-Resolution-20261030.zip')
    args = parser.parse_args()
    require(args.aapt2.is_file(), 'existing_aapt2_required')
    require(not args.defer_distribution_long or args.verify_only, 'deferring_active_round_is_preview_only')
    private_file = args.private_identifiers_file.resolve()
    require(private_file.is_relative_to((ROOT / 'build').resolve()) and private_file.is_file(), 'private_identifiers_must_be_ignored_build_file')
    forbidden = [line.strip().encode('utf-8') for line in private_file.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    require(0 < len(forbidden) <= 128 and all(4 <= len(value) <= 1024 for value in forbidden), 'valid_private_identifier_list_required')
    for location in (str(Path.home()), str(ROOT)):
        if len(location) >= 4:
            forbidden.extend(value.encode('utf-8') for value in {location, location.replace('\\', '/'), location.replace('/', '\\')})
    require(re.fullmatch(r'MeguvizMemory-0\.2\.0-Android-Close-Resolution-20261030(?:-[A-Za-z0-9_-]+)?\.zip', args.bundle_name) is not None, 'unsafe_bundle_name')
    require(re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', args.distribution_revision) is not None, 'unsafe_distribution_revision')
    signer = args.apksigner_jar or args.aapt2.parent / 'lib/apksigner.jar'
    java = args.java_home / ('bin/java.exe' if os.name == 'nt' else 'bin/java') if args.java_home else shutil.which('java')
    require(java and Path(java).is_file() and signer.is_file(), 'existing_JDK_and_apksigner_required')
    for value in (args.distribution_smoke_receipt, args.distribution_smoke_plan,
                  args.distribution_long_receipt, args.distribution_long_plan):
        candidate = (BASE / value).resolve()
        require(candidate.is_relative_to(BASE) and value == candidate.relative_to(BASE).as_posix(), 'distribution_receipt_or_plan_path_must_be_stage_relative')

    frozen = verify_plans([args.distribution_smoke_plan, args.distribution_long_plan])
    baseline = verify_baseline()
    host = verify_host()
    controls = verify_controls(frozen)
    api34_redaction = verify_api34_redaction()
    smoke = round_result('retry-smoke/receipt.json', 'retry-smoke-plan.json', frozen)
    long = round_result('retry-long/receipt.json', 'retry-long-plan.json', frozen, required_minimum_ms=900000)
    distribution_smoke = round_result(args.distribution_smoke_receipt, args.distribution_smoke_plan, frozen)
    first_distribution_long = ({'state': 'RUNNING_NOT_READ_NOT_QUALIFIED', 'qualified': False,
                                'receipt_path': relative(BASE / 'distribution-long/receipt.json')}
                               if args.defer_distribution_long and args.distribution_long_receipt == 'distribution-long/receipt.json' else
                               round_result('distribution-long/receipt.json', 'distribution-long-plan.json', frozen, required_minimum_ms=900000))
    distribution_long = ({'state': 'RUNNING_NOT_READ_NOT_QUALIFIED', 'qualified': False,
                          'receipt_path': relative(BASE / args.distribution_long_receipt)}
                         if args.defer_distribution_long else
                         round_result(args.distribution_long_receipt, args.distribution_long_plan, frozen, required_minimum_ms=900000))
    source_lock = verify_source_lock()

    tested_build_paths = [BASE / 'retry-r8-arm64-build.json', BASE / 'retry-r8-x86_64-build.json']
    tested_builds = [read(path) for path in tested_build_paths]
    require([value.get('abi') for value in tested_builds] == ['arm64-v8a', 'x86_64'] and
            all(value.get('status') == 'PASS' and value.get('package_name') == PACKAGE for value in tested_builds),
            'tested_build_identity')
    tested_apks = []
    source_aliases = aliases()
    for value in tested_builds:
        for item in value.get('sources', []): checked_source(item, source_aliases)
        for item in value.get('artifacts', []): checked(item)
        items = [item for item in value.get('artifacts', []) if item['path'].endswith('.apk')]
        require(len(items) == 1, 'one_tested_APK_per_ABI_required')
        tested_apks.append(checked(items[0]))
    tested_plan = read(BASE / 'retry-long-plan.json') if (BASE / 'retry-long-plan.json').is_file() else {}
    if tested_plan:
        require(tested_plan.get('APK_sha256') == sha(tested_apks[0]) and
                tested_plan.get('build_receipt_sha256') == sha(tested_build_paths[0]), 'long_plan_tested_APK_identity_mismatch')
    build_files = [under_workspace(args.distribution_arm_build_receipt), under_workspace(args.distribution_x86_build_receipt)]
    builds = [read(path) for path in build_files]
    require([value.get('abi') for value in builds] == ['arm64-v8a', 'x86_64'], 'diagnostic_ABI_identity')
    require(all(value.get('status') == 'PASS' and value.get('package_name') == PACKAGE for value in builds), 'diagnostic_build_identity')
    native_build_file = under_workspace(args.distribution_native_build_receipt)
    native_build = read(native_build_file)
    require(native_build.get('status') == 'PASS' and native_build.get('abi') == 'arm64-v8a', 'distribution_native_build_not_pass')
    native_sources = native_build.get('sources', [])
    for item in native_sources: checked_source(item, source_aliases)
    for item in native_build.get('artifacts', []): checked(item)
    native_jni = [item for item in native_sources if item.get('path') == 'adapters/android/native/pressure_jni.cpp']
    native_backend = [item for item in native_sources if item.get('path') == 'adapters/android/java/io/github/jichuo1/meguviz/memory/android/NativePressureBackend.java']
    require(len(native_jni) == len(native_backend) == 1, 'native_or_Java_backend_source_missing_from_AAR_build')
    distribution_source_sha = native_jni[0]['sha256']
    distribution_host = verify_distribution_host(under_workspace(args.distribution_host_summary) if args.distribution_host_summary else None,
                                                  distribution_source_sha, source_aliases)
    api34 = verify_api34_optional(tested_builds[1])
    build_paths, apks, aars = [], [], []
    for abi, value, build_file in zip(('arm64-v8a', 'x86_64'), builds, build_files):
        for item in value.get('sources', []):
            checked_source(item, source_aliases)
        for item in value.get('artifacts', []):
            checked(item)
        aar = checked(value['input_aar'])
        checked(value['base_aar'])
        apk_items = [item for item in value['artifacts'] if item['path'].endswith('.apk')]
        require(len(apk_items) == 1, 'exactly_one_diagnostic_APK_per_ABI')
        apk = checked(apk_items[0])
        package_proof = aapt_package(args.aapt2, apk)
        package_proof['signature'] = verify_signature(Path(java), signer, apk, value['certificate_sha256'])
        with zipfile.ZipFile(aar) as a, zipfile.ZipFile(apk) as p:
            require(a.testzip() is None and p.testzip() is None, 'AAR_or_APK_integrity')
            require('classes.jar' in a.namelist() and 'proguard.txt' in a.namelist() and
                    'META-INF/LICENSES/llvm-NOTICE.txt' in a.namelist(), 'AAR_classes_rules_or_notice_missing')
            require(a.read('META-INF/LICENSES/llvm-NOTICE.txt') == (ROOT / 'third_party_notices/llvm-NOTICE.txt').read_bytes(), 'LLVM_notice_bytes_mismatch')
            require(b'PressureR8ForegroundActivity' not in a.read('AndroidManifest.xml'), 'diagnostic_activity_in_production_AAR_manifest')
            with zipfile.ZipFile(io.BytesIO(a.read('classes.jar'))) as classes:
                require(not any(name.endswith('/PressureR8ForegroundActivity.class') for name in classes.namelist()), 'diagnostic_activity_in_production_AAR_classes')
            for library in ('libmgmem.so', 'libmgmem_pressure_jni.so'):
                require(a.read('jni/' + abi + '/' + library) == p.read('lib/' + abi + '/' + library), 'APK_AAR_native_runtime_mismatch')
            dex_items = [item for item in value['artifacts'] if item['path'].endswith('/r8-dex/classes.dex')]
            require(len(dex_items) == 1 and hashlib.sha256(p.read('classes.dex')).hexdigest() == dex_items[0]['sha256'], 'APK_DIAGNOSTIC_DEX_IDENTITY')
        aars.append((abi, aar)); apks.append((abi, apk, package_proof))
        build_paths.append(identity(build_file))
    native_notice_file = under_workspace(args.distribution_native_notice_receipt)
    native_notice = read(native_notice_file)
    original_aars = [item for item in native_build.get('artifacts', []) if item['path'].endswith('.aar')]
    require(len(original_aars) == 1, 'one_unaugmented_native_AAR_required')
    original_aar = checked(original_aars[0])
    notice_aar_item = native_notice.get('sanitized_AAR', native_notice.get('notice_augmented_aar'))
    require(isinstance(notice_aar_item, dict), 'native_notice_augmented_AAR_missing')
    notice_aar = checked(notice_aar_item)
    base_hash = native_notice.get('base_AAR_sha256', native_notice.get('source_aar_sha256'))
    require(base_hash == sha(original_aar) and sha(notice_aar) == sha(aars[0][1]) and
            native_notice.get('source_jni_sha256') == distribution_source_sha, 'native_AAR_notice_build_identity')
    class_artifacts = [item for item in native_build.get('artifacts', []) if item['path'].endswith('/classes.jar')]
    native_artifacts = {Path(item['path']).name: checked(item) for item in native_build.get('artifacts', []) if item['path'].endswith('.so')}
    require(len(class_artifacts) == 1 and {'libmgmem.so', 'libmgmem_pressure_jni.so'}.issubset(native_artifacts), 'AAR_classes_or_native_build_outputs_missing')
    with zipfile.ZipFile(original_aar) as before, zipfile.ZipFile(aars[0][1]) as after:
        require(all(after.read(name) == before.read(name) for name in before.namelist()), 'notice_assembly_changed_AAR_content')
        require(after.read('classes.jar') == checked(class_artifacts[0]).read_bytes(), 'AAR_library_classes_differ_from_build')
        for name, path in native_artifacts.items():
            if name in ('libmgmem.so', 'libmgmem_pressure_jni.so'):
                require(after.read('jni/arm64-v8a/' + name) == path.read_bytes(), 'AAR_native_differs_from_build:' + name)
    distribution_smoke = bind_distribution_round(distribution_smoke, args.distribution_smoke_receipt,
                                                   args.distribution_smoke_plan, build_files[0], apks[0][1],
                                                   reason_required=True)
    first_distribution_long = bind_distribution_round(first_distribution_long, 'distribution-long/receipt.json',
                                                        'distribution-long-plan.json', build_files[0], apks[0][1],
                                                        reason_required=True)
    distribution_long = bind_distribution_round(distribution_long, args.distribution_long_receipt,
                                                  args.distribution_long_plan, build_files[0], apks[0][1],
                                                  reason_required=True)
    distribution_postflight = ({'state': 'DEFERRED_NOT_QUALIFIED', 'qualified': False, 'device_UI_recovery': 'UNKNOWN'}
                               if args.defer_distribution_long else
                               verify_distribution_postflight(args.distribution_long_receipt, build_files[0],
                                                              apks[0][1], aars[0][1], builds[0]['certificate_sha256']))
    distribution_long['protocol_and_lifecycle_pass_before_postflight'] = bool(distribution_long.get('qualified'))
    if not distribution_postflight['qualified']:
        distribution_long['qualified'] = False
        distribution_long.setdefault('reasons', []).append('INSTALLED_BYTE_IDENTITY_OR_POSTFLIGHT_NOT_QUALIFIED')
    distribution_api34 = verify_distribution_api34_optional(build_files[1], apks[1][1], api34_redaction)

    binaries_changed = any(sha(apks[i][1]) != sha(tested_apks[i]) for i in range(2))
    component_path = (under_workspace(args.distribution_component_security) if args.distribution_component_security else
                      BASE / ('diagnostic-component-security-final.json' if binaries_changed else 'diagnostic-component-security.json'))
    require(component_path.is_file(), 'compiled_diagnostic_component_evidence_missing')
    component = read(component_path)
    require(component.get('state') == 'PASS_COMPILED_DIAGNOSTIC_APK_COMPONENT_SCOPE' and
            component.get('apk_sha256') == sha(apks[0][1]) and component.get('aapt2_sha256') == sha(args.aapt2) and
            component.get('single_explicit_activity') == 'io.github.jichuo1.meguviz.memory.android.PressureR8ForegroundActivity' and
            component.get('exported') is True and component.get('required_permission') == 'android.permission.DUMP' and
            component.get('production_pressure_AAR_contains_diagnostic_activity') is False and
            component.get('ordinary_app_denial_runtime_test') == 'NOT_RUN', 'diagnostic_component_scope_mismatch')
    with zipfile.ZipFile(apks[0][1]) as arm_apk, zipfile.ZipFile(apks[1][1]) as x86_apk:
        require(arm_apk.read('AndroidManifest.xml') == x86_apk.read('AndroidManifest.xml'),
                'diagnostic_component_manifests_differ_from_reviewed_component')

    dist = ROOT / 'dist'
    require(dist.is_dir(), 'existing_distribution_directory_required')
    bundle = dist / args.bundle_name
    sidecar = dist / (args.bundle_name + '.sha256')
    manifest = BASE / 'ARTIFACTS.json'
    if not args.verify_only:
        require(not bundle.exists() and not sidecar.exists() and not manifest.exists(), 'refuse_existing_bundle_or_manifest')
    selected_sdks = {'windows-x64': under_workspace(args.distribution_windows_sdk),
                     'android-arm64-v8a': under_workspace(args.distribution_arm64_sdk),
                     'android-x86_64': under_workspace(args.distribution_x86_sdk)}
    packages = [selected_sdks['windows-x64'], selected_sdks['android-arm64-v8a'], selected_sdks['android-x86_64'],
                ROOT / 'dist/meguviz-memory-packed-reader-0.1.0.jar']
    require(all(path.is_file() for path in packages), 'prior_install_artifacts_missing')
    require(len({path.name.lower() for path in packages}) == len(packages), 'selected_SDK_package_name_collision')
    default_sdks = [ROOT / 'dist/MeguvizMemory-0.2.0-Windows-x64.zip',
                    ROOT / 'dist/MeguvizMemory-0.2.0-Android-arm64-v8a.zip',
                    ROOT / 'dist/MeguvizMemory-0.2.0-Android-x86_64-Closeout-20261030.zip']
    sdk_changed = any(path.resolve() != old.resolve() for path, old in zip(packages[:3], default_sdks))
    require(not sdk_changed or args.distribution_proof is not None, 'sanitized_SDK_proof_required_for_changed_SDKs')
    require(not binaries_changed or bool(args.distribution_jni_proof), 'sanitized_JNI_proof_required_for_changed_APKs')
    require(not binaries_changed or args.distribution_member_proof is not None, 'sanitized_AAR_APK_member_proof_required_for_changed_APKs')
    review_tools = None
    if args.distribution_proof or args.distribution_jni_proof:
        require(args.ndk_prebuilt is not None, 'NDK_binary_review_tools_required')
        suffix = '.exe' if os.name == 'nt' else ''
        folder = args.ndk_prebuilt / 'bin'
        review_tools = {'readelf': folder / ('llvm-readelf' + suffix), 'nm': folder / ('llvm-nm' + suffix),
                        'readobj': folder / ('llvm-readobj' + suffix)}
        require(all(path.is_file() for path in review_tools.values()), 'NDK_binary_review_tool_missing')
    sdk_proof = verify_sdk_proof(under_workspace(args.distribution_proof), selected_sdks, review_tools) if args.distribution_proof else None
    jni_proof = (verify_jni_proofs([under_workspace(path) for path in args.distribution_jni_proof],
                                    aars, review_tools, distribution_source_sha)
                 if args.distribution_jni_proof else None)
    member_proof = (verify_member_proof(under_workspace(args.distribution_member_proof), aars, apks, build_files)
                    if args.distribution_member_proof else None)
    for path in packages:
        inspect_archive(path, forbidden)
    for abi, path in aars:
        inspect_archive(path, forbidden)
    for abi, path, _ in apks:
        inspect_archive(path, forbidden, allow_apk=True)
    # Each distributed AAR must share its core byte-for-byte with its selected
    # sanitized SDK. The old tested APK remains a separate frozen identity.
    for abi, aar in aars:
        sdk = selected_sdks['android-' + abi]
        with zipfile.ZipFile(aar) as a, zipfile.ZipFile(sdk) as s:
            require(a.read('jni/' + abi + '/libmgmem.so') == s.read('lib/libmgmem.so'), 'distribution_AAR_SDK_core_mismatch:' + abi)
    # Verify x86 derived AAR was built from the exact ARM base Java/notice and
    # the selected x86 shared Memory and JNI overrides, not a second core.
    require(builds[1]['base_aar']['sha256'] == builds[0]['input_aar']['sha256'], 'derived_AAR_base_identity')
    for item in builds[1].get('native_overrides', []):
        checked(item)
    require({item.get('kind') for item in builds[1].get('native_overrides', [])} == {'core', 'jni'}, 'derived_AAR_override_closure')

    tracked = subprocess.check_output(['git', '-c', 'safe.directory=' + ROOT.as_posix(), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).decode('utf-8').split('\0')
    entries = {}
    active_prefix = (BASE / args.distribution_long_receipt).parent.relative_to(ROOT).as_posix().rstrip('/') + '/'
    for name in sorted(set(tracked)):
        if args.defer_distribution_long and name.startswith(active_prefix):
            continue
        if not name or not safe_public(name) or name.startswith(('research/', 'docs/research/', 'build/', 'install/', 'dist/')):
            continue
        if name.startswith('artifacts/') and not name.startswith(('artifacts/android-close-investigation-20261030/', 'artifacts/hang-diagnostic-20261030/')):
            continue
        path = (ROOT / name).resolve()
        require(path.is_relative_to(ROOT), 'source_outside_workspace')
        if path.is_file():
            require(not (ROOT / name).is_symlink(), 'source_symlink_not_allowed')
            scan_bytes(name, path.read_bytes(), forbidden)
            if path.suffix.lower() in ('.zip', '.aar', '.jar'):
                inspect_archive(path, forbidden)
            entries['source/' + name] = path
    public_goldens = verify_public_capture_goldens(entries)
    for name in ('baseline-freezer.json', 'retry-host/summary.json', 'retry-host/red-receipt.json', 'SOURCE_LOCK.json'):
        require('source/artifacts/android-close-investigation-20261030/' + name in entries, 'public_evidence_not_in_bundle:' + name)
    require('source/' + relative(component_path) in entries, 'diagnostic_component_evidence_not_in_bundle')

    arm_out = dist / ('MeguvizMemory-Android-Pressure-0.2.0-arm64-' + args.distribution_revision + '.aar')
    x86_out = dist / ('MeguvizMemory-Android-Pressure-0.2.0-x86_64-' + args.distribution_revision + '.aar')
    for source, target in ((aars[0][1], arm_out), (aars[1][1], x86_out)):
        if target.exists():
            require(target.is_file() and sha(target) == sha(source), 'refuse_existing_different_AAR')

    if args.verify_only:
        print(json.dumps({'state': 'VERIFIED_READ_ONLY_PREFLIGHT',
                          'tested_long_qualification': 'PASS_OBSERVED_UNFROZEN_SAMPLES' if long['qualified'] else 'NOT_QUALIFIED',
                          'distribution_smoke_qualification': 'PASS_SCOPED' if distribution_smoke['qualified'] else 'NOT_QUALIFIED',
                          'distribution_long_qualification': 'PASS_OBSERVED_UNFROZEN_SAMPLES' if distribution_long['qualified'] else 'NOT_QUALIFIED',
                          'distribution_host': distribution_host['state'], 'source_count': len(entries),
                          'active_round_receipt': 'NOT_OPENED' if args.defer_distribution_long else 'NOT_DEFERRED',
                          'privacy_scan': 'PASS'}, ensure_ascii=False))
        return

    # All proof, identities, privacy and planned output checks above run before
    # creating any new deliverable. A failure never marks a long round as PASS.
    stage_copy(aars[0][1], arm_out); stage_copy(aars[1][1], x86_out)
    packages += [arm_out, x86_out]
    for path in packages:
        entries['packages/' + path.name] = path
    for abi, path, proof in apks:
        entries['diagnostic/' + abi + '/MeguvizMemory-R8-Stability-Probe.apk'] = path
    allowed_apks = {'diagnostic/arm64-v8a/MeguvizMemory-R8-Stability-Probe.apk',
                    'diagnostic/x86_64/MeguvizMemory-R8-Stability-Probe.apk'}
    require({name for name in entries if name.lower().endswith('.apk')} == allowed_apks, 'unexpected_APK_bundle_entries')

    result = {'schema_version': 2, 'version': '0.2.0', 'native_abi': '1.1',
              'tested_pressure_revision': 'retryfix2', 'distribution_pressure_revision': args.distribution_revision,
              'qualification': 'ENGINEERING_PREVIEW_NOT_FORMAL_RELEASE_NOT_PERFORMANCE',
              'source_identity': source_lock, 'frozen_plans': frozen, 'baseline_freezer': baseline,
              'short_controls': controls, 'host_retry_regression': host,
              'retry_smoke': smoke, 'retry_long': long,
              'tested_long_binary': {'APK': identity(tested_apks[0]), 'build_receipt': identity(tested_build_paths[0]),
                  'native_source_sha256': tested_plan.get('native_source_sha256'),
                  'native_library_sha256': tested_plan.get('native_library_sha256'),
                  'qualification': 'PASS_OBSERVED_UNFROZEN_SAMPLES' if long['qualified'] else 'NOT_QUALIFIED'},
              'distribution_binary': {'APK': identity(apks[0][1]), 'build_receipt': identity(build_files[0]),
                  'native_build_receipt': identity(native_build_file), 'native_notice_receipt': identity(native_notice_file),
                  'native_source_sha256': distribution_source_sha, 'Java_backend_source_sha256': native_backend[0]['sha256'],
                  'byte_equal_to_tested_APK': sha(apks[0][1]) == sha(tested_apks[0]),
                  'smoke': distribution_smoke, 'first_long': first_distribution_long,
                  'long': distribution_long, 'postflight': distribution_postflight,
                  'long_qualification': 'PASS_OBSERVED_UNFROZEN_SAMPLES' if distribution_long['qualified'] else 'NOT_QUALIFIED',
                  'old_long_result_transferred': False},
              'android_long_qualification': 'PASS_FOR_DISTRIBUTION_BINARY_ONLY' if distribution_long['qualified'] else 'NOT_QUALIFIED',
              'API34_x86_64': api34,
              'API34_source_redaction': api34_redaction,
              'tested_API34_x86_64_runtime': 'PASS_RETRYFIX2_TESTED_BINARY_SCOPED_SHORT_ONLY' if api34['smoke']['qualified'] else 'UNAVAILABLE_OR_NOT_QUALIFIED',
              'distribution_API34_x86_64': distribution_api34,
              'distribution_API34_x86_64_runtime': 'PASS_SCOPED_FINAL_SHORT_NOT_LONG' if distribution_api34['qualified'] else 'UNAVAILABLE_OR_NOT_QUALIFIED',
              'diagnostic_component_scope': {'receipt': identity(component_path), 'exported': True,
                  'required_permission': 'android.permission.DUMP',
                  'authorized_callers': 'SIGNATURE_PRIVILEGED_OR_DEVELOPMENT_AUTHORIZED_NOT_ONLY_SHELL',
                  'production_AAR_contains_activity': False, 'ordinary_app_denial_runtime_test': 'NOT_RUN'},
              'artifact_builds': build_paths, 'SDK_strip_proof': sdk_proof, 'JNI_strip_proof': jni_proof,
              'distribution_member_proof': member_proof,
              'distribution_host_regression': distribution_host, 'public_capture_goldens': public_goldens,
              'packages': [identity(path) for path in packages],
              'diagnostic_APKs': [{**identity(path), 'abi': abi, 'package_proof': proof} for abi, path, proof in apks],
              'bundle': {'path': relative(bundle), 'sha256_sidecar': sidecar.name, 'hash_location': 'SIDECAR_AVOIDS_SELF_REFERENCE'},
              'privacy': 'ONLY_TWO_VERIFIED_OWN_APKS; NO_RAW_INPUT_DEX/APK, SERIALS, AUTHORIZATIONS, KEYS OR RAW_COMMAND_LOGS',
              'BIL': 'NOT_MODIFIED_NOT_INTEGRATED', 'performance': 'NOT_MEASURED_NO_NEW_QUALIFICATION',
              'energy': 'UNAVAILABLE', 'formal_release': 'NOT_QUALIFIED',
              'limits': ['Freezer samples are observational and cannot prove no freeze between samples',
                         'Foreground and background controls are associative; bootstrap/restart also differ',
                         'JNI fake VM tests do not independently qualify ART',
                         'Long PASS, if achieved, means bounded repeated create-close only; not 24h or single long-lived domain',
                         'RSS/PSS are whole-process observations, not domain attribution or a savings claim',
                         '16KB loader, MTE/HWASan, energy, BIL host/UI and formal release remain separate']}
    manifest_bytes = (json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    scan_bytes('stage_manifest', manifest_bytes, forbidden)
    with zipfile.ZipFile(bundle, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(entries.items()):
            archive.write(path, name)
        archive.writestr('source/artifacts/android-close-investigation-20261030/ARTIFACTS.json', manifest_bytes)
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        require(archive.testzip() is None and len(names) == len(set(names)), 'bundle_integrity')
        require({name for name in names if name.lower().endswith('.apk')} == allowed_apks, 'bundle_APK_allowlist')
        require(all(not name.lower().endswith(('.dex', '.key', '.pem', '.keystore', '.jks', '.p12', '.pfx')) for name in names), 'bundle_secret_or_input_member')
        for name, path in entries.items():
            require(archive.read(name) == path.read_bytes(), 'bundle_member_drift:' + name)
        require(archive.read('source/artifacts/android-close-investigation-20261030/ARTIFACTS.json') == manifest_bytes, 'bundle_manifest_drift')
    with manifest.open('xb') as file:
        file.write(manifest_bytes)
    with sidecar.open('x', encoding='ascii', newline='\n') as file:
        file.write(sha(bundle) + '  ' + bundle.name + '\n')
    print(json.dumps({'state': 'VERIFIED_SCOPED_DELIVERY', 'android_long_qualification': result['android_long_qualification'],
                      'bundle': identity(bundle), 'entries': len(entries) + 1, 'manifest': identity(manifest)}))


if __name__ == '__main__':
    main()
