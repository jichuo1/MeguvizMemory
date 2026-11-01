#!/usr/bin/env python3
"""Pure fixture tests for stability observations; never invoke ADB or a device."""
import copy
from argparse import Namespace
import json
from pathlib import Path
import secrets
import unittest
from unittest import mock

import android_managed_stability_round as subject

RUN_ID = '0123456789abcdef'
PID = 1234
DURATION = 900000
PACKAGE = 'io.github.jichuo1.meguviz.memory.stabilityprobe'
ACTIVITY = 'io.github.jichuo1.meguviz.memory.android.PressureR8ForegroundActivity'
COMPONENT = PACKAGE + '/' + ACTIVITY


def sample(status='RUNNING', **overrides):
    result = {
        'suite': 'android-managed-stability-v1', 'status': status,
        'run_id': RUN_ID, 'pid': PID, 'sequence': 20,
        'phase': 'system_pressure', 'iteration': 1, 'elapsed_ms': 1200,
        'workload_elapsed_ms': 1100, 'snapshot_consistency': 'non_atomic',
        'foreground_confirmed': True, 'reason': 'none',
        'pressure_origin': 'synthetic', 'performance_qualified': False,
    }
    if status == 'PASS':
        result.update(phase='complete', iteration=2, elapsed_ms=900100,
                      workload_elapsed_ms=900000)
    elif status == 'FAIL':
        result['reason'] = 'operation_failed'
    elif status == 'INCOMPLETE':
        result['reason'] = 'phase_timeout_unknown'
    result.update(overrides)
    return result


def encode(value):
    return json.dumps(value, separators=(',', ':'))


def instrumentation(value, code):
    return 'INSTRUMENTATION_RESULT: report_json=' + encode(value) + '\nINSTRUMENTATION_CODE: ' + str(code) + '\n'


class ResumedTests(unittest.TestCase):
    def test_own_and_other_resumed(self):
        self.assertIs(subject.parse_resumed('mResumedActivity: ActivityRecord{abc u0 ' + COMPONENT + ' t7}\n'), True)
        self.assertIs(subject.parse_resumed('mResumedActivity: ActivityRecord{abc u0 other.app/.Activity t7}\n'), False)

    def test_unparseable_and_conflicting_are_not_accepted(self):
        for text in ('', 'Permission Denial\n', 'mResumedActivity: null\n',
                     'mResumedActivity: ActivityRecord{abc u0 ' + COMPONENT + ' t7}\n'
                     'mResumedActivity: ActivityRecord{def u0 other.app/.Activity t8}\n'):
            with self.subTest(text=text), self.assertRaises(subject.Blocked):
                subject.parse_resumed(text)


class ProgressTests(unittest.TestCase):
    def test_all_statuses_with_exact_identity(self):
        for status in ('RUNNING', 'PASS', 'FAIL', 'INCOMPLETE'):
            with self.subTest(status=status):
                value = sample(status)
                self.assertEqual(subject.parse_progress(encode(value), RUN_ID, PID), value)

    def test_rejects_missing_duplicate_extra_or_malformed(self):
        valid = sample()
        missing = dict(valid); del missing['snapshot_consistency']
        extra = dict(valid, unexpected=1)
        invalids = [encode(missing), encode(extra), '{',
                    encode(valid).replace('"sequence":20', '"sequence":20,"sequence":20'),
                    encode(valid).replace('"sequence":20', '"sequence":NaN')]
        for text in invalids:
            with self.subTest(text=text), self.assertRaises(subject.Blocked):
                subject.parse_progress(text, RUN_ID, PID)

    def test_rejects_identity_type_and_scope_mismatch(self):
        for changed in (sample(run_id='1111111111111111'), sample(pid=999), sample(pid=True),
                        sample(sequence=True), sample(iteration=-1), sample(elapsed_ms=-1),
                        sample(workload_elapsed_ms=-1), sample(suite='other'),
                        sample(snapshot_consistency='atomic'), sample(foreground_confirmed='true'),
                        sample(pressure_origin='system'), sample(performance_qualified=True)):
            with self.subTest(changed=changed), self.assertRaises(subject.Blocked):
                subject.parse_progress(encode(changed), RUN_ID, PID)


