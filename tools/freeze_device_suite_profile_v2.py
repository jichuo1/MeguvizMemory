#!/usr/bin/env python3
"""Freeze a private profile-bound suite; no device operations are performed.

Requires a fresh profile-aware functional receipt with persisted provenance.
The resulting order is descriptive, does not qualify performance, and is not a
substitute for device admission or the collector's separate staging step.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets

import freeze_arm64_input_profile as inputs
from paired_suite_v2 import default_protocol, freeze_errors, make_plan
from run_arm64_validation import nonempty_evidence
from validate_arm64_consumers_profile_v2 import (PROFILE_RUNTIME_SOURCES, CORE_PROGRAMS, make_specs,
                                                 core_checks, dex_checks, image_checks)

ROOT = Path(__file__).resolve().parents[1]
SCOPE = 'ARM64_V2_PROFILE_CONSUMERS_FUNCTIONAL_ONLY_NOT_PERFORMANCE'
COUNTS = {'dex': 32, 'image_workspace': 156, 'image_perf': 8}
SOURCES = tuple(dict.fromkeys(inputs.HELPERS + (
    'tools/freeze_device_suite_profile_v2.py', 'tools/validate_arm64_consumers_profile_v2.py',
    'tools/device_capture_v2.py', 'tools/paired_suite_v2.py', 'tools/paired_analysis_v2.py',
    'tools/run_arm64_validation.py', 'tools/run_paired_device.py', 'tools/run_device_suite_v2.py',
    'tools/run_device_suite_profile_v2.py',
    'tools/verify_dex_borrow_v2.py', 'tools/verify_image_workspace_v2.py', 'tools/verify_image_perf_v2.py',
    'consumers/image/perf.cpp', 'consumers/image/CMakeLists.txt',
    'consumers/dex/paired_v2.cpp', 'consumers/dex/CMakeLists.txt',
    'bench/paired_protocol_v2_draft.json', 'bench/image_perf_v2.json')))


def require(value, reason):
    if not value:
        raise ValueError(reason)


def private_file(path, maximum=inputs.MAX_FILE):
    return inputs.regular(path, inputs.build_root(), maximum)


def private_json(path):
    selected, data = private_file(path, inputs.MAX_PROFILE)
    value = inputs.unique_json(data)
    require(isinstance(value, dict), 'invalid_json_object')
    return selected, data, value


def timestamp(value):
    require(isinstance(value, str), 'invalid_timestamp')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(stamp.tzinfo is not None, 'timestamp_needs_timezone')
    return stamp.astimezone(timezone.utc)


def source_records():
    result = []
    for name in SOURCES:
        path, data = inputs.regular(ROOT / name, ROOT)
        result.append({'path': inputs.relative(path), 'sha256': inputs.sha(data)})
    return result


def rows_by_name(rows):
    require(isinstance(rows, list) and rows, 'deployment_manifest_missing')
    result = {}
    for row in rows:
        require(isinstance(row, dict), 'deployment_item_invalid')
        name = row.get('name')
        require(isinstance(name, str) and name and name not in result, 'deployment_name_invalid')
        require(type(row.get('bytes')) is int and row['bytes'] > 0 and
                isinstance(row.get('sha256'), str) and inputs.SHA.fullmatch(row['sha256']) and
                row.get('device_sha256') == row['sha256'], 'deployment_identity_invalid')
        result[name] = row
    return result


def validate_functional_evidence(receipt_path, functional):
    """Bind the actual per-case journal, not merely a self-reported PASS count.

    This is consistency checking of local evidence, not a signed attestation.
    """
    path, data = private_file(receipt_path.parent / 'functional.jsonl')
    require(data.endswith(b'\n'), 'functional_journal_incomplete')
    rows = [inputs.unique_json(line) for line in data.splitlines()]
    specs = make_specs()
    expected = [('core_safety', name) for name in CORE_PROGRAMS] + [(s['group'], s['id']) for s in specs]
    require(len(rows) == len(expected) and functional.get('native_session_rows') == len(expected),
            'functional_journal_count_mismatch')
    core = functional.get('core_safety', {}).get('results')
    require(isinstance(core, dict) and set(core) == set(CORE_PROGRAMS), 'functional_core_results_missing')
    oracles = functional.get('oracles')
    require(isinstance(oracles, dict), 'functional_oracles_missing')
    for index, (row, (group, case_id)) in enumerate(zip(rows, expected)):
        require(isinstance(row, dict) and row.get('schema_version') == 2 and
                row.get('scope') == 'FUNCTIONAL_NOT_PERFORMANCE' and row.get('group') == group and
                row.get('case_id') == case_id and row.get('success') is True, 'functional_case_invalid')
        checks, command, native = row.get('checks'), row.get('command_result'), row.get('native')
        require(isinstance(checks, dict) and checks and all(value is True for value in checks.values()) and
                isinstance(command, dict) and command.get('timed_out') is False and
                command.get('launch_error') is None and isinstance(command.get('stdout'), str) and
                isinstance(command.get('stderr'), str) and not command['stderr'].strip(), 'functional_case_command_invalid')
        if group == 'core_safety':
            parsed, computed = core_checks(case_id, command['stdout'])
            require(command.get('exit_code') == 0 and parsed == native and computed == checks and
                    core[case_id] == {'native': native, 'checks': checks, 'status': 'PASS'},
                    'functional_core_result_mismatch')
            continue
        spec = specs[index - len(CORE_PROGRAMS)]
        require(command.get('exit_code') == (1 if spec.get('expected_error') else 0) and
                isinstance(native, dict) and inputs.unique_json(command['stdout'].encode('utf-8')) == native,
                'functional_native_result_mismatch')
        if group == 'dex':
            computed = dex_checks(native, spec['variant'], spec['case'], spec['diagnostic'], strict_contract=True)
            computed['CPU_affinity'] = all(type(native.get(k)) is int and native[k] == 2
                                           for k in ('selected_cpu', 'cpu_before', 'cpu_after'))
            require(computed == checks, 'functional_dex_checks_mismatch')
        elif group == 'image_workspace':
            image_oracle = oracles.get(spec['input'])
            if image_oracle is not None:
                spec = {**spec, 'checksum': image_oracle['checksum']}
            computed = image_checks(native, spec)
            if not spec.get('expected_error'):
                output = row.get('output_oracle', {})
                require(isinstance(image_oracle, dict) and output.get('actual_pgm_sha256') ==
                        output.get('expected_pgm_sha256') == image_oracle.get('sha256'), 'functional_image_output_mismatch')
                computed['complete_PGM_byte_oracle'] = True
            else:
                # The separate no-output command is recorded in the command journal.
                computed['no_output_created'] = checks.get('no_output_created') is True
            require(computed == checks, 'functional_image_checks_mismatch')
        else:
            required = {'success', 'normal_case', 'schema_workload', 'native_variant', 'input_sha', 'input_size',
                        'provider_layout', 'dimensions', 'complete_frames', 'all_pixel_sum', 'complete_output_digest',
                        'gray_once', 'diagnostic_mode', 'no_phases', 'source_final_release', 'lease_lifetime',
                        'closed_zero', 'final_reserved_zero', 'positive_total', 'payload_model', 'backing_model',
                        'provider_model', 'unknowns_not_zero', 'actual_steady_calls', 'CPU_affinity',
                        'resource_counters_nonnegative_integers'}
            if spec['capture']:
                required.add('complete_PGM_byte_oracle')
                output, oracle = row.get('output_oracle', {}), oracles.get(spec['input'], {})
                require(output.get('actual_pgm_sha256') == output.get('expected_pgm_sha256') == oracle.get('sha256') and
                        isinstance(oracle.get('sha256'), str), 'functional_perf_output_mismatch')
            require(set(checks) == required and native.get('success') is True and native.get('diagnostic') is spec['capture'] and
                    native.get('provider') == spec['provider'] and native.get('layout') == spec['layout'] and
                    all(type(native.get(k)) is int and native[k] == 2 for k in ('cpu_requested', 'cpu_before', 'cpu_after')),
                    'functional_perf_checks_mismatch')
    return path, data


def freeze(profile_path, profile_sha256, functional_path, deployment_path, admission_path,
           dex_path, image_path, output, start_delay=240, battery_source='dumpsys_battery', now=None):
    require(type(start_delay) is int and 120 <= start_delay <= 1800, 'invalid_start_delay')
    require(battery_source in ('sysfs', 'dumpsys_battery'), 'invalid_battery_source')
    now = now or datetime.now(timezone.utc)
    require(now.tzinfo is not None, 'clock_requires_timezone')
    now = now.astimezone(timezone.utc)
    profile = inputs.validate(profile_path, profile_sha256)
    profile_file, profile_data = private_file(profile_path, inputs.MAX_PROFILE)
    fpath, fdata, functional = private_json(functional_path)
    dpath, ddata, deployment = private_json(deployment_path)
    apath, adata, admission = private_json(admission_path)
    require(fpath.parent == dpath.parent and dpath.name == 'deployment.json', 'deployment_not_same_run')
    require(functional.get('state') == 'PASS' and functional.get('all_passed') is True and
            functional.get('scope') == SCOPE and functional.get('completed_cases') == COUNTS,
            'profile_functional_pass_required')
    core = functional.get('core_safety', {})
    require(core.get('status') == 'PASS' and type(core.get('tests')) is int and core['tests'] == 3,
            'fresh_core_safety_required')
    journal_path, journal_data = validate_functional_evidence(fpath, functional)
    device = functional.get('device', {})
    device_hash = functional.get('device_serial_sha256')
    require(isinstance(device_hash, str) and inputs.SHA.fullmatch(device_hash) and
            isinstance(device, dict) and isinstance(device.get('boot_id_sha256'), str) and
            inputs.SHA.fullmatch(device['boot_id_sha256']) and
            isinstance(device.get('abi_list'), str) and 'arm64-v8a' in device['abi_list'].split(',') and
            type(device.get('api')) is int and device['api'] >= 27 and
            type(device.get('page_bytes')) is int and device['page_bytes'] >= 4096 and
            type(device.get('requested_cpu')) is int and device['requested_cpu'] == 2,
            'device_profile_invalid')
    require(admission.get('state') == 'RELEASED_FOR_MEMORY' and nonempty_evidence(admission.get('owner_evidence')) and
            admission.get('device_serial_sha256') == device_hash and
            functional.get('admission', {}).get('sha256') == inputs.sha(adata) and
            functional['admission'].get('expires_at_utc') == admission.get('expires_at_utc'),
            'admission_identity_mismatch')
    require(0 <= (now - timestamp(functional.get('completed_at_utc'))).total_seconds() <= 1800,
            'functional_receipt_not_fresh')
    first = now + timedelta(seconds=start_delay)
    require(timestamp(admission.get('expires_at_utc')) > first + timedelta(seconds=15 * 120 + 110),
            'admission_does_not_cover_schedule')
    require(first.astimezone(timezone(timedelta(hours=8))).date() ==
            (first + timedelta(seconds=15 * 120 + 110)).astimezone(timezone(timedelta(hours=8))).date(),
            'schedule_crosses_frozen_local_day')
    require(deployment.get('state') == 'FUNCTIONAL_STAGED_IDENTITY_ONLY' and
            deployment.get('device_serial_sha256') == device_hash and deployment.get('profile') == device and
            functional.get('device_directory_created') is True and isinstance(functional.get('device_directory'), str) and
            deployment.get('device_directory') == functional['device_directory'] and
            deployment.get('artifacts') == functional.get('deployment_manifest'), 'deployment_not_functional_receipt')
    manifest = rows_by_name(functional.get('deployment_manifest'))
    # Functional source provenance must be persisted at execution time: matching
    # binaries afterwards alone does not establish which profile was used.
    provenance = functional.get('input_binding')
    require(isinstance(provenance, dict) and type(provenance.get('schema_version')) is int and provenance['schema_version'] == 1 and
            provenance.get('identity_scope') == 'PRIVATE_PRE_ADB_AND_PHASE_CHECKPOINTS_NOT_LOADED_IMAGE_ATTESTATION' and
            provenance.get('profile_sha256') == profile_sha256 and provenance.get('profile_id') == profile['profile_id'] and
            deployment.get('input_binding') == provenance and
            provenance.get('profile_toolchain_entries') == profile['snapshot']['toolchain_entries'],
            'functional_profile_provenance_missing_or_changed')
    runtime = provenance.get('runtime_sources')
    expected_runtime = {'tools/' + name for name in PROFILE_RUNTIME_SOURCES} | set(inputs.HELPERS)
    require(isinstance(runtime, dict) and set(runtime) == expected_runtime, 'functional_runtime_provenance_missing')
    current_sources = source_records()
    current = {item['path']: item['sha256'] for item in current_sources}
    require(all(isinstance(name, str) and isinstance(record, dict) and
                name in current and record == inputs.record(inputs.regular(ROOT / name, ROOT)[1]) for name, record in runtime.items()),
            'functional_runtime_source_changed')
    tool_path, tool_data, tool_manifest = private_json(fpath.parent / 'private_runtime_tools.json')
    runtime_tools = provenance.get('runtime_tools')
    tool_records = tool_manifest.get('tools')
    require(inputs.sha(tool_data) == provenance.get('runtime_tools_manifest_sha256') and
            type(tool_manifest.get('schema_version')) is int and tool_manifest['schema_version'] == 1 and
            isinstance(runtime_tools, dict) and set(runtime_tools) == {'adb', 'python'} and
            isinstance(tool_records, dict) and set(tool_records) == set(runtime_tools), 'functional_runtime_tools_missing')
    for name, item in tool_records.items():
        require(isinstance(item, dict) and isinstance(item.get('path'), str) and
                runtime_tools[name] == {'bytes': item.get('bytes'), 'sha256': item.get('sha256')} and
                inputs.external_file(Path(item['path'])) == runtime_tools[name],
                'functional_runtime_tool_changed')
    roles = profile['snapshot']['roles']
    by_role = {item['role']: item for item in roles}
    require(len(by_role) == len(roles) == len(inputs.ROLES) and set(by_role) == set(inputs.ROLES),
            'profile_roles_invalid')
    paths = {}
    for role, name in inputs.ROLES.items():
        item = by_role[role]
        require(item.get('name') == name and name in manifest and manifest[name].get('role') == role,
                'profile_role_name_mismatch')
        path, data = private_file(ROOT / item['path'])
        identity = inputs.record(data)
        require(identity == {'bytes': item.get('bytes'), 'sha256': item.get('sha256')} and
                identity == {'bytes': manifest[name]['bytes'], 'sha256': manifest[name]['sha256']},
                'profile_role_identity_mismatch')
        paths[role] = (path, data)
    dex_file, dex_data = private_file(dex_path)
    image_file, image_data = private_file(image_path)
    for name, data in (('input.dex', dex_data), ('image-1920x1080.ppm', image_data)):
        require(name in manifest and inputs.record(data) ==
                {'bytes': manifest[name]['bytes'], 'sha256': manifest[name]['sha256']}, 'input_identity_mismatch')
    # Paths must be the same assets recorded by the functional run, rather than
    # a coincidentally equal file selected after it.
    require(manifest['input.dex'].get('private_path') == inputs.relative(dex_file) and
            manifest['image-1920x1080.ppm'].get('private_path') == inputs.relative(image_file),
            'functional_input_paths_missing_or_changed')
    _, image_bytes = inputs.regular(ROOT / 'bench/image_perf_v2.json', ROOT)
    image = inputs.unique_json(image_bytes)
    image_input, completion = image['input'], image['completion']
    oracle = functional.get('oracles', {}).get('image-1920x1080.ppm', {})
    require(inputs.sha(image_data) == image_input['source_sha256'] and len(image_data) == image_input['input_bytes'] and
            oracle.get('width') == image_input['width'] == 1920 and oracle.get('height') == image_input['height'] == 1080 and
            oracle.get('sha256') == completion['oracle_pgm_sha256'] and
            oracle.get('checksum') * 3 == completion['result_checksum'] and
            oracle.get('output_hash_fnv64') == completion['output_hash_fnv64'], 'image_oracle_mismatch')
    protocol = default_protocol()
    dex_identity = protocol['families']['dex']['semantic_identity']
    require(inputs.sha(dex_data) == dex_identity['source_sha256'] and len(dex_data) == dex_identity['input_bytes'],
            'real_dex_identity_mismatch')
    ident = secrets.token_hex(16)
    protocol.update({'experiment_id': 'MM-v2-profile-' + ident, 'attempt_id': 'profile-' + ident,
                     'state': 'FROZEN_BEFORE_PILOT', 'device_execution': 'AUTHORIZED_CURRENT_BATCH',
                     'frozen_at_utc': now.isoformat(),
                     'parent_protocol_file_sha256': current['bench/paired_protocol_v2_draft.json'],
                     'old_attempt_pooling': False, 'additional_engineering_retry_allowed': False})
    identity = protocol['families']['image']['semantic_identity']
    identity.update({key: image_input[key] for key in ('source_sha256', 'input_bytes', 'width', 'height')})
    identity.update({key: completion[key] for key in ('frames', 'completed_frames', 'result_checksum', 'output_hash_fnv64', 'gray_conversions')})
    artifacts = []
    for role, name, executable, pair in (
        ('library', 'libmgmem.so', False, paths['library']),
        ('dex_formal', 'mgmem_dex_paired_v2', True, paths['dex_formal']),
        ('image_formal', 'mgmem_image_perf', True, paths['image_formal']),
        ('dex_input', 'input.dex', False, (dex_file, dex_data)),
        ('image_input', 'input.ppm', False, (image_file, image_data))):
        artifacts.append({'role': role, 'path': inputs.relative(pair[0]), 'name': name,
                          'sha256': inputs.sha(pair[1]), 'executable': executable})
    for family, body in protocol['families'].items():
        for variant in body['variants']:
            if family == 'dex':
                limits = {'unique_owned_backing_peak_bytes': 0 if variant == 'D-Z0' else 10540184,
                          'external_source_capacity_bytes': 21080368, 'parser_peak_owned_bytes': 268435456,
                          'reserved_final_bytes': 0}
            else:
                limits = {'unique_owned_backing_peak_bytes': 10368000 if variant == 'I-DF' else 8359936 if variant == 'I-AR' else 8300160,
                          'external_source_capacity_bytes': 12441634, 'steady_state_provider_allocations': 0, 'reserved_final_bytes': 0}
            body['resource_limits'][variant] = limits
    schedule = []
    for index in range(16):
        phase = 'pilot' if index < 4 else 'formal'
        start = first + timedelta(seconds=index * 120)
        schedule.append({'phase': phase, 'window_id': f'{phase}-w{index if index < 4 else index - 4:02d}',
                         'start_utc': start.isoformat(), 'day_id': start.astimezone(timezone(timedelta(hours=8))).date().isoformat(),
                         'environment_id': 'profile-shared-day-environment'})
    frozen_device = {**device, 'abi': 'arm64-v8a', 'selected_cpu': 2, 'device_serial_sha256': device_hash}
    protocol['freeze'].update({'device_profile': frozen_device, 'artifact_closure': artifacts,
        'functional_receipt_sha256': inputs.sha(fdata), 'functional_receipts': [{'path': inputs.relative(fpath),
        'sha256': inputs.sha(fdata), 'expected': {'state': 'PASS', 'all_passed': True, 'scope': SCOPE, 'completed_cases': COUNTS}}],
        'source_closure': current_sources + [
            {'path': inputs.relative(path), 'sha256': inputs.sha(data)}
            for path, data in ((profile_file, profile_data), (fpath, fdata), (journal_path, journal_data), (dpath, ddata),
                               (apath, adata), (tool_path, tool_data))],
        'profile_provenance': {'profile_path': inputs.relative(profile_file),
        'profile_sha256': profile_sha256, 'profile_id': profile['profile_id'], 'deployment_path': inputs.relative(dpath),
        'deployment_sha256': inputs.sha(ddata), 'admission_sha256': inputs.sha(adata)},
        'host_clock_epoch_id': 'memory-v2-' + ident, 'scheduled_windows': schedule,
        'single_task_timeout_seconds': 30, 'session_wall_cap_seconds': 110, 'overall_wall_cap_seconds': 2400,
        'maximum_environment_waits': 5, 'cadence': {'battery_source': battery_source, 'background_window_seconds': .4,
        'busy_interval_min_ns': 300000000, 'busy_interval_max_ns': 1500000000, 'observation_to_launch_max_ns': 2000000000,
        'cooldown_seconds': 1, 'window_start_lateness_max_seconds': 45, 'minimum_actual_window_start_gap_seconds': 90},
        'resource_budgets': {'scope': 'NATIVE_REQUESTED_INPUT_WORKSPACE_AND_PARSER_ACCOUNTS_ONLY',
        'metadata_RSS_PSS_energy': 'UNMEASURED_NOT_ZERO_OR_NO_REGRESSION', 'limits': 'families.<family>.resource_limits'}})
    plan = make_plan(protocol)
    require(not freeze_errors(protocol, plan), 'frozen_protocol_validation_failed')
    # Recheck every externally supplied object just before writing the freeze.
    require(inputs.validate(profile_file, profile_sha256) == profile and source_records() == current_sources and
            private_file(tool_path, inputs.MAX_PROFILE)[1] == tool_data and
            all(inputs.external_file(Path(item['path'])) == runtime_tools[name] for name, item in tool_records.items()) and
            private_file(fpath, inputs.MAX_PROFILE)[1] == fdata and private_file(dpath, inputs.MAX_PROFILE)[1] == ddata and
            private_file(apath, inputs.MAX_PROFILE)[1] == adata and private_file(journal_path)[1] == journal_data and
            private_file(dex_file)[1] == dex_data and
            private_file(image_file)[1] == image_data and private_file(profile_file, inputs.MAX_PROFILE)[1] == profile_data,
            'freeze_input_changed')
    finished = datetime.now(timezone.utc)
    require(0 <= (finished - timestamp(functional['completed_at_utc'])).total_seconds() <= 1800,
            'functional_receipt_expired_during_freeze')
    require(finished < first - timedelta(seconds=30), 'freeze_completed_too_late_for_schedule')
    out = inputs.confined(output, inputs.build_root(), exists=False)
    require(not os.path.lexists(out), 'output_already_exists')
    out.mkdir(parents=True, exist_ok=False)
    for name, value in (('protocol.json', protocol), ('plan.json', plan)):
        data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
        with (out / name).open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return {'state': 'FROZEN_BEFORE_PILOT', 'planned_tasks': len(plan['entries']), 'performance_qualified': False,
            'device_executed': False, 'privacy': 'PRIVATE_BUILD_ONLY'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('profile', 'functional-receipt', 'deployment', 'admission', 'dex-input', 'image-input', 'out-dir'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--profile-sha256', required=True)
    parser.add_argument('--start-delay', type=int, default=240)
    parser.add_argument('--battery-source', choices=('sysfs', 'dumpsys_battery'), default='dumpsys_battery')
    args = parser.parse_args()
    try:
        result = freeze(args.profile, args.profile_sha256, args.functional_receipt, args.deployment, args.admission,
                        args.dex_input, args.image_input, args.out_dir, args.start_delay, args.battery_source)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        print(json.dumps({'state': 'BLOCKED_PRIVATE_INPUT_OR_PROVENANCE', 'performance_qualified': False,
                          'device_executed': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
