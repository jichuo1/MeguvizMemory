#!/usr/bin/env python3
"""Offline parser, admission, freeze and cleanup regressions; never calls ADB."""
import hashlib
import json
from pathlib import Path
import secrets
from types import SimpleNamespace
from unittest import mock

import android_borrow_round as runner


PASS = {'suite': 'android-native-borrow-v1', 'status': 'PASS', 'checks': 29,
        'read_only_and_write_cases': 2, 'initialization_assertion_and_bytes': 'PASS',
        'owner_release_and_domain_closing_lease_access': 'PASS',
        'duplicate_claim_and_idempotent_close': 'PASS', 'final_domain_backing_bytes': 0,
        'performance_qualified': False, 'scope': 'isolated_own_domain_no_BIL_no_actual_low_memory'}
FAIL = {'suite': 'android-native-borrow-v1', 'status': 'FAIL', 'checks': 7, 'error_type': 'AssertionError'}
ACTIVITY = ('ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)\n'
            'ACTIVITY MANAGER RUNNING PROCESSES (dumpsys activity processes)\n')
PROCESSES = ('PID NAME ARGS\n1 init /init\n2 system_server system_server\n'
             '3 zygote zygote\n4 logd logd\n5 servicemanager servicemanager\n')


def output(value=PASS, code=0):
    return ('INSTRUMENTATION_RESULT: report_json=' + json.dumps(value, separators=(',', ':')) +
            '\nINSTRUMENTATION_CODE: ' + str(code) + '\n')


def rejects(value, code=0):
    try:
        runner.parse_report(value, code)
    except (runner.Blocked, UnicodeDecodeError):
        return
    raise AssertionError('invalid report was accepted')


def parsers():
    assert runner.parse_report(output(), 0) == PASS
    assert runner.parse_report(output(FAIL, 1), 0) == FAIL
    rejects(output(), 1)
    rejects(output(PASS, 1))
    rejects(output(FAIL, 0))
    rejects(output() + output())
    rejects('INSTRUMENTATION_CODE: 0\n' + output())
    rejects(output().replace('"checks":29', '"checks":true'))
    rejects(output().replace('"checks":29', '"checks":29,"checks":29'))
    rejects(output().replace('"checks":29', '"checks":NaN'))
    rejects(output().replace('"final_domain_backing_bytes":0', '"final_domain_backing_bytes":1'))
    rejects(output().replace('"performance_qualified":false', '"performance_qualified":0'))
    rejects(output().replace('INSTRUMENTATION_CODE: 0', 'INSTRUMENTATION_FAILED: crashed\nINSTRUMENTATION_CODE: 0'))
    rejects(output().replace('INSTRUMENTATION_CODE: 0', 'INSTRUMENTATION_RESULT: shortMsg=crashed\nINSTRUMENTATION_CODE: 0'))
    rejects(output() + 'INSTRUMENTATION_STATUS_CODE: -2\n')
    rejects(output()[:-2])
    rejects(output()[:-1])
    rejects(b'\xff')
    clean = runner.parse_gate(PROCESSES, ACTIVITY)
    assert clean['result'] == 'PASS_OBSERVATIONAL_ONLY'
    assert runner.parse_gate(PROCESSES + '6 gpuDiagnostic benchmark\n', ACTIVITY)['result'] == 'BLOCKED'
    assert runner.parse_gate(PROCESSES + '6 DexMeguviz worker\n', ACTIVITY)['result'] == 'BLOCKED'
    assert runner.parse_gate(PROCESSES + '6 DexDash worker\n', ACTIVITY)['result'] == 'BLOCKED'
    assert runner.parse_gate(PROCESSES + '6 com.meguvizpack app\n', ACTIVITY)['result'] == 'BLOCKED'
    assert runner.parse_gate(PROCESSES, ACTIVITY + 'ActiveInstrumentation{example}\n')['result'] == 'BLOCKED'
    for ps, activity in [('', ACTIVITY), ('PID NAME ARGS\n', ACTIVITY), ('1 root init\n', ACTIVITY),
                         ('PID NAME ARGS\n1 system_server system_server\n', ''),
                         ('PID NAME ARGS\n1 system_server system_server\n', 'Permission Denial: cannot dump activity\n'),
                         ('PID NAME ARGS\n1 system_server system_server\n', ACTIVITY + 'Permission Denial: cannot dump activity\n')]:
        try:
            runner.parse_gate(ps, activity)
        except runner.Blocked:
            pass
        else:
            raise AssertionError('unparseable inventory was accepted')
    assert runner.parse_package_paths('package:/data/app/own/base.apk\r\n') == ['/data/app/own/base.apk']
    assert runner.parse_package_paths('') == []
    for bad in ('package:/data/app/own/base.apk\nunexpected output\n',
                'package:/data/app/a/base.apk\npackage:/data/app/b/base.apk\n',
                'package:/outside/base.apk\n'):
        try: runner.parse_package_paths(bad)
        except runner.Blocked: pass
        else: raise AssertionError('ambiguous installed package path was accepted')
    tree = ('  E: manifest (line=1)\n    E: uses-sdk (line=1)\n    E: application (line=2)\n'
            '      A: http://schemas.android.com/apk/res/android:label(0x01010001)="probe"\n'
            '      A: http://schemas.android.com/apk/res/android:debuggable(0x0101000f)=true\n'
            '      A: http://schemas.android.com/apk/res/android:allowBackup(0x01010280)=false\n'
            '    E: instrumentation (line=3)\n'
            '      A: http://schemas.android.com/apk/res/android:name(0x01010003)="' + runner.INSTRUMENT + '"\n'
            '      A: http://schemas.android.com/apk/res/android:targetPackage(0x01010021)="' + runner.PACKAGE + '"\n'
            '      A: http://schemas.android.com/apk/res/android:functionalTest(0x01010023)=true\n')
    runner.verify_manifest_tree(tree)
    for bad in (tree.replace(runner.INSTRUMENT, 'malicious.Other'),
                tree.replace('targetPackage(0x01010021)="' + runner.PACKAGE + '"', 'targetPackage(0x01010021)="other.app"'),
                tree.replace('debuggable(0x0101000f)=true','debuggable(0x0101000f)=false'),
                tree.replace('    E: uses-sdk (line=1)', '    A: http://schemas.android.com/apk/res/android:sharedUserId(0x0101000b)=@0x7f010001\n    E: uses-sdk (line=1)'),
                tree.replace('    E: uses-sdk (line=1)', '    E: uses-permission (line=1)\n    E: uses-sdk (line=1)'),
                tree + '    E: receiver (line=7)\n'):
        try: runner.verify_manifest_tree(bad)
        except runner.Blocked: pass
        else: raise AssertionError('unsafe manifest was accepted')