class ReportTests(unittest.TestCase):
    def test_terminal_results_and_codes(self):
        for status, code in (('PASS', 0), ('FAIL', 1), ('INCOMPLETE', 1)):
            with self.subTest(status=status):
                value = sample(status)
                self.assertEqual(subject.parse_report(instrumentation(value, code), 0, RUN_ID, PID, DURATION), value)

    def test_foreground_cleanup_unknown_stays_incomplete(self):
        value = sample('INCOMPLETE', reason='foreground_cleanup_unknown')
        self.assertEqual(subject.parse_report(instrumentation(value, 1), 0, RUN_ID, PID, DURATION), value)
        self.assertEqual(subject.classify_observation(value, None, RUN_ID, PID, DURATION), 'INCOMPLETE')

    def test_invalid_transport_output_and_incomplete_duration(self):
        good = instrumentation(sample('PASS'), 0)
        invalids = [good + good, good[:-1], instrumentation(sample('PASS'), 1),
                    instrumentation(sample('FAIL'), 0),
                    good.replace('INSTRUMENTATION_CODE: 0', 'INSTRUMENTATION_FAILED: crashed\nINSTRUMENTATION_CODE: 0'),
                    instrumentation(sample('PASS', workload_elapsed_ms=899999), 0),
                    instrumentation(sample('PASS', phase='system_pressure'), 0),
                    instrumentation(sample('PASS', iteration=0), 0),
                    instrumentation(sample('PASS', foreground_confirmed=False), 0)]
        for text in invalids:
            with self.subTest(text=text), self.assertRaises(subject.Blocked):
                subject.parse_report(text, 0, RUN_ID, PID, DURATION)
        with self.assertRaises(subject.Blocked):
            subject.parse_report(good, 1, RUN_ID, PID, DURATION)


class ObservationTests(unittest.TestCase):
    def test_running_and_pass(self):
        self.assertEqual(subject.classify_observation(sample(), None, RUN_ID, PID, DURATION), 'RUNNING')
        self.assertEqual(subject.classify_observation(sample('PASS'), None, RUN_ID, PID, DURATION), 'PASS')
        self.assertEqual(subject.classify_observation(sample('FAIL'), None, RUN_ID, PID, DURATION), 'FAIL')
        self.assertEqual(subject.classify_observation(sample('INCOMPLETE'), None, RUN_ID, PID, DURATION), 'INCOMPLETE')

    def test_stall_wins_even_against_later_pass(self):
        stall = sample('INCOMPLETE', sequence=19)
        self.assertEqual(subject.classify_observation(sample('PASS', sequence=21), stall, RUN_ID, PID, DURATION), 'INCOMPLETE')
        self.assertEqual(subject.classify_observation(None, stall, RUN_ID, PID, DURATION), 'INCOMPLETE')

    def test_short_duration_and_unproved_identity_are_not_pass(self):
        for value in (sample('PASS', workload_elapsed_ms=899999), sample('PASS', foreground_confirmed=False),
                      sample('PASS', run_id='1111111111111111'), sample('PASS', pid=999)):
            with self.subTest(value=value):
                self.assertEqual(subject.classify_observation(value, None, RUN_ID, PID, DURATION), 'INCOMPLETE')
        self.assertEqual(subject.classify_observation(None, None, RUN_ID, PID, DURATION), 'INCOMPLETE')


