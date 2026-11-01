#!/usr/bin/env python3
"""Run a private, profile-bound frozen v2 suite; never release a device.

The collector checks source closure at startup and completion, not continuously.
Timeouts and interrupts leave remote process state unknown; no process is killed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import sys

import freeze_arm64_input_profile as inputs
import freeze_device_suite_profile_v2 as freezer
from device_capture_v2 import CaptureV2, REMOTE, object_sha
from paired_suite_v2 import default_protocol, freeze_errors

ROOT = Path(__file__).resolve().parents[1]


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def private(path, maximum=inputs.MAX_PROFILE):
    return inputs.regular(path, inputs.build_root(), maximum)


def document(path):
    path, data = private(path)
    value = inputs.unique_json(data)
    require(isinstance(value, dict), 'invalid_private_document')
    return path, data, value


def relative_path(value):
    require(isinstance(value, str) and value and ':' not in value and '\\' not in value and
            not PurePosixPath(value).is_absolute() and '..' not in PurePosixPath(value).parts,
            'invalid_relative_path')
    return ROOT / value


def stamp(value):
    require(isinstance(value, str), 'invalid_expiry')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.tzinfo is not None, 'expiry_timezone_missing')
    return result.astimezone(timezone.utc)


def preflight(protocol_path, plan_path, admission_path, adb_path):
    """Check all bindings before the collector can issue any ADB command."""
    _, protocol_bytes, protocol = document(protocol_path)
    _, plan_bytes, plan = document(plan_path)
    apath, admission_bytes, admission = document(admission_path)
    require(not freeze_errors(protocol, plan), 'protocol_or_plan_invalid')
    frozen = protocol.get('freeze', {})
    provenance = frozen.get('profile_provenance')
    require(isinstance(provenance, dict), 'profile_provenance_missing')
    admission_sha = inputs.sha(admission_bytes)
    require(provenance.get('admission_sha256') == admission_sha, 'admission_not_frozen_identity')
    require(admission.get('state') == 'RELEASED_FOR_MEMORY' and
            freezer.nonempty_evidence(admission.get('owner_evidence')) and
            stamp(admission.get('expires_at_utc')) > datetime.now(timezone.utc), 'admission_invalid')

    profile_path = relative_path(provenance.get('profile_path'))
    profile_file, profile_bytes = private(profile_path)
    profile_sha = provenance.get('profile_sha256')
    require(isinstance(profile_sha, str) and inputs.SHA.fullmatch(profile_sha) and
            inputs.sha(profile_bytes) == profile_sha, 'profile_hash_mismatch')
    profile = inputs.validate(profile_file, profile_sha)
    require(profile.get('profile_id') == provenance.get('profile_id'), 'profile_identity_mismatch')

    receipts = frozen.get('functional_receipts')
    expected = {'state': 'PASS', 'all_passed': True, 'scope': freezer.SCOPE, 'completed_cases': freezer.COUNTS}
    require(isinstance(receipts, list) and len(receipts) == 1 and isinstance(receipts[0], dict) and
            receipts[0].get('expected') == expected, 'functional_contract_invalid')
    receipt_spec = receipts[0]
    fpath, fbytes, functional = document(relative_path(receipt_spec.get('path')))
    require(inputs.sha(fbytes) == receipt_spec.get('sha256') == frozen.get('functional_receipt_sha256') and
            all(type(functional.get(k)) is type(v) and functional.get(k) == v for k, v in expected.items()),
            'functional_identity_mismatch')
    journal_path, journal_bytes = freezer.validate_functional_evidence(fpath, functional)
    require(isinstance(functional.get('core_safety'), dict) and
            functional['core_safety'].get('status') == 'PASS' and
            type(functional['core_safety'].get('tests')) is int and
            functional['core_safety']['tests'] == 3, 'functional_safety_missing')

    dpath, dbytes, deployment = document(relative_path(provenance.get('deployment_path')))
    require(inputs.sha(dbytes) == provenance.get('deployment_sha256') and
            dpath.parent == fpath.parent and dpath.name == 'deployment.json', 'deployment_identity_mismatch')
    device = functional.get('device')
    serial = functional.get('device_serial_sha256')
    require(isinstance(device, dict) and isinstance(serial, str) and inputs.SHA.fullmatch(serial) and
            isinstance(device.get('boot_id_sha256'), str) and inputs.SHA.fullmatch(device['boot_id_sha256']) and
            device.get('requested_cpu') == 2 and type(device.get('requested_cpu')) is int and
            type(device.get('api')) is int and device['api'] >= 27 and
            type(device.get('page_bytes')) is int and device['page_bytes'] >= 4096 and
            isinstance(device.get('abi_list'), str) and 'arm64-v8a' in device['abi_list'].split(','),
            'functional_device_invalid')
    require(frozen.get('device_profile') == {**device, 'abi': 'arm64-v8a', 'selected_cpu': 2,
                                             'device_serial_sha256': serial} and
            admission.get('device_serial_sha256') == serial and
            functional.get('admission', {}).get('sha256') == admission_sha and
            functional['admission'].get('expires_at_utc') == admission.get('expires_at_utc'),
            'device_boot_or_admission_mismatch')
    binding = functional.get('input_binding')
    require(isinstance(binding, dict) and binding.get('schema_version') == 1 and
            binding.get('identity_scope') == 'PRIVATE_PRE_ADB_AND_PHASE_CHECKPOINTS_NOT_LOADED_IMAGE_ATTESTATION' and
            binding.get('profile_sha256') == profile_sha and binding.get('profile_id') == profile['profile_id'] and
            binding.get('profile_toolchain_entries') == profile['snapshot']['toolchain_entries'],
            'functional_profile_binding_mismatch')
    require(deployment.get('state') == 'FUNCTIONAL_STAGED_IDENTITY_ONLY' and
            deployment.get('device_serial_sha256') == serial and deployment.get('profile') == device and
            functional.get('device_directory_created') is True and
            isinstance(functional.get('device_directory'), str) and
            deployment.get('device_directory') == functional['device_directory'] and
            deployment.get('artifacts') == functional.get('deployment_manifest') and
            deployment.get('input_binding') == binding, 'functional_deployment_mismatch')

    runtime = binding.get('runtime_sources')
    runtime_names = {'tools/' + name for name in freezer.PROFILE_RUNTIME_SOURCES} | set(inputs.HELPERS)
    require(isinstance(runtime, dict) and set(runtime) == runtime_names, 'runtime_sources_missing')
    for name, identity in runtime.items():
        _, data = inputs.regular(ROOT / name, ROOT)
        require(identity == inputs.record(data), 'functional_runtime_changed')
    tool_path, tool_bytes, tool_manifest = document(fpath.parent / 'private_runtime_tools.json')
    tools = tool_manifest.get('tools')
    runtime_tools = binding.get('runtime_tools')
    require(inputs.sha(tool_bytes) == binding.get('runtime_tools_manifest_sha256') and
            type(tool_manifest.get('schema_version')) is int and tool_manifest['schema_version'] == 1 and
            isinstance(tools, dict) and set(tools) == {'adb', 'python'} and
            isinstance(runtime_tools, dict) and set(runtime_tools) == set(tools), 'runtime_tools_invalid')
    for name, item in tools.items():
        require(isinstance(item, dict) and isinstance(item.get('path'), str) and
                runtime_tools[name] == {'bytes': item.get('bytes'), 'sha256': item.get('sha256')} and
                inputs.external_file(Path(item['path'])) == runtime_tools[name], 'runtime_tool_changed')
    require(inputs.external_file(Path(adb_path).absolute()) == runtime_tools['adb'] and
            inputs.external_file(Path(sys.executable).absolute()) == runtime_tools['python'],
            'capture_runtime_differs_from_functional')

    manifest = freezer.rows_by_name(functional.get('deployment_manifest'))
    roles = profile['snapshot']['roles']
    by_role = {item['role']: item for item in roles}
    require(len(by_role) == len(roles) == len(inputs.ROLES) and set(by_role) == set(inputs.ROLES),
            'profile_roles_invalid')
    for role, name in inputs.ROLES.items():
        item = by_role[role]
        require(item.get('name') == name and name in manifest and manifest[name].get('role') == role and
                {'bytes': item.get('bytes'), 'sha256': item.get('sha256')} ==
                {'bytes': manifest[name]['bytes'], 'sha256': manifest[name]['sha256']}, 'profile_role_mismatch')
    closure = frozen.get('artifact_closure')
    require(isinstance(closure, list) and len(closure) == 5 and
            all(isinstance(item, dict) for item in closure), 'artifact_closure_invalid')
    artifacts = {item.get('role'): item for item in closure}
    names = {'library': 'libmgmem.so', 'dex_formal': 'mgmem_dex_paired_v2',
             'image_formal': 'mgmem_image_perf', 'dex_input': 'input.dex', 'image_input': 'input.ppm'}
    require(len(artifacts) == len(closure) and set(artifacts) == set(names), 'artifact_roles_invalid')
    for role, name in names.items():
        item = artifacts[role]
        manifest_name = 'image-1920x1080.ppm' if role == 'image_input' else name
        require(item.get('name') == name and item.get('executable') is (role in ('dex_formal', 'image_formal')) and
                manifest_name in manifest, 'artifact_mapping_invalid')
        path, data = private(relative_path(item.get('path')), inputs.MAX_FILE)
        require(inputs.record(data) == {'bytes': manifest[manifest_name]['bytes'], 'sha256': manifest[manifest_name]['sha256']} and
                item.get('sha256') == inputs.sha(data), 'artifact_identity_mismatch')
        if role in by_role:
            require(item['path'] == by_role[role].get('path'), 'artifact_profile_path_mismatch')
        else:
            require(inputs.relative(path) == manifest[manifest_name].get('private_path'), 'artifact_input_path_mismatch')

    # freeze_errors checks the structural schema, not the fixed meanings of
    # variants, budgets, or baseline contrasts; bind those explicitly.
    baseline = default_protocol()
    require(protocol.get('sampling') == baseline['sampling'] and
            protocol.get('environment_limits') == baseline['environment_limits'] and
            protocol.get('estimand_id') == baseline['estimand_id'] and
            protocol.get('performance_qualified') is False and protocol.get('BIL_auto_enable') is False and
            protocol.get('old_attempt_pooling') is False and protocol.get('additional_engineering_retry_allowed') is False,
            'sampling_or_qualification_changed')
    require(all(frozen.get(key) == value for key, value in {
        'single_task_timeout_seconds': 30, 'session_wall_cap_seconds': 110,
        'overall_wall_cap_seconds': 2400, 'maximum_environment_waits': 5}.items()) and
        frozen.get('cadence') == {'battery_source': frozen.get('cadence', {}).get('battery_source'),
            'background_window_seconds': .4, 'busy_interval_min_ns': 300000000,
            'busy_interval_max_ns': 1500000000, 'observation_to_launch_max_ns': 2000000000,
            'cooldown_seconds': 1, 'window_start_lateness_max_seconds': 45,
            'minimum_actual_window_start_gap_seconds': 90} and
        frozen['cadence']['battery_source'] in ('sysfs', 'dumpsys_battery') and
        frozen.get('resource_budgets') == {'scope': 'NATIVE_REQUESTED_INPUT_WORKSPACE_AND_PARSER_ACCOUNTS_ONLY',
            'metadata_RSS_PSS_energy': 'UNMEASURED_NOT_ZERO_OR_NO_REGRESSION',
            'limits': 'families.<family>.resource_limits'}, 'capture_bounds_changed')
    _, image_bytes = inputs.regular(ROOT / 'bench/image_perf_v2.json', ROOT)
    image = inputs.unique_json(image_bytes)
    image_identity = baseline['families']['image']['semantic_identity']
    image_identity.update({key: image['input'][key] for key in ('source_sha256', 'input_bytes', 'width', 'height')})
    image_identity.update({key: image['completion'][key] for key in
                           ('frames', 'completed_frames', 'result_checksum', 'output_hash_fnv64', 'gray_conversions')})
    oracle = functional.get('oracles', {}).get('image-1920x1080.ppm', {})
    require(isinstance(oracle, dict) and oracle.get('width') == image['input']['width'] == 1920 and
            oracle.get('height') == image['input']['height'] == 1080 and
            oracle.get('sha256') == image['completion']['oracle_pgm_sha256'] and
            type(oracle.get('checksum')) is int and
            oracle['checksum'] * 3 == image['completion']['result_checksum'] and
            oracle.get('output_hash_fnv64') == image['completion']['output_hash_fnv64'] and
            artifacts['dex_input']['sha256'] == baseline['families']['dex']['semantic_identity']['source_sha256'] and
            artifacts['image_input']['sha256'] == image['input']['source_sha256'], 'input_semantic_mismatch')
    for family, body in baseline['families'].items():
        for variant in body['variants']:
            if family == 'dex':
                limits = {'unique_owned_backing_peak_bytes': 0 if variant == 'D-Z0' else 10540184,
                          'external_source_capacity_bytes': 21080368, 'parser_peak_owned_bytes': 268435456,
                          'reserved_final_bytes': 0}
            else:
                limits = {'unique_owned_backing_peak_bytes': 10368000 if variant == 'I-DF' else
                          8359936 if variant == 'I-AR' else 8300160,
                          'external_source_capacity_bytes': 12441634, 'steady_state_provider_allocations': 0,
                          'reserved_final_bytes': 0}
            body['resource_limits'][variant] = limits
        require(protocol['families'][family] == body, 'family_semantics_changed')

    sources = frozen.get('source_closure')
    require(isinstance(sources, list) and all(isinstance(item, dict) for item in sources), 'source_closure_invalid')
    source_map = {item.get('path'): item.get('sha256') for item in sources}
    require(len(source_map) == len(sources), 'duplicate_source_closure')
    private_paths = (profile_file, dpath, apath, tool_path, fpath, journal_path)
    expected_sources = {item['path']: item['sha256'] for item in freezer.source_records()}
    for path in private_paths:
        _, data = private(path, inputs.MAX_FILE if path == journal_path else inputs.MAX_PROFILE)
        expected_sources[inputs.relative(path)] = inputs.sha(data)
    require(expected_sources[inputs.relative(journal_path)] == inputs.sha(journal_bytes),
            'functional_journal_changed')
    require(source_map == expected_sources, 'source_closure_mismatch')
    return {'protocol_sha256': inputs.sha(protocol_bytes), 'plan_sha256': inputs.sha(plan_bytes),
            'admission_sha256': admission_sha, 'serial_sha256': serial}


def validate_deployment(deployment, serial_sha, boot_sha, protocol, artifacts):
    """Validate local stage identity before ADB; remote hashes need ADB later."""
    require(isinstance(deployment, dict) and deployment.get('state') == 'STAGED' and
            deployment.get('device_serial_sha256') == serial_sha and
            deployment.get('boot_id_sha256') == boot_sha and
            deployment.get('protocol_canonical_sha256') == object_sha(protocol) and
            deployment.get('artifacts') == artifacts and
            REMOTE.fullmatch(str(deployment.get('device_directory', ''))),
            'deployment_identity_mismatch')


class ProfileCapture(CaptureV2):
    def __init__(self, *args, expected, **kwargs):
        self.expected_profile = expected
        self.expected_deployment_sha256 = None
        super().__init__(*args, **kwargs)

    def validate(self):
        super().validate()
        actual = preflight(self.protocol_path, self.plan_path, self.args.admission, self.args.adb)
        require(actual == self.expected_profile and self.receipt['device_serial_sha256'] == actual['serial_sha256'] and
                self.admission_file_sha == actual['admission_sha256'], 'capture_identity_changed')
        if self.phase != 'stage':
            _, deployment_bytes, deployment = document(self.deployment_path)
            validate_deployment(deployment, self.receipt['device_serial_sha256'],
                                self.protocol['freeze']['device_profile']['boot_id_sha256'],
                                self.protocol, list(self.artifacts.values()))
            self.expected_deployment_sha256 = inputs.sha(deployment_bytes)

    def check_immutable(self, include_source=False):
        super().check_immutable(include_source=include_source)
        if self.protocol is not None:
            require(self.admission_file_sha == self.expected_profile['admission_sha256'] and
                    self.protocol_file_sha == self.expected_profile['protocol_sha256'] and
                    self.plan_file_sha == self.expected_profile['plan_sha256'], 'capture_frozen_identity_changed')
            if self.phase != 'stage':
                require(self.expected_deployment_sha256 is not None and
                        inputs.sha(private(self.deployment_path)[1]) == self.expected_deployment_sha256,
                        'deployment_changed_during_capture')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('adb', 'admission', 'protocol', 'plan', 'out-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--phase', choices=('stage', 'pilot', 'formal'), required=True)
    parser.add_argument('--deployment', type=Path)
    parser.add_argument('--pilot-journal', type=Path)
    args = parser.parse_args()
    try:
        require((args.phase == 'stage') == (args.deployment is None), 'deployment_phase_mismatch')
        require((args.phase == 'formal') == (args.pilot_journal is not None), 'pilot_journal_phase_mismatch')
        output = inputs.confined(args.out_dir, inputs.build_root(), exists=False)
        require(not output.exists(), 'output_already_exists')
        if args.deployment is not None:
            private(args.deployment)
        if args.pilot_journal is not None:
            private(args.pilot_journal, inputs.MAX_FILE)
        expected = preflight(args.protocol, args.plan, args.admission, args.adb)
        runner = ProfileCapture(ROOT, args.adb, args.serial, args.admission, args.protocol, args.plan,
                                output, args.phase, deployment_path=args.deployment,
                                pilot_journal_path=args.pilot_journal, expected=expected)
        result = runner.run()
        print(json.dumps({'state': result['state'], 'phase': args.phase,
                          'native_tasks_recorded': result['native_tasks_recorded'], 'performance_qualified': False}))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        print(json.dumps({'state': 'CAPTURE_STOPPED', 'performance_qualified': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