def atomic(work):
    target = work / 'atomic.json'; target.write_bytes(b'old')
    original = runner.os.replace; calls = []
    def transient(source, destination):
        calls.append(1)
        assert target.read_bytes() == b'old'
        if len(calls) < 3: raise PermissionError('private-machine-path')
        original(source, destination)
    with mock.patch.object(runner.os, 'replace', side_effect=transient):
        runner.atomic_receipt(target, {'state': 'PASS'})
    assert len(calls) == 3 and json.loads(target.read_text()) == {'state': 'PASS'}
    prior = target.read_bytes(); pending = target.with_suffix('.pending')
    pending.write_bytes(b'previous pending')
    try:
        runner.atomic_receipt(target, {'state': 'OTHER'})
    except runner.Blocked as error:
        assert str(error) == 'receipt_pending_already_exists'
    else:
        raise AssertionError('existing pending was overwritten')
    assert target.read_bytes() == prior and pending.read_bytes() == b'previous pending'
    denied = work / 'denied.json'; denied.write_bytes(b'old-denied')
    ticks = iter([0, .5, 1])
    with mock.patch.object(runner.os, 'replace', side_effect=PermissionError('private-machine-path')), \
         mock.patch.object(runner.time, 'monotonic', side_effect=lambda: next(ticks)), \
         mock.patch.object(runner.time, 'sleep', return_value=None):
        try: runner.atomic_receipt(denied, {'state': 'NEW'})
        except runner.Blocked as error: assert str(error) == 'receipt_replace_permission_denied'
        else: raise AssertionError('persistent denial was accepted')
    assert denied.read_bytes() == b'old-denied' and denied.with_suffix('.pending').is_file()


class FakeProcess:
    def __init__(self, stdout, stderr, frozen, fail=False):
        self.stdout, self.stderr, self.frozen = stdout, stderr, frozen
        self.code = None if frozen else 0
        stdout.write(output(FAIL, 1).encode() if fail else output().encode()); stdout.flush()
    @property
    def returncode(self): return self.code
    def poll(self): return self.code
    def wait(self, timeout):
        self.code = 0
        return self.code
    def terminate(self): self.code = -1