class CoverageTests(unittest.TestCase):
    def make_samples(self):
        return [{'begin': float(i), 'end': float(i), 'mode': '0', 'frozen': '0'} for i in range(5, 901, 5)]

    def test_full_discrete_coverage_is_not_continuous_proof(self):
        result = subject.verify_observations(self.make_samples(), 0.0, 901.0, DURATION)
        self.assertEqual(result['count'], 180)
        self.assertEqual(result['freezer_observation'], 'DISCRETE_SAMPLES_UNFROZEN_NOT_CONTINUOUS')

    def test_missing_samples_large_gaps_and_short_window_fail(self):
        samples = self.make_samples()
        for records, start, end in (([], 0, 901), (samples[:1], 0, 901),
                                    (samples[8:], 0, 901), (samples[:-8], 0, 901),
                                    (samples[:30] + samples[37:], 0, 901),
                                    (samples, 0, 899), (samples[::-1], 0, 901)):
            with self.subTest(count=len(records), end=end), self.assertRaises(subject.Blocked):
                subject.verify_observations(records, start, end, DURATION)

    def test_freeze_evidence_cannot_be_promoted_to_pass(self):
        records = self.make_samples()
        records[42] = dict(records[42], frozen='1')
        with self.assertRaises(subject.Blocked):
            subject.verify_observations(records, 0, 901, DURATION)

    def test_unequal_sampling_delays_use_worst_gap(self):
        records = [r for r in self.make_samples() if r['begin'] not in (205, 210, 215, 225, 230)]
        record = next(r for r in records if r['begin'] == 220)
        record['end'] = 231
        # No overlap; the worst possible actual freezer gap is 31 seconds.
        with self.assertRaisesRegex(subject.Blocked, 'observation_coverage_insufficient'):
            subject.verify_observations(records, 0, 901, DURATION)


