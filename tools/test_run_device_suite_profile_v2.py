#!/usr/bin/env python3
"""Synthetic offline checks for the profile-aware capture gate; no ADB."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import freeze_arm64_input_profile as inputs
import run_device_suite_profile_v2 as runner
import test_freeze_device_suite_profile_v2 as fixtures
from paired_suite_v2 import make_plan


class ProfileCaptureGateTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProfileFreezeTest('test_complete_synthetic_batch_makes_exact_two_family_plan')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.run_freeze()
        self.protocol = self.fixture.root / 'frozen/protocol.json'
        self.plan = self.fixture.root / 'frozen/plan.json'
        self.admission = self.fixture.admission_path
        self.tool = self.fixture.tool
        original = inputs.external_file
        def external(path):
            if Path(path) == Path(runner.sys.executable).absolute():
                return inputs.record(self.tool.read_bytes())
            return original(path)
        self.external = patch.object(inputs, 'external_file', side_effect=external)
        self.external.start()
        self.addCleanup(self.external.stop)
        self.baseline = patch.object(runner, 'default_protocol', side_effect=runner.freezer.default_protocol)
        self.baseline.start()
        self.addCleanup(self.baseline.stop)

    def verify(self, admission=None, adb=None):
        return runner.preflight(self.protocol, self.plan, admission or self.admission, adb or self.tool)

    def test_same_batch_is_accepted_without_device(self):
        self.assertEqual(self.verify()['admission_sha256'], inputs.sha(self.admission.read_bytes()))

    def test_other_valid_admission_is_rejected(self):
        other = self.fixture.root / 'other-admission.json'
        value = dict(self.fixture.admission, owner_evidence='different owner release')
        other.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'admission_not_frozen_identity'):
            self.verify(admission=other)

    def test_mutated_functional_receipt_is_rejected(self):
        value = json.loads(self.fixture.functional_path.read_text())
        value['scope'] = 'PASS_CAPTURE_ONLY'
        self.fixture.functional_path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'functional_identity_mismatch'):
            self.verify()

    def test_altered_frozen_source_is_rejected(self):
        self.fixture.tools_path.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.verify()

    def test_changed_source_closure_rejected_even_with_regenerated_plan(self):
        protocol = json.loads(self.protocol.read_text())
        protocol['freeze']['source_closure'][0]['sha256'] = 'f' * 64
        self.protocol.write_text(json.dumps(protocol), encoding='utf-8')
        self.plan.write_text(json.dumps(make_plan(protocol)), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'source_closure_mismatch'):
            self.verify()

    def test_changed_boot_rejected_even_with_regenerated_plan(self):
        protocol = json.loads(self.protocol.read_text())
        protocol['freeze']['device_profile']['boot_id_sha256'] = 'f' * 64
        self.protocol.write_text(json.dumps(protocol), encoding='utf-8')
        self.plan.write_text(json.dumps(make_plan(protocol)), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'device_boot_or_admission_mismatch'):
            self.verify()

    def test_changed_role_rejected_even_with_regenerated_plan(self):
        protocol = json.loads(self.protocol.read_text())
        protocol['freeze']['artifact_closure'][0]['role'] = 'other'
        self.protocol.write_text(json.dumps(protocol), encoding='utf-8')
        self.plan.write_text(json.dumps(make_plan(protocol)), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'artifact_roles_invalid'):
            self.verify()

    def test_adb_change_is_rejected(self):
        other = self.fixture.root / 'other-tool'
        other.write_bytes(b'other adb')
        with self.assertRaisesRegex(ValueError, 'capture_runtime_differs_from_functional'):
            self.verify(adb=other)

    def test_invalid_private_path_is_rejected(self):
        with self.assertRaises(ValueError):
            runner.relative_path('../outside')
        with self.assertRaises(ValueError):
            runner.relative_path('C:/outside')

    def test_changed_workload_rejected_even_with_regenerated_plan(self):
        protocol = json.loads(self.protocol.read_text())
        protocol['families']['dex']['variants']['D-C0']['argv'][-1] = '3'
        self.protocol.write_text(json.dumps(protocol), encoding='utf-8')
        self.plan.write_text(json.dumps(make_plan(protocol)), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'family_semantics_changed'):
            self.verify()

    def test_changed_sampling_rejected_even_with_regenerated_plan(self):
        protocol = json.loads(self.protocol.read_text())
        protocol['sampling']['precision_target_log_half_width'] = .5
        self.protocol.write_text(json.dumps(protocol), encoding='utf-8')
        self.plan.write_text(json.dumps(make_plan(protocol)), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'sampling_or_qualification_changed'):
            self.verify()

    def test_output_outside_private_build_rejected_before_collector(self):
        argv = ['run', '--adb', str(self.tool), '--serial', 'synthetic', '--admission', str(self.admission),
                '--protocol', str(self.protocol), '--plan', str(self.plan), '--phase', 'stage',
                '--out-dir', str(runner.ROOT / 'not-private')]
        with patch.object(runner.sys, 'argv', argv), patch.object(runner, 'ProfileCapture') as capture:
            self.assertEqual(runner.main(), 2)
            capture.assert_not_called()

    def test_check_immutable_rejects_different_frozen_admission(self):
        # No runner initialization or device is involved in this negative case.
        subject = object.__new__(runner.ProfileCapture)
        subject.protocol = {}
        subject.expected_profile = {'admission_sha256': 'expected', 'protocol_sha256': 'p', 'plan_sha256': 'q'}
        subject.admission_file_sha = 'different'
        subject.protocol_file_sha, subject.plan_file_sha = 'p', 'q'
        with patch.object(runner.CaptureV2, 'check_immutable'):
            with self.assertRaisesRegex(ValueError, 'capture_frozen_identity_changed'):
                subject.check_immutable()

    def test_local_deployment_validates_all_identity_fields(self):
        protocol = {'freeze': {'device_profile': {'boot_id_sha256': 'b' * 64}}}
        artifacts = [{'role': 'library', 'sha256': 'c' * 64}]
        deployment = {'state': 'STAGED', 'device_serial_sha256': 'a' * 64,
                      'boot_id_sha256': 'b' * 64, 'protocol_canonical_sha256': runner.object_sha(protocol),
                      'artifacts': artifacts, 'device_directory': '/data/local/tmp/meguvizmemory-v2-20261101T010203Z-abcdef123456'}
        runner.validate_deployment(deployment, 'a' * 64, 'b' * 64, protocol, artifacts)
        for key in deployment:
            with self.subTest(key=key):
                changed = dict(deployment, **{key: 'wrong'})
                with self.assertRaisesRegex(ValueError, 'deployment_identity_mismatch'):
                    runner.validate_deployment(changed, 'a' * 64, 'b' * 64, protocol, artifacts)

    def test_bad_deployment_fails_validate_before_any_adb(self):
        subject = object.__new__(runner.ProfileCapture)
        subject.phase = 'pilot'
        subject.protocol_path, subject.plan_path = self.protocol, self.plan
        subject.args = SimpleNamespace(admission=self.admission, adb=self.tool)
        subject.expected_profile = {'serial_sha256': 'a' * 64, 'admission_sha256': 'b' * 64}
        subject.receipt = {'device_serial_sha256': 'a' * 64}
        subject.admission_file_sha = 'b' * 64
        subject.protocol = {'freeze': {'device_profile': {'boot_id_sha256': 'c' * 64}}}
        subject.deployment_path = self.fixture.root / 'bad-deployment.json'
        subject.artifacts = {}
        with patch.object(runner.CaptureV2, 'validate'), \
             patch.object(runner, 'preflight', return_value=subject.expected_profile), \
             patch.object(runner, 'document', return_value=(subject.deployment_path, b'{}', {})), \
             patch.object(runner.CaptureV2, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'deployment_identity_mismatch'):
                subject.validate()
            command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