class FakeDevice:
    calls = []
    sample_frozen = False
    expected_apk = b''
    other_busy = False
    installation_timeout = False
    other_user_package = False
    def __init__(self, args, out, private, receipt):
        self.receipt = receipt; self.installed = False; self.deadline = None
    def gate(self, label):
        self.receipt['gates'].append({'name': label, 'result': 'PASS_OBSERVATIONAL_ONLY'})
    def pid(self): return '1234' if self.sample_frozen else None
    def process_identity(self, pid, uid): return 'f' * 64
    def freeze_sample(self, pid, uid):
        return {'mode': '1' if self.sample_frozen else '0', 'frozen': '1' if self.sample_frozen else '0',
                'pid_sha256': 'a' * 64, 'cgroup_path_sha256': 'b' * 64, 'observed_at_utc': 'fixture'}
    def command(self, label, parts, **_):
        self.calls.append((label, [str(x) for x in parts]))
        if label == 'install-own-diagnostic' and self.installation_timeout:
            raise runner.Blocked('install-own-diagnostic_timeout')
        if label == 'user-inventory' and self.other_user_package:
            return 0, 'Users:\n UserInfo{0:Owner:13} running\n UserInfo{10:Other:30}\n', ''
        if label == 'package-inventory-user' and self.other_user_package and '10' in parts:
            return 0, 'package:com.android.settings\npackage:' + runner.PACKAGE + '\n', ''
        values = {
            'api': '33\n', 'abi': 'arm64-v8a\n', 'page': '4096\n',
            'boot-before': '12345678-1234-1234-1234-123456789abc\n',
            'boot-after': '12345678-1234-1234-1234-123456789abc\n', 'current-user': '0\n',
            'user-inventory': 'Users:\n UserInfo{0:Owner:13} running\n',
            'package-inventory-user': 'package:com.android.settings\n', 'own-uid': '10123\n',
            'install-own-diagnostic': 'Success\n',
            'package-path-after-install': 'package:/data/app/own/base.apk\n',
            'package-path-postflight': 'package:/data/app/own/base.apk\n',
            'cleanup-processes': PROCESSES + '1234 ' + runner.PACKAGE + ' app\n' + ('555 DexDash other\n' if self.other_busy else ''),
            'cleanup-activity': ACTIVITY + 'ActiveInstrumentation{' + runner.PACKAGE + '}\n',
            'stop-only-own-diagnostic': '',
        }
        if label == 'package-path-before': return 1, '', ''
        if label in ('pull-installed-own-apk', 'pull-postflight-own-apk'):
            Path(parts[-1]).write_bytes(self.expected_apk)
            return 0, '', ''
        assert label in values, label
        return 0, values[label], ''