class OwnLauncherGateTests(unittest.TestCase):
    """Offline reproduction of the app_process self-occupancy false positive."""
    def setUp(self):
        self.plan = {'run_id': RUN_ID, 'duration_ms': DURATION, 'max_rounds': 10000,
                     'interval_ms': 250, 'phase_timeout_ms': 30000}
        self.argv = ['app_process', '/system/bin', 'com.android.commands.am.Am',
                     *subject.instrumentation_arguments(self.plan, PID, '0')]
        self.own_line = '4321 app_process ' + ' '.join(self.argv)
        self.ps = ('PID NAME ARGS\n1 init init\n2 system_server system_server\n'
                   '3 zygote zygote\n4 surfaceflinger surfaceflinger\n' +
                   str(PID) + ' ' + PACKAGE + ' ' + PACKAGE + '\n' + self.own_line + '\n')
        self.activity = ('ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)\n'
                         'ACTIVITY MANAGER RUNNING PROCESSES (dumpsys activity processes)\n'
                         '  Instrumentation #0: ActiveInstrumentation{abc123 {' + subject.COMPONENT + '} 1 procs}\n')
        self.uid_status = 'Name:\tapp_process\nUid:\t2000\t2000\t2000\t2000\n'
        self.stat_before = '4321 (app_process) S ' + ' '.join(['0'] * 18 + ['777'])
        self.stat_after = self.stat_before
        self.cmdline = '\x00'.join(self.argv) + '\x00'
        self.device = object.__new__(subject.Device)
        self.device.receipt = {'gates': []}
        self.device.launcher_context = (str(PID), tuple(self.argv), '2000')
        self.device.command = mock.Mock(side_effect=self.command)

    def command(self, label, args, **kwargs):
        if label.endswith('-processes'): return 0, self.ps, ''
        if label.endswith('-activity'): return 0, self.activity, ''
        if label == 'own-launcher-status': return 0, self.uid_status, ''
        if label == 'own-launcher-stat-before': return 0, self.stat_before, ''
        if label == 'own-launcher-stat-after': return 0, self.stat_after, ''
        if label == 'own-launcher-cmdline': return 0, self.cmdline, ''
        raise AssertionError('unexpected command')

    def gate(self):
        self.device.gate('during-instrumentation', own_pid=PID, own_instrument=True)

    def test_exact_single_own_launcher_passes(self):
        self.gate()
        result = self.device.receipt['gates'][-1]
        self.assertEqual(result['result'], 'PASS_OBSERVATIONAL_ONLY')
        self.assertEqual(result['own_launcher_count'], 1)
        self.assertEqual(result['matching_other_tests'], 0)
        calls = [call.args[0] for call in self.device.command.call_args_list]
        self.assertLess(calls.index('own-launcher-stat-before'), calls.index('own-launcher-status'))
        self.assertLess(calls.index('own-launcher-status'), calls.index('own-launcher-stat-after'))

    def test_unexpected_or_forged_arguments_block(self):
        substitutions = [
            ('--user 0', '--user 1'), (RUN_ID, '1111111111111111'),
            ('expected_pid ' + str(PID), 'expected_pid 4322'),
            ('duration_ms 900000', 'duration_ms 900001'),
            ('max_rounds 10000', 'max_rounds 9999'),
            ('interval_ms 250', 'interval_ms 251'),
            ('phase_timeout_ms 30000', 'phase_timeout_ms 30001'),
            (subject.COMPONENT, subject.COMPONENT + 'Impostor'),
            ('--no-restart', '--no-restart --extra'),
            ('4321 app_process ', '4321 spoof_app_process '),
        ]
        for before, after in substitutions:
            with self.subTest(before=before):
                self.setUp()
                self.ps = self.ps.replace(self.own_line, self.own_line.replace(before, after))
                with self.assertRaises(subject.Blocked): self.gate()

    def test_duplicate_own_launchers_block(self):
        self.ps += self.own_line.replace('4321 ', '4322 ', 1) + '\n'
        with self.assertRaisesRegex(subject.Blocked, 'own_launcher_ambiguous'): self.gate()

    def test_wrong_uid_proc_cmdline_or_reused_pid_blocks(self):
        for attribute, value in (
            ('uid_status', 'Uid:\t2001\t2001\t2001\t2001\n'),
            ('cmdline', '\x00'.join(self.argv[:-1]) + '\x00'),
            ('stat_after', self.stat_before.rsplit(' ', 1)[0] + ' 778'),
        ):
            with self.subTest(attribute=attribute):
                self.setUp()
                setattr(self, attribute, value)
                with self.assertRaises(subject.Blocked): self.gate()

    def test_other_or_spoofed_instrumentation_blocks(self):
        for extra in (
            '  Instrumentation #1: ActiveInstrumentation{def123 {other.app/other.Runner} 1 procs}\n',
            '  Instrumentation #1: ActiveInstrumentation{def123 {' + subject.COMPONENT + 'Fake} 1 procs}\n',
            '  Instrumentation #1: ActiveInstrumentation{def123 {' + subject.COMPONENT + '} 1 procs}\n',
        ):
            with self.subTest(extra=extra):
                self.setUp(); self.activity += extra
                with self.assertRaises(subject.Blocked): self.gate()

    def test_context_is_required_and_not_valid_for_other_gate(self):
        del self.device.launcher_context
        with self.assertRaisesRegex(subject.Blocked, 'own_launcher_context_invalid'): self.gate()
        self.setUp()
        with self.assertRaisesRegex(subject.Blocked, 'own_launcher_context_invalid'):
            self.device.gate('before-instrumentation', own_pid=PID, own_instrument=True)

    def test_missing_launcher_is_not_assumed_to_be_a_lock(self):
        self.ps = self.ps.replace(self.own_line + '\n', '')
        # A component-only activity record is not enough to identify this run.
        with self.assertRaises(subject.Blocked): self.gate()
        self.activity = ('ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)\n'
                         'ACTIVITY MANAGER RUNNING PROCESSES (dumpsys activity processes)\n')
        self.gate()
        self.assertEqual(self.device.receipt['gates'][-1]['own_launcher_count'], 0)

    def test_target_identity_fences_uid_between_start_time_reads(self):
        before = str(PID) + ' (' + PACKAGE + ') S ' + ' '.join(['0'] * 18 + ['777'])
        after = before.rsplit(' ', 1)[0] + ' 778'
        labels = []
        def command(label, args, **kwargs):
            labels.append(label)
            if label == 'own-process-stat-before': return 0, before, ''
            if label == 'own-process-status': return 0, 'Uid:\t10001\t10001\t10001\t10001\n', ''
            if label == 'own-process-stat-after': return 0, after, ''
            raise AssertionError('unexpected command')
        self.device.command = mock.Mock(side_effect=command)
        with self.assertRaisesRegex(subject.Blocked, 'own_process_identity_unavailable'):
            self.device.process_identity(str(PID), '10001')
        self.assertEqual(labels, ['own-process-stat-before', 'own-process-status', 'own-process-stat-after'])


