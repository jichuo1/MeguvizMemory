#!/usr/bin/env python3
"""Synthetic local and fake-ADB checks; never connects to a device."""
import json
from pathlib import Path
import secrets
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
import android_native_pressure_round as runner

PASS = {'suite': runner.REPORT_SUITE, 'status': 'PASS', 'checks': 53, 'case_count': 4,
        'final_backing_bytes': 0, 'callback_origin': 'synthetic',
        'race_window_guaranteed': False, 'performance_qualified': False}
FAIL = {'suite': runner.REPORT_SUITE, 'status': 'FAIL', 'checks': 2, 'stage': 'create'}
INCOMPLETE = {'suite': runner.REPORT_SUITE, 'status': 'INCOMPLETE', 'checks': 9, 'stage': 'final_close'}
PS = 'PID NAME ARGS\n1 init init\n2 system_server system_server\n3 zygote zygote\n4 logd logd\n5 servicemanager service\n'
ACTIVITY = ('ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)\n'
            'ACTIVITY MANAGER RUNNING PROCESSES (dumpsys activity processes)\n')


def output(value=PASS, code=0):
    return ('INSTRUMENTATION_RESULT: report_json=' + json.dumps(value, separators=(',', ':')) +
            '\nINSTRUMENTATION_CODE: ' + str(code) + '\n')


class FakeProcess:
    def __init__(self, stdout, stderr, state):
        self.state = state
        self.returncode = None if state in ('frozen', 'timeout') else 0
        if state == 'output_flood':
            stdout.write(b'x' * (runner.MAX_INSTRUMENT_OUTPUT + 1)); stdout.flush()
            return
        if state not in ('frozen', 'timeout'):
            report = FAIL if state == 'fail' else INCOMPLETE if state == 'incomplete' else PASS
            stdout.write(output(report, 1 if state in ('fail', 'incomplete') else 0).encode())
            stdout.flush()
    def poll(self): return self.returncode
    def terminate(self): self.returncode = -1
    def wait(self, timeout): return self.returncode


class FakeDevice:
    scenario = ''
    apk = b''
    calls = []
    def __init__(self, args, out, private, receipt):
        self.receipt = receipt
        self.deadline = None
    def gate(self, label):
        if self.scenario == 'busy' and label == 'before-install': raise runner.Blocked('device_busy')
        self.receipt['gates'].append({'name': label, 'result': 'PASS_OBSERVATIONAL_ONLY'})
    def pid(self): return '1234' if self.scenario == 'frozen' else None
    def process_identity(self, pid, uid): return 'a' * 64
    def freeze_sample(self, pid, uid): return {'mode': '1', 'frozen': '1'}
    def command(self, label, parts, **kwargs):
        self.calls.append((label, [str(x) for x in parts]))
        if label == 'install-own-diagnostic' and self.scenario == 'install_timeout':
            raise runner.Blocked('install-own-diagnostic_timeout')
        if label == 'user-inventory' and self.scenario == 'other_user':
            return 0, 'Users:\n UserInfo{0:Owner:13} running\n UserInfo{10:Other:30}\n', ''
        if label == 'package-inventory-user' and self.scenario == 'other_user' and '10' in parts:
            return 0, 'package:android\npackage:' + runner.PACKAGE + '\n', ''
        if label == 'package-inventory-user' and self.scenario in ('same_apk', 'same_signer', 'bad_signer'):
            return 0, 'package:android\npackage:' + runner.PACKAGE + '\n', ''
        if label == 'package-path-before':
            return (0, 'package:/data/app/own/base.apk\n', '') if self.scenario in ('same_apk', 'same_signer', 'bad_signer') else (1, '', '')
        if label == 'pull-existing-own-apk':
            Path(parts[-1]).write_bytes(self.apk if self.scenario == 'same_apk' else b'older-apk')
            return 0, '', ''
        if label in ('pull-installed-own-apk', 'pull-postflight-own-apk'):
            value = b'different' if self.scenario == 'postflight_drift' and label == 'pull-postflight-own-apk' else self.apk
            Path(parts[-1]).write_bytes(value)
            return 0, '', ''
        values = {'api': '33', 'abi': 'arm64-v8a', 'page': '4096',
            'boot-before': '12345678-1234-1234-1234-123456789abc',
            'boot-after': '12345678-1234-1234-1234-123456789abc',
            'current-user': '0', 'current-user-postflight': '0',
            'user-inventory': 'Users:\n UserInfo{0:Owner:13} running\n',
            'package-inventory-user': 'package:android\n', 'own-uid': '10123',
            'install-own-diagnostic': 'Success\n',
            'package-path-after-install': 'package:/data/app/own/base.apk\n',
            'package-path-postflight': 'package:/data/app/own/base.apk\n'}
        if label not in values: raise AssertionError('unexpected fake command: ' + label)
        return 0, values[label], ''