def synthetic_run(work, frozen=False, timeout=False, fail=False, late_exit=False, other_busy=False, install_timeout=False, other_user=False):
    suffix = str(frozen) + str(timeout) + str(fail) + str(late_exit) + str(other_busy) + str(install_timeout) + str(other_user)
    serial = 'synthetic-device'; apk = work / ('apk-' + suffix); apk.write_bytes(b'synthetic-apk')
    plan_path = work / ('plan-' + suffix + '.json')
    plan = {'scope': 'SYNTHETIC_NO_DEVICE', 'apk_sha256': hashlib.sha256(apk.read_bytes()).hexdigest(),
            'aar_sha256': 'a' * 64, 'core_sha256': 'b' * 64, 'jni_sha256': 'c' * 64,
            'certificate_sha256': 'd' * 64, 'device': {'api': 33, 'abi': 'arm64-v8a', 'page_bytes': 4096},
            'timeout_seconds': 0 if timeout else 15}
    plan_path.write_text(json.dumps(plan))
    args = SimpleNamespace(serial=serial, plan=plan_path, apk=apk, aar=apk, sdk_core=apk, build_receipt=apk,
                           plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                           adb=work / 'NO_ADB', out_dir=work / ('receipts-' + suffix),
                           private_dir=work / ('private-' + suffix))
    FakeDevice.calls = []; FakeDevice.sample_frozen = frozen; FakeDevice.expected_apk = apk.read_bytes()
    FakeDevice.other_busy = other_busy; FakeDevice.installation_timeout = install_timeout; FakeDevice.other_user_package = other_user
    clock = {'launched': False, 'start_seen': False}
    def fake_popen(command, stdout, stderr):
        assert str(args.adb) == str(command[0])
        clock['launched'] = True
        return FakeProcess(stdout, stderr, frozen or timeout, fail)
    with mock.patch.object(runner, 'BASE', work), mock.patch.object(runner, 'Device', FakeDevice), \
         mock.patch.object(runner, 'validate_plan', return_value=({'apk': apk}, {})), \
         mock.patch.object(runner.subprocess, 'Popen', side_effect=fake_popen):
        expected_hash = args.plan_sha256
        args.plan_sha256 = '0' * 64
        try: runner.run(args)
        except runner.Blocked as error: assert str(error) == 'frozen_plan_sha256_mismatch'
        else: raise AssertionError('modified plan hash was accepted')
        assert not FakeDevice.calls and not args.out_dir.exists() and not args.private_dir.exists()
        args.plan_sha256 = expected_hash
        if late_exit:
            def now():
                if clock['launched'] and not clock['start_seen']:
                    clock['start_seen'] = True
                    return 100.0
                return 116.0 if clock['launched'] else 100.0
            with mock.patch.object(runner.time, 'monotonic', side_effect=now):
                result = runner.run(args)
        else:
            result = runner.run(args)
    value = json.loads((args.out_dir / 'receipt.json').read_text(encoding='utf-8'))
    assert serial not in json.dumps(value)
    assert all('uninstall' not in ' '.join(parts) and 'clear' not in ' '.join(parts) for _, parts in FakeDevice.calls)
    if other_user:
        assert result == 2 and value['state'] == 'INCOMPLETE_OR_BLOCKED'
        assert value['reason'] == 'same_package_present_for_other_user' and value['other_user_package_present']
        assert not [x for x in FakeDevice.calls if x[0] == 'install-own-diagnostic']
    elif install_timeout:
        assert result == 2 and value['state'] == 'INCOMPLETE_OR_BLOCKED'
        assert value['installation_outcome'] == 'UNKNOWN_REQUIRES_READ_ONLY_RECHECK'
        assert value['remote_process_state'] == 'UNKNOWN_POSTFLIGHT_NOT_COMPLETED'
        assert not [x for x in FakeDevice.calls if x[0] == 'stop-only-own-diagnostic']
    elif frozen:
        assert result == 2 and value['state'] == 'ENVIRONMENT_FROZEN_INCOMPLETE'
        assert value['freezer_observation'] == 'FROZEN_OBSERVED' and not value['normal_domain_closed_proven']
        stops = [x for x in FakeDevice.calls if x[0] == 'stop-only-own-diagnostic']
        assert len(stops) == (0 if other_busy else 1)
    elif timeout or late_exit:
        assert result == 2 and value['state'] == 'TIMEOUT_OR_PROCESS_CHANGE_INCOMPLETE'
        assert value['stop_reason'] == 'TIMEOUT' and not value['normal_domain_closed_proven']
        assert not [x for x in FakeDevice.calls if x[0] == 'stop-only-own-diagnostic']
    elif fail:
        assert result == 2 and value['state'] == 'FAIL_FUNCTIONAL_REPORT_POSTFLIGHT_COMPLETE'
        assert not value['normal_domain_closed_proven'] and value['android_report']['status'] == 'FAIL'
    else:
        assert result == 0 and value['state'] == 'PASS_FUNCTIONAL_POSTFLIGHT_FREEZER_NOT_OBSERVED'
        assert value['normal_domain_closed_proven'] and value['freezer_observation'] == 'NOT_OBSERVED'


def main():
    work = runner.ROOT / 'build' / ('borrow-runner-offline-' + secrets.token_hex(8))
    work.mkdir(parents=True, exist_ok=False)
    parsers(); atomic(work)
    synthetic_run(work); synthetic_run(work, frozen=True); synthetic_run(work, timeout=True); synthetic_run(work, fail=True)
    synthetic_run(work, late_exit=True); synthetic_run(work, frozen=True, other_busy=True); synthetic_run(work, install_timeout=True)
    synthetic_run(work, other_user=True)
    print(json.dumps({'state': 'PASS_OFFLINE_SYNTHETIC_NO_DEVICE', 'coverage': ['strict_report', 'occupancy',
          'receipt_atomic', 'unobserved_freezer', 'observed_freeze', 'timeout_own_only_cleanup']}))


if __name__ == '__main__':
    main()