class DeviceObservationTests(unittest.TestCase):
    def setUp(self):
        self.device = mock.Mock()
        self.device.receipt = {}
        self.device.pid.return_value = str(PID)
        self.device.process_identity.return_value = 'same-identity'
        self.device.command.return_value = (0, 'mResumedActivity: ActivityRecord{abc u0 ' + COMPONENT + ' t7}\n', '')
        self.device.freeze_sample.return_value = {'mode': '0', 'frozen': '0'}
        self.device.read_record.side_effect = lambda kind, run_id: None if kind == 'stall' else encode(sample())

    def observe(self):
        return subject.observe_once(self.device, RUN_ID, PID, '123', 'same-identity', DURATION)

    def test_checked_foreground_and_identity(self):
        progress, freeze = self.observe()
        self.assertEqual(progress['status'], 'RUNNING')
        self.assertEqual(freeze['frozen'], '0')
        self.device.gate.assert_called_once_with('during-instrumentation', own_pid=PID, own_instrument=True)

    def test_pid_replacement_and_identity_change_are_incomplete(self):
        for attr, value in (('pid', '987'), ('process_identity', 'changed')):
            self.setUp()
            getattr(self.device, attr).return_value = value
            with self.subTest(attr=attr), self.assertRaises(subject.Blocked): self.observe()

    def test_stall_takes_precedence_over_later_pass(self):
        self.device.read_record.side_effect = lambda kind, run_id: encode(sample('INCOMPLETE')) if kind == 'stall' else encode(sample('PASS'))
        with self.assertRaises(subject.Blocked): self.observe()

    def test_observed_freeze_is_recorded_before_progress_reads(self):
        self.device.freeze_sample.return_value = {'mode': '1', 'frozen': '1'}
        with self.assertRaisesRegex(subject.Blocked, 'own_cgroup_frozen'): self.observe()
        self.assertEqual(self.device.receipt['freezer_observation'], 'FROZEN_OBSERVED')
        self.device.read_record.assert_not_called()

    def test_other_test_or_unconfirmed_foreground_blocks(self):
        self.device.gate.side_effect = subject.Blocked('other_test')
        with self.assertRaises(subject.Blocked): self.observe()
        self.setUp()
        self.device.command.return_value = (0, 'mResumedActivity: ActivityRecord{abc u0 other.app/.Activity t7}\n', '')
        with self.assertRaises(subject.Blocked): self.observe()