class NativePressureRoundTests(unittest.TestCase):
    def setUp(self):
        self.work = runner.ROOT / 'build' / ('native-pressure-fake-' + secrets.token_hex(12))
        self.work.mkdir(parents=True, exist_ok=False)

    def test_report_contract_and_nonterminal_rejection(self):
        self.assertEqual(runner.parse_report(output(), 0), PASS)
        self.assertEqual(runner.parse_report(output(FAIL, 1), 0), FAIL)
        self.assertEqual(runner.parse_report(output(INCOMPLETE, 1), 0), INCOMPLETE)
        for value in [output()[:-1], output() + output(), output(dict(PASS, checks=54)),
                      output(dict(PASS, case_count=3)), output(dict(PASS, race_window_guaranteed=True)),
                      output(dict(PASS, callback_origin='system')), output(dict(PASS, final_backing_bytes=1)),
                      output(PASS, 1), output(dict(FAIL, stage='unknown'), 1),
                      'INSTRUMENTATION_STATUS: report_json=' + json.dumps(FAIL) + '\n']:
            with self.subTest(value=value), self.assertRaises(runner.Blocked): runner.parse_report(value, 0)

    def test_admission_parser(self):
        self.assertEqual(runner.parse_gate(PS, ACTIVITY)['result'], 'PASS_OBSERVATIONAL_ONLY')
        self.assertEqual(runner.parse_gate(PS + '8 DexDash diagnostic\n', ACTIVITY)['result'], 'BLOCKED')
        self.assertEqual(runner.parse_gate(PS, ACTIVITY + 'ActiveInstrumentation{other}\n')['result'], 'BLOCKED')
        with self.assertRaises(runner.Blocked): runner.parse_gate('', ACTIVITY)

    def test_transport_caps_and_timeout_stop_only_fake_child(self):
        original = subprocess.Popen
        for program, reason, timeout in [
            ('import os,time;os.write(1,b"x"*65536);time.sleep(10)', 'output_limit', 3),
            ('import os,time;os.write(2,b"x"*65536);time.sleep(10)', 'output_limit', 3),
            ('import time;time.sleep(10)', 'timeout', .3),
        ]:
            children = []
            def start(*args, **kwargs):
                child = original(*args, **kwargs); children.append(child); return child
            with self.subTest(reason=reason, program=program), \
                 mock.patch.object(runner.subprocess, 'Popen', side_effect=start), \
                 self.assertRaisesRegex(runner.Blocked, reason):
                runner.bounded_transport([sys.executable, '-c', program], timeout, 1024, 1024)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())

    def test_transport_drains_both_pipes(self):
        program = 'import os;os.write(2,b"e"*16384);os.write(1,b"o"*16384)'
        code, stdout, stderr = runner.bounded_transport([sys.executable, '-c', program], 3, 32768, 32768)
        self.assertEqual((code, len(stdout), len(stderr)), (0, 16384, 16384))

    def fake_run(self, scenario):
        apk = self.work / 'apk'; apk.write_bytes(b'fixture-apk')
        plan = {'scope': 'SYNTHETIC_ONLY', 'apk_sha256': runner.digest(apk), 'aar_sha256': 'a' * 64,
                'core_sha256': 'b' * 64, 'jni_sha256': 'c' * 64, 'certificate_sha256': 'd' * 64,
                'device': {'api': 33, 'abi': 'arm64-v8a', 'page_bytes': 4096},
                'timeout_seconds': 0 if scenario == 'timeout' else 15}
        plan_path = self.work / 'plan.json'; plan_path.write_text(json.dumps(plan), encoding='utf-8')
        args = SimpleNamespace(serial='fixture-device', plan=plan_path, plan_sha256=runner.digest(plan_path),
            apk=apk, aar=apk, sdk_core=apk, build_receipt=apk, adb=self.work / 'NO_ADB',
            out_dir=self.work / 'receipt', private_dir=self.work / 'raw')
        FakeDevice.scenario = scenario; FakeDevice.apk = apk.read_bytes(); FakeDevice.calls = []
        launched = []
        def popen(command, stdout, stderr):
            launched.append(command)
            self.assertEqual(command[-1], runner.COMPONENT)
            return FakeProcess(stdout, stderr, scenario)
        signer = 'e' if scenario == 'bad_signer' else 'd'
        fake_signer = SimpleNamespace(returncode=0,
            stdout=('certificate SHA-256 digest: ' + signer * 64).encode(), stderr=b'')
        with mock.patch.object(runner, 'BASE', self.work), mock.patch.object(runner, 'Device', FakeDevice), \
             mock.patch.object(runner, 'validate_plan', return_value=({'apk': apk}, {'java': 'fake', 'apksigner': 'fake'})), \
             mock.patch.object(runner.subprocess, 'Popen', side_effect=popen), \
             mock.patch.object(runner.subprocess, 'run', return_value=fake_signer):
            result = runner.run(args)
        receipt = json.loads((args.out_dir / 'receipt.json').read_text(encoding='utf-8'))
        self.assertNotIn(args.serial, json.dumps(receipt))
        self.assertFalse(any(any(word in ' '.join(parts) for word in ('force-stop', 'uninstall', 'clear'))
                             for _, parts in FakeDevice.calls))
        return result, receipt, launched

    def test_success_and_terminal_failures(self):
        result, value, launched = self.fake_run('pass')
        self.assertEqual(result, 0); self.assertTrue(launched)
        self.assertEqual(value['state'], 'PASS_FUNCTIONAL_POSTFLIGHT_FREEZER_NOT_OBSERVED')
        self.assertEqual(value['android_report'], PASS)
        self.assertTrue(value['normal_domain_closed_proven'])
        self.assertEqual(value['freezer_observation'], 'NOT_OBSERVED')

    def test_terminal_fail(self):
        result, value, _ = self.fake_run('fail')
        self.assertEqual(result, 2); self.assertEqual(value['state'], 'FAIL_FUNCTIONAL_REPORT_POSTFLIGHT_COMPLETE')
        self.assertFalse(value['normal_domain_closed_proven'])

    def test_terminal_incomplete(self):
        result, value, _ = self.fake_run('incomplete')
        self.assertEqual(result, 2); self.assertEqual(value['state'], 'INCOMPLETE_REPORTED_POSTFLIGHT_COMPLETE')
        self.assertFalse(value['normal_domain_closed_proven'])

    def test_freeze_does_not_force_stop(self):
        result, value, _ = self.fake_run('frozen')
        self.assertEqual(result, 2); self.assertEqual(value['freezer_observation'], 'FROZEN_OBSERVED')
        self.assertEqual(value['own_stop'], 'NOT_REQUESTED_SHARED_DEVICE_NO_LOCK')

    def test_timeout_does_not_force_stop(self):
        result, value, _ = self.fake_run('timeout')
        self.assertEqual(result, 2); self.assertEqual(value['stop_reason'], 'TIMEOUT')
        self.assertEqual(value['own_stop'], 'NOT_REQUESTED_SHARED_DEVICE_NO_LOCK')

    def test_install_outcome_unknown(self):
        result, value, launched = self.fake_run('install_timeout')
        self.assertEqual(result, 2); self.assertFalse(launched)
        self.assertEqual(value['installation_outcome'], 'UNKNOWN_REQUIRES_READ_ONLY_RECHECK')

    def test_other_user_blocks_install(self):
        result, value, launched = self.fake_run('other_user')
        self.assertEqual(result, 2); self.assertFalse(launched)
        self.assertEqual(value['reason'], 'same_package_present_for_other_user')

    def test_busy_blocks_install(self):
        result, value, launched = self.fake_run('busy')
        self.assertEqual(result, 2); self.assertFalse(launched)
        self.assertEqual(value['reason'], 'device_busy')

    def test_postflight_drift_blocks_pass(self):
        result, value, _ = self.fake_run('postflight_drift')
        self.assertEqual(result, 2); self.assertEqual(value['reason'], 'postflight_apk_identity_mismatch')
        self.assertTrue(value['report_validated'])
        self.assertFalse(value['normal_domain_closed_proven'])

    def test_existing_different_signer_blocks_install(self):
        result, value, launched = self.fake_run('bad_signer')
        self.assertEqual(result, 2); self.assertEqual(value['reason'], 'existing_package_signer_mismatch')
        self.assertFalse(launched)
        self.assertFalse(any(label == 'install-own-diagnostic' for label, _ in FakeDevice.calls))

    def test_existing_same_signer_update(self):
        result, value, _ = self.fake_run('same_signer')
        self.assertEqual(result, 0)
        self.assertTrue(any(label == 'install-own-diagnostic' and '-r' in args for label, args in FakeDevice.calls))

    def test_exact_installed_package_reused(self):
        result, value, _ = self.fake_run('same_apk')
        self.assertEqual(result, 0)
        self.assertEqual(value['installation'], 'EXACT_APK_ALREADY_INSTALLED_NO_UPDATE')
        self.assertFalse(any(label == 'install-own-diagnostic' for label, _ in FakeDevice.calls))

    def test_modified_plan_rejected_before_device(self):
        plan = self.work / 'plan.json'; plan.write_text('{}', encoding='utf-8')
        args = SimpleNamespace(serial='fixture-device', plan=plan, plan_sha256='0' * 64)
        with mock.patch.object(runner, 'Device') as device, self.assertRaisesRegex(runner.Blocked, 'frozen_plan_sha256_mismatch'):
            runner.run(args)
        device.assert_not_called()

    def test_build_source_drift_rejected(self):
        sources = runner.source_files()
        built = {'source_sha256': {name: runner.digest(path) for name, path in sources.items()}}
        self.assertEqual(runner.verify_build_sources(built), sources)
        built['source_sha256']['AndroidManifest.xml'] = '0' * 64
        with self.assertRaisesRegex(runner.Blocked, 'build_source_drift'):
            runner.verify_build_sources(built)

    def test_instrument_output_flood_remains_incomplete(self):
        result, value, _ = self.fake_run('output_flood')
        self.assertEqual(result, 2)
        self.assertEqual(value['stop_reason'], 'OUTPUT_LIMIT')
        self.assertFalse(value['report_validated'])

    def test_real_build_log_checks_are_bound_when_fixture_available(self):
        work = runner.ROOT / 'build' / 'native-pressure-probe-r8-ce2a5b2-arm64-v8a-20261101-final2'
        receipt = work / 'receipt.json'
        if not receipt.is_file(): self.skipTest('optional private R8 build fixture unavailable')
        built = runner.unique_json(receipt.read_text(encoding='utf-8'))
        runner.verify_build_logs(built, work)
        wrong = dict(built); wrong['r8_evidence'] = dict(built['r8_evidence'])
        wrong['r8_evidence']['mapping_sha256'] = '0' * 64
        with self.assertRaisesRegex(runner.Blocked, 'build_r8_log_drift'):
            runner.verify_build_logs(wrong, work)
        wrong = dict(built); wrong['operations'] = [dict(row) for row in built['operations']]
        wrong['operations'][0]['stdout_sha256'] = '0' * 64
        with self.assertRaisesRegex(runner.Blocked, 'build_log_drift'):
            runner.verify_build_logs(wrong, work)


if __name__ == '__main__': unittest.main()
