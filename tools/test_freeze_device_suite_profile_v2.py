#!/usr/bin/env python3
"""Offline contract tests; all synthetic test inputs remain under build/."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import freeze_arm64_input_profile as inputs
import freeze_device_suite_profile_v2 as freezer
import validate_arm64_consumers_profile_v2 as producer
from device_capture_v2 import CaptureV2
from run_arm64_validation import Blocked, digest


class ProfileFreezeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='test-profile-freeze-', dir=inputs.build_root())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime.now(timezone.utc)
        self.profile_path = self.file('profile.json', b'profile')
        self.profile_sha = inputs.sha(b'profile')
        self.dex = self.file('input.dex', b'synthetic dex; not a real workload')
        self.image = self.file('image.ppm', b'synthetic image; not a real workload')
        self.tool = self.file('test-tool', b'synthetic tool')
        roles = []
        manifest = []
        for role, name in inputs.ROLES.items():
            path = self.file('role-' + role, ('binary:' + role).encode())
            identity = inputs.record(path.read_bytes())
            roles.append({'role': role, 'name': name, 'path': inputs.relative(path), **identity})
            manifest.append({'role': role, 'name': name, **identity, 'device_sha256': identity['sha256']})
        for name, path in (('input.dex', self.dex), ('image-1920x1080.ppm', self.image)):
            identity = inputs.record(path.read_bytes())
            manifest.append({'name': name, **identity, 'device_sha256': identity['sha256'], 'private_path': inputs.relative(path)})
        self.profile = {'profile_id': 'a' * 32, 'snapshot': {'roles': roles, 'toolchain_entries': {'compiler': {'bytes': 1, 'sha256': 'b' * 64}}}}
        runtime_paths = {'tools/' + item for item in freezer.PROFILE_RUNTIME_SOURCES} | set(inputs.HELPERS)
        runtime_sources = {name: inputs.record(inputs.regular(freezer.ROOT / name, freezer.ROOT)[1]) for name in runtime_paths}
        tool_id = inputs.record(self.tool.read_bytes())
        tool_manifest = {'schema_version': 1, 'tools': {name: {'path': str(self.tool), **tool_id} for name in ('adb', 'python')}}
        self.tools_path = self.jsonfile('private_runtime_tools.json', tool_manifest)
        binding = {'schema_version': 1, 'identity_scope': 'PRIVATE_PRE_ADB_AND_PHASE_CHECKPOINTS_NOT_LOADED_IMAGE_ATTESTATION',
                   'profile_sha256': self.profile_sha, 'profile_id': self.profile['profile_id'],
                   'runtime_sources': runtime_sources, 'runtime_tools': {name: tool_id for name in ('adb', 'python')},
                   'runtime_tools_manifest_sha256': inputs.sha(self.tools_path.read_bytes()),
                   'profile_toolchain_entries': self.profile['snapshot']['toolchain_entries']}
        device = {'abi_list': 'arm64-v8a', 'api': 33, 'page_bytes': 4096, 'requested_cpu': 2, 'boot_id_sha256': 'c' * 64}
        self.admission = {'state': 'RELEASED_FOR_MEMORY', 'device_serial_sha256': 'd' * 64,
                          'owner_evidence': 'released', 'expires_at_utc': (self.now + timedelta(hours=2)).isoformat()}
        self.admission_path = self.jsonfile('admission.json', self.admission)
        self.functional = {'state': 'PASS', 'all_passed': True, 'scope': freezer.SCOPE, 'completed_cases': freezer.COUNTS,
                           'core_safety': {'status': 'PASS', 'tests': 3}, 'device': device,
                           'device_directory_created': True, 'device_directory': '/data/local/tmp/synthetic',
                           'device_serial_sha256': 'd' * 64, 'completed_at_utc': self.now.isoformat(),
                           'admission': {'sha256': inputs.sha(self.admission_path.read_bytes()),
                                         'expires_at_utc': self.admission['expires_at_utc']},
                           'deployment_manifest': manifest, 'input_binding': binding,
                           'oracles': {'image-1920x1080.ppm': {'width': 1920, 'height': 1080,
                                       'sha256': 'e' * 64, 'checksum': 10, 'output_hash_fnv64': 99}}}
        # Synthetic journal with stubbed semantic checkers; these are not device evidence.
        specs = freezer.make_specs()
        for spec in specs:
            if spec['group'] != 'dex':
                self.functional['oracles'].setdefault(spec['input'], {'sha256': 'e' * 64, 'checksum': 10})
        rows = []
        core_results = {}
        for program in freezer.CORE_PROGRAMS:
            checks = {'core_contract': True}
            rows.append({'schema_version': 2, 'scope': 'FUNCTIONAL_NOT_PERFORMANCE', 'group': 'core_safety',
                         'case_id': program, 'success': True, 'native': {}, 'checks': checks,
                         'command_result': {'stdout': '{}', 'stderr': '', 'exit_code': 0, 'timed_out': False, 'launch_error': None}})
            core_results[program] = {'native': {}, 'checks': checks, 'status': 'PASS'}
        self.functional['core_safety']['results'] = core_results
        perf_keys = {'success', 'normal_case', 'schema_workload', 'native_variant', 'input_sha', 'input_size',
                     'provider_layout', 'dimensions', 'complete_frames', 'all_pixel_sum', 'complete_output_digest',
                     'gray_once', 'diagnostic_mode', 'no_phases', 'source_final_release', 'lease_lifetime',
                     'closed_zero', 'final_reserved_zero', 'positive_total', 'payload_model', 'backing_model',
                     'provider_model', 'unknowns_not_zero', 'actual_steady_calls', 'CPU_affinity',
                     'resource_counters_nonnegative_integers'}
        for spec in specs:
            group = spec['group']
            native = {}
            checks = {'contract': True}
            if group == 'dex':
                native = {'selected_cpu': 2, 'cpu_before': 2, 'cpu_after': 2}
                checks['CPU_affinity'] = True
            elif group == 'image_workspace':
                checks['no_output_created' if spec.get('expected_error') else 'complete_PGM_byte_oracle'] = True
            else:
                native = {'success': True, 'diagnostic': spec['capture'], 'provider': spec['provider'],
                          'layout': spec['layout'], 'cpu_requested': 2, 'cpu_before': 2, 'cpu_after': 2}
                checks = {key: True for key in perf_keys}
                if spec['capture']:
                    checks['complete_PGM_byte_oracle'] = True
            row = {'schema_version': 2, 'scope': 'FUNCTIONAL_NOT_PERFORMANCE', 'group': group,
                   'case_id': spec['id'], 'success': True, 'native': native, 'checks': checks,
                   'command_result': {'stdout': json.dumps(native), 'stderr': '',
                                      'exit_code': 1 if spec.get('expected_error') else 0,
                                      'timed_out': False, 'launch_error': None}}
            if (group == 'image_workspace' and not spec.get('expected_error')) or (group == 'image_perf' and spec['capture']):
                row['output_oracle'] = {'actual_pgm_sha256': 'e' * 64, 'expected_pgm_sha256': 'e' * 64}
            rows.append(row)
        self.journal_path = self.file('functional.jsonl', b''.join((json.dumps(row) + '\n').encode() for row in rows))
        self.functional['native_session_rows'] = len(rows)
        for name, checker in (('core_checks', lambda program, stdout: ({}, {'core_contract': True})),
                              ('dex_checks', lambda *args, **kwargs: {'contract': True}),
                              ('image_checks', lambda *args, **kwargs: {'contract': True})):
            mock = patch.object(freezer, name, side_effect=checker)
            mock.start()
            self.addCleanup(mock.stop)
        self.deployment = {'state': 'FUNCTIONAL_STAGED_IDENTITY_ONLY', 'device_serial_sha256': 'd' * 64,
                           'device_directory': '/data/local/tmp/synthetic',
                           'profile': device, 'artifacts': manifest, 'input_binding': binding}
        self.functional_path = self.jsonfile('functional.json', self.functional)
        self.deployment_path = self.jsonfile('deployment.json', self.deployment)
        self.image_spec = {'input': {'source_sha256': inputs.sha(self.image.read_bytes()), 'input_bytes': self.image.stat().st_size,
                                     'width': 1920, 'height': 1080},
                           'completion': {'frames': 3, 'completed_frames': 3, 'result_checksum': 30,
                                          'output_hash_fnv64': 99, 'gray_conversions': 6, 'oracle_pgm_sha256': 'e' * 64}}
        real_regular = inputs.regular
        def regular(path, boundary, maximum=inputs.MAX_FILE):
            if Path(path) == freezer.ROOT / 'bench/image_perf_v2.json':
                return Path(path), json.dumps(self.image_spec).encode()
            return real_regular(path, boundary, maximum)
        self.regular_patch = patch.object(inputs, 'regular', side_effect=regular)
        self.regular_patch.start()
        self.addCleanup(self.regular_patch.stop)
        self.validate_patch = patch.object(inputs, 'validate', return_value=self.profile)
        self.validate_patch.start()
        self.addCleanup(self.validate_patch.stop)
        original_protocol = freezer.default_protocol
        def protocol():
            value = original_protocol()
            value['families']['dex']['semantic_identity'].update(source_sha256=inputs.sha(self.dex.read_bytes()), input_bytes=self.dex.stat().st_size)
            return value
        self.protocol_patch = patch.object(freezer, 'default_protocol', side_effect=protocol)
        self.protocol_patch.start()
        self.addCleanup(self.protocol_patch.stop)

    def file(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return path

    def jsonfile(self, name, data):
        return self.file(name, json.dumps(data).encode())

    def run_freeze(self):
        return freezer.freeze(self.profile_path, self.profile_sha, self.functional_path, self.deployment_path,
                              self.admission_path, self.dex, self.image, self.root / 'frozen', now=self.now)

    def save_functional(self):
        self.jsonfile('functional.json', self.functional)

    def test_complete_synthetic_batch_makes_exact_two_family_plan(self):
        result = self.run_freeze()
        plan = json.loads((self.root / 'frozen/plan.json').read_text())
        protocol = json.loads((self.root / 'frozen/protocol.json').read_text())
        self.assertFalse(result['performance_qualified'])
        self.assertEqual(len(plan['entries']), 512)
        self.assertEqual({x['family'] for x in plan['entries']}, {'dex', 'image'})
        self.assertEqual(sum(x['phase'] == 'pilot' for x in plan['entries']), 128)
        self.assertEqual(freezer.freeze_errors(protocol, plan), [])
        self.assertIn({'path': inputs.relative(self.functional_path), 'sha256': inputs.sha(self.functional_path.read_bytes())},
                      protocol['freeze']['source_closure'])
        self.assertEqual({x['role'] for x in protocol['freeze']['artifact_closure']},
                         {'library', 'dex_formal', 'image_formal', 'dex_input', 'image_input'})

    def test_missing_profile_binding_is_rejected(self):
        del self.functional['input_binding']
        self.save_functional()
        with self.assertRaises(ValueError):
            self.run_freeze()
        self.assertFalse((self.root / 'frozen').exists())

    def test_missing_or_forged_case_evidence_is_rejected(self):
        original = self.journal_path.read_bytes()
        rows = original.splitlines()
        for altered in (b'\n'.join(rows[:-1]) + b'\n',
                        original.replace(b'"success": true', b'"success": false', 1),
                        original.replace(b'"case_id": "mgmem_core_tests"', b'"case_id": "invented"', 1)):
            self.journal_path.write_bytes(altered)
            with self.assertRaises(ValueError):
                self.run_freeze()
        self.journal_path.write_bytes(original)
        self.functional['core_safety']['results'] = {}
        self.save_functional()
        with self.assertRaises(ValueError):
            self.run_freeze()

    def test_missing_journal_rejects_summary_only_fake_pass(self):
        self.journal_path.unlink()
        with self.assertRaises((ValueError, OSError)):
            self.run_freeze()

    def test_wrong_device_deployment_is_rejected(self):
        self.deployment['device_serial_sha256'] = 'f' * 64
        self.jsonfile('deployment.json', self.deployment)
        with self.assertRaises(ValueError):
            self.run_freeze()

    def test_mutated_input_is_rejected(self):
        self.dex.write_bytes(b'drift')
        with self.assertRaises(ValueError):
            self.run_freeze()

    def test_missing_runtime_source_is_rejected(self):
        self.functional['input_binding']['runtime_sources'].pop(next(iter(self.functional['input_binding']['runtime_sources'])))
        self.deployment['input_binding'] = self.functional['input_binding']
        self.save_functional()
        self.jsonfile('deployment.json', self.deployment)
        with self.assertRaises(ValueError):
            self.run_freeze()

    def test_private_hardlink_is_rejected(self):
        link = self.root / 'hardlink.dex'
        try:
            os.link(self.dex, link)
        except OSError:
            self.skipTest('hardlinks unavailable')
        with self.assertRaises(ValueError):
            freezer.private_file(self.dex)

    def test_manifest_device_hash_is_required(self):
        self.functional['deployment_manifest'][0]['device_sha256'] = 'f' * 64
        self.deployment['artifacts'] = self.functional['deployment_manifest']
        self.save_functional()
        self.jsonfile('deployment.json', self.deployment)
        with self.assertRaises(ValueError):
            self.run_freeze()

    def test_stale_functional_receipt_is_rejected(self):
        self.functional['completed_at_utc'] = (self.now - timedelta(seconds=1801)).isoformat()
        self.save_functional()
        with self.assertRaises(ValueError):
            self.run_freeze()

    def test_duplicate_deployment_name_is_rejected(self):
        self.functional['deployment_manifest'].append(dict(self.functional['deployment_manifest'][0]))
        self.deployment['artifacts'] = self.functional['deployment_manifest']
        self.save_functional()
        self.jsonfile('deployment.json', self.deployment)
        with self.assertRaises(ValueError):
            self.run_freeze()

    def test_collector_boundary_rejects_changed_functional_receipt(self):
        self.run_freeze()
        protocol_path = self.root / 'frozen/protocol.json'
        plan_path = self.root / 'frozen/plan.json'
        protocol = json.loads(protocol_path.read_text())
        capture = CaptureV2.__new__(CaptureV2)
        capture.protocol = protocol
        capture.admission_deadline = self.now + timedelta(hours=1)
        capture.protocol_path, capture.plan_path = protocol_path, plan_path
        capture.protocol_file_sha, capture.plan_file_sha = digest(protocol_path), digest(plan_path)
        capture.args = SimpleNamespace(admission=self.admission_path)
        capture.admission_file_sha = digest(self.admission_path)
        capture.pilot_evidence_hashes = {}
        item = next(item for item in protocol['freeze']['source_closure']
                    if item['path'] == inputs.relative(self.functional_path))
        capture.source_file_hashes = {self.functional_path: item['sha256']}
        capture.check_immutable(include_source=True)
        self.functional_path.write_bytes(b'changed')
        with self.assertRaises(Blocked):
            capture.check_immutable(include_source=True)

    def test_image_oracle_is_required(self):
        self.functional['oracles']['image-1920x1080.ppm']['sha256'] = 'f' * 64
        self.save_functional()
        with self.assertRaises(ValueError):
            self.run_freeze()

    def test_same_bytes_different_input_path_is_rejected(self):
        other = self.file('other.dex', self.dex.read_bytes())
        with self.assertRaises(ValueError):
            freezer.freeze(self.profile_path, self.profile_sha, self.functional_path, self.deployment_path,
                           self.admission_path, other, self.image, self.root / 'frozen', now=self.now)

    def test_symlink_is_rejected(self):
        link = self.root / 'symlink.dex'
        try:
            link.symlink_to(self.dex)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(ValueError):
            freezer.private_file(link)


class CurrentProducerCompatibilityTest(unittest.TestCase):
    def test_journal_generated_by_current_producer_is_accepted(self):
        # Historical native outputs are replayed through the current producer;
        # this is a synthetic format test, not another device execution.
        source = freezer.ROOT / 'artifacts/device-v2-20261030/new-device-functional'
        old = json.loads((source / 'functional-receipt.json').read_text(encoding='utf-8'))
        old_rows = [json.loads(line) for line in (source / 'functional.jsonl').read_text(encoding='utf-8').splitlines()]
        rows = {row['case_id']: row for row in old_rows}
        with tempfile.TemporaryDirectory(prefix='test-current-journal-', dir=inputs.build_root()) as directory:
            root = Path(directory)
            runner = producer.ConsumerRunner.__new__(producer.ConsumerRunner)
            runner.root_output, runner.phase, runner.remote = root, 'functional', '/synthetic'
            runner.args = SimpleNamespace(cpu=2, task_timeout=30, serial='SYNTHETIC-SERIAL', input=root / 'input.dex')
            runner.input_profile = root / 'synthetic-profile'
            runner.oracles = old['oracles']
            runner.assets = {}
            image = root / 'image.ppm'
            perf = rows['perf-direct-full-formal']['native']
            with image.open('wb') as stream:
                stream.truncate(perf['input_bytes'])
            runner.assets['image-1920x1080.ppm'] = image
            runner.receipt = {'core_safety': {'status': 'NOT_RUN', 'tests': 0, 'results': {}},
                              'native_session_rows': 0, 'completed_cases': {}, 'oracles': runner.oracles}
            runner.allowed = {}
            def command(name, *args, **kwargs):
                if name.endswith('-no-output'):
                    return {'exit_code': 0, 'timed_out': False}
                case = name[5:] if name.startswith('core-') else name[:-7]
                return rows[case]['command_result']
            def verify(case_id, remote, expected):
                return {'actual_pgm_sha256': expected, 'expected_pgm_sha256': expected}
            with patch.object(runner, 'command', side_effect=command), patch.object(runner, 'verify_pgm', side_effect=verify), \
                 patch.object(runner, 'save'), patch.object(runner, 'admission_gate'), patch.object(runner, 'occupancy'), \
                 patch.object(runner, 'boot_gate'), patch.object(producer, 'digest', return_value=perf['source_sha256']):
                runner.core_safety()
                for spec in producer.make_specs():
                    runner.one(spec)
            self.assertEqual(runner.receipt['native_session_rows'], 199)
            path, content = freezer.validate_functional_evidence(root / 'functional-receipt.json', runner.receipt)
            self.assertEqual(path, root / 'functional.jsonl')
            self.assertTrue(content)


if __name__ == '__main__':
    unittest.main()