class MonitorTests(unittest.TestCase):
    class Clock:
        def __init__(self): self.now = 0.0
        def monotonic(self): return self.now
        def sleep(self, seconds): self.now += seconds

    def setUp(self):
        self.clock = self.Clock()
        self.device = mock.Mock()
        self.device.receipt = {}
        self.child = mock.Mock()
        self.plan = {'run_id': RUN_ID, 'duration_ms': DURATION, 'outer_seconds': 1020, 'poll_seconds': 5}
        self.child.poll.side_effect = lambda: 0 if self.clock.now >= 900 else None

    def observation(self, *args):
        return sample(), {'mode': '0', 'frozen': '0', 'sample_started_monotonic': self.clock.now,
                          'sample_finished_monotonic': self.clock.now}

    def test_success_requires_later_coverage_review(self):
        with mock.patch.object(subject, 'observe_once', side_effect=self.observation):
            samples, end = subject.monitor(self.child, self.device, self.plan, str(PID), '123', 'identity', 0,
                                           self.clock.monotonic, self.clock.sleep)
        self.assertEqual(end, 900)
        self.assertEqual(subject.verify_observations(samples, 0, end, DURATION)['count'], 180)
        self.assertIsNone(self.device.deadline)

    def test_timeout_and_observation_error_do_not_pass(self):
        self.child.poll.return_value = None; self.child.poll.side_effect = None
        self.plan['outer_seconds'] = 10
        with mock.patch.object(subject, 'observe_once', side_effect=self.observation):
            with self.assertRaises(subject.Blocked):
                subject.monitor(self.child, self.device, self.plan, str(PID), '123', 'identity', 0,
                                self.clock.monotonic, self.clock.sleep)
        self.clock.now = 0
        with mock.patch.object(subject, 'observe_once', side_effect=subject.Blocked('own_stall_reported')):
            with self.assertRaises(subject.Blocked):
                subject.monitor(self.child, self.device, self.plan, str(PID), '123', 'identity', 0,
                                self.clock.monotonic, self.clock.sleep)

    def test_progress_sequence_regression_is_rejected(self):
        values = [(sample(sequence=n), {'mode': '0', 'frozen': '0', 'sample_started_monotonic': (2-n)*5,
                  'sample_finished_monotonic': (2-n)*5}) for n in (2, 1)]
        with mock.patch.object(subject, 'observe_once', side_effect=values):
            with self.assertRaises(subject.Blocked):
                subject.monitor(self.child, self.device, self.plan, str(PID), '123', 'identity', 0,
                                self.clock.monotonic, self.clock.sleep)

    def test_frozen_sample_is_preserved_as_frozen_observed(self):
        def frozen(*args):
            progress, reading = self.observation()
            reading['frozen'] = '1'
            return progress, reading
        with mock.patch.object(subject, 'observe_once', side_effect=frozen):
            with self.assertRaisesRegex(subject.Blocked, 'own_cgroup_frozen'):
                subject.monitor(self.child, self.device, self.plan, str(PID), '123', 'identity', 0,
                                self.clock.monotonic, self.clock.sleep)
        self.assertEqual(self.device.receipt['freezer_observation'], 'FROZEN_OBSERVED')

    def test_activity_finish_handoff_requires_complete_phase(self):
        self.clock.now = 5
        self.child.poll.side_effect = lambda: 0 if self.clock.now >= 12 else None
        self.device.pid.return_value = str(PID)
        self.device.process_identity.return_value = 'identity'
        self.device.read_record.side_effect = lambda kind, run_id: None if kind == 'stall' else encode(sample(phase='complete'))
        with mock.patch.object(subject, 'observe_once', side_effect=[self.observation(), subject.Blocked('own_activity_not_resumed')]):
            samples, end = subject.monitor(self.child, self.device, self.plan, str(PID), '123', 'identity', 0,
                                           self.clock.monotonic, self.clock.sleep)
        self.assertEqual(len(samples), 1)
        self.assertGreaterEqual(end, 12)
        self.clock.now = 5
        self.device.read_record.side_effect = lambda kind, run_id: None if kind == 'stall' else encode(sample())
        with mock.patch.object(subject, 'observe_once', side_effect=[self.observation(), subject.Blocked('own_activity_not_resumed')]):
            with self.assertRaisesRegex(subject.Blocked, 'finish_handoff_not_confirmed'):
                subject.monitor(self.child, self.device, self.plan, str(PID), '123', 'identity', 0,
                                self.clock.monotonic, self.clock.sleep)


class EndToEndFakeTests(unittest.TestCase):
    """Exercise orchestration through mocks only; never invoke ADB or a device."""
    def setUp(self):
        self.root = subject.ROOT / 'build' / ('stability-orchestration-fixture-' + secrets.token_hex(8))
        self.root.mkdir(parents=True, exist_ok=False)
        self.plan = {'scope': 'fixture', 'device': {'api': 33, 'abi': 'arm64-v8a', 'page_bytes': 4096},
                     'apk_sha256': subject.sha(b'fixture-apk'), 'run_id': RUN_ID,
                     'duration_ms': DURATION, 'outer_seconds': 1020, 'poll_seconds': 5,
                     'max_rounds': 10000, 'interval_ms': 250, 'phase_timeout_ms': 30000}
        self.plan_path = self.root / 'plan.json'
        self.plan_path.write_text(json.dumps(self.plan), encoding='utf-8')
        self.args = Namespace(serial='fixture-device', plan=self.plan_path, plan_sha256=subject.digest(self.plan_path),
                              admission=self.root / 'admission.json', out_dir=self.root / 'out',
                              private_dir=self.root / 'private', adb=Path('fake-adb'))
        self.clock = MonitorTests.Clock()
        self.receipt = None
        self.terminal = sample('PASS')
        self.reads = 0

    def fake_device(self, args, out, private, receipt):
        self.receipt = receipt
        device = mock.Mock()
        self.device_instance = device
        device.receipt = receipt
        device.pid.return_value = str(PID)
        device.process_identity.return_value = 'identity'
        device.read_record.side_effect = lambda kind, run_id: (None if self.clock.now < 900 or kind == 'stall'
                                                                else encode(self.terminal))
        def command(label, args, **kwargs):
            outputs = {
                'api': '33', 'abi': 'arm64-v8a', 'page': '4096',
                'transport-shell-uid': '2000',
                'start-own-activity': 'Status: ok\n',
                'own-activity-resumed': 'mResumedActivity: ActivityRecord{abc u0 ' + COMPONENT + ' t7}\n',
                'postflight-resumed': 'mResumedActivity: ActivityRecord{abc u0 other.app/.Activity t7}\n',
                'boot-after': 'same-boot', 'current-user-after': '0',
                'package-path-after': 'package:/data/app/abc/base.apk\n',
            }
            if label == 'pull-postflight-own-apk': Path(args[-1]).write_bytes(b'fixture-apk')
            return (0, outputs.get(label, ''), '')
        device.command.side_effect = command
        return device

    def execute(self, inventory=None, observer=None):
        child = mock.Mock()
        child.poll.side_effect = lambda: 0 if self.clock.now >= 900 else None
        child.returncode = 0
        self.child = child
        def popen(command, stdout, stderr):
            self.spawned_command = command
            stdout.write(instrumentation(self.terminal, 0).encode('utf-8'))
            stdout.flush()
            return child
        inventory = inventory or (lambda *args: ('0', '123', 'same-boot', ['/data/app/abc/base.apk']))
        observer = observer or (lambda *args: (sample(), {
            'mode': '0', 'frozen': '0', 'sample_started_monotonic': self.clock.now,
            'sample_finished_monotonic': self.clock.now}))
        with mock.patch.object(subject, 'validate_plan', return_value=({}, {})), \
             mock.patch.object(subject, 'admission_check', return_value='safe-admission'), \
             mock.patch.object(subject, 'Device', side_effect=self.fake_device), \
             mock.patch.object(subject, 'package_inventory', side_effect=inventory), \
             mock.patch.object(subject.subprocess, 'Popen', side_effect=popen), \
             mock.patch.object(subject, 'observe_once', side_effect=observer), \
             mock.patch.object(subject.time, 'monotonic', self.clock.monotonic), \
             mock.patch.object(subject.time, 'sleep', self.clock.sleep):
            code = subject.run(self.args)
        return code, json.loads((self.args.out_dir / 'receipt.json').read_text(encoding='utf-8'))

    def test_pass_needs_matching_terminal_record_and_postflight(self):
        code, receipt = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(receipt['state'], 'PASS_SCOPED_STABILITY_POSTFLIGHT')
        self.assertEqual(receipt['observation']['count'], 180)
        self.assertFalse(receipt['shared_device_lock'])
        self.assertEqual(receipt['foreground_window_state'], 'OWN_PROCESS_PRESENT_ACTIVITY_NOT_RESUMED_MAY_REMAIN')
        expected_args = subject.instrumentation_arguments(self.plan, PID, '0')
        self.assertEqual(self.spawned_command[5:], expected_args)
        self.assertEqual(self.device_instance.launcher_context,
                         (str(PID), tuple(['app_process', '/system/bin',
                          'com.android.commands.am.Am', *expected_args]), '2000'))

    def test_own_foreground_window_remaining_cannot_pass(self):
        old = self.fake_device
        def still_resumed(*args):
            device = old(*args)
            original = device.command.side_effect
            def command(label, parts, **kwargs):
                if label == 'postflight-resumed':
                    return (0, 'mResumedActivity: ActivityRecord{abc u0 ' + COMPONENT + ' t7}\n', '')
                return original(label, parts, **kwargs)
            device.command.side_effect = command
            return device
        self.fake_device = still_resumed
        code, receipt = self.execute()
        self.assertEqual(code, 2)
        self.assertEqual(receipt['reason'], 'own_activity_still_resumed')
        self.assertEqual(receipt['foreground_window_state'], 'OWN_ACTIVITY_RESUMED_KEEP_SCREEN_ON')

    def test_failed_install_is_unknown_and_never_launches(self):
        def fail_install(device, private, plan, paths, tools, save):
            device._mock_name = 'fixture'
            self.receipt['installation'] = 'REQUEST_IN_FLIGHT_OUTCOME_UNKNOWN'
            raise subject.Blocked('install_timeout')
        code, receipt = self.execute(fail_install)
        self.assertEqual(code, 2)
        self.assertEqual(receipt['state'], 'INCOMPLETE_OR_BLOCKED')
        self.assertEqual(receipt['installation_outcome'], 'UNKNOWN_REQUIRES_READ_ONLY_RECHECK')
        self.assertNotIn('android_report', receipt)

    def test_terminal_mismatch_never_passes(self):
        old = self.fake_device
        def mismatched(*args):
            device = old(*args)
            device.read_record.side_effect = lambda kind, run_id: (None if self.clock.now < 900 or kind == 'stall'
                                                                    else encode(sample('PASS', sequence=21)))
            return device
        self.fake_device = mismatched
        code, receipt = self.execute()
        self.assertEqual(code, 2)
        self.assertEqual(receipt['state'], 'INCOMPLETE_OR_BLOCKED')
        self.assertEqual(receipt['reason'], 'terminal_progress_mismatch')

    def test_observation_failure_stops_only_local_transport(self):
        # Failure after the child is started tests the local-transport-only
        # path; the runner must preserve the remote UNKNOWN state.
        def observed(*args):
            if self.clock.now >= 10: raise subject.Blocked('own_stall_reported')
            return sample(), {'mode': '0', 'frozen': '0', 'sample_started_monotonic': self.clock.now,
                              'sample_finished_monotonic': self.clock.now}
        code, receipt = self.execute(observer=observed)
        self.assertEqual(code, 2)
        self.assertEqual(receipt['reason'], 'own_stall_reported')
        self.assertEqual(receipt['remote_process_state'], 'UNKNOWN_LOCAL_TRANSPORT_ONLY_NO_FORCE_STOP')
        self.child.terminate.assert_called_once_with()
        self.assertIn('KEEP_SCREEN_ON_POSSIBLE', receipt['foreground_window_state'])


if __name__ == '__main__':
    unittest.main()
