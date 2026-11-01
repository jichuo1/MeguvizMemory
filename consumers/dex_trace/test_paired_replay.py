#!/usr/bin/env python3
"""Focused integrity and oracle checks; no device or product input."""
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

import paired_replay as subject


def expected_progress():
    result = [json.dumps({'state': 'RUNNING', 'completed_pairs': n, 'total_pairs': 20})
              for n in (5, 10, 15, 20)]
    result.append(json.dumps({'state': 'PASS_SCOPED_PAIRED_READER_CAPTURE', 'pair_count': 20,
                              'scope': subject.PAIR_SCOPE, 'performance_qualified': False}))
    return ('\n'.join(result) + '\n').encode()


def trace(payload, events):
    lines = [b'MGMEM_CORE_MODULE_HEX 00', b'{}',
             b'MGMEM_DEX_READER_TRACE_V1 ' + str(len(events)).encode() + b' ' +
             hashlib.sha256(payload).hexdigest().encode()]
    lines.extend(f'E {site} {offset}'.encode() for site, offset in events)
    return b'\n'.join(lines) + b'\n'


class ReplayContractTests(unittest.TestCase):
    def fake_workspace(self, base):
        base = Path(base)
        def file(name, data=b'x'):
            path = base / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data); return path
        sdk = base / 'sdk'
        core = file('sdk/bin/mgmem.dll' if os.name == 'nt' else 'sdk/lib/libmgmem.so', b'core')
        dex_stage = base / 'dex-stage'; dex_stage.mkdir()
        paired_stage = base / 'paired-stage'; paired_stage.mkdir()
        return SimpleNamespace(input=file('input.dex', b'dex\n'), baseline=file('baseline/a.exe'),
            traced=file('traced/b.exe'), core=core, candidate=file('candidate.zip'),
            candidate_evidence=file('evidence.json'), source_lock=file('lock.json'), dex_stage=dex_stage,
            paired_stage=paired_stage, sdk_prefix=sdk, out_dir=base / 'out', max_events=10,
            timeout_seconds=5, capture_timeout_seconds=20, overall_timeout_seconds=60)

    def test_terminal_requires_complete_progress_and_one_success(self):
        self.assertEqual(subject.verify_capture_stdout(expected_progress())['pair_count'], 20)
        for data in (expected_progress().rstrip(b'\n'), expected_progress() + b'{}\n',
                     expected_progress().replace(b'"completed_pairs": 15', b'"completed_pairs": true'),
                     expected_progress().replace(b'"pair_count": 20', b'"pair_count": 19')):
            with self.subTest(data=data[-100:]), self.assertRaises(ValueError):
                subject.verify_capture_stdout(data)

    def test_byte_oracle_preserves_site_offset_value_and_order(self):
        data = b'dex\nabc'
        events = [(1, 2), (1001, 5), (1, 2)]
        digest = hashlib.sha256(b''.join(struct.pack('<IQB', site, offset, data[offset])
                                         for site, offset in events)).hexdigest()
        actual = subject.oracle(trace(data, events), data, hashlib.sha256(data).hexdigest(),
                                'D-C1', 'normal', 3, 2, 10)
        self.assertEqual(actual, digest)
        reversed_digest = subject.oracle(trace(data, list(reversed(events[:2]))), data,
                                         hashlib.sha256(data).hexdigest(), 'D-C1', 'normal', 2, 2, 10)
        self.assertNotEqual(actual, reversed_digest)

    def test_corrupt_input_uses_mutated_first_byte(self):
        data = b'dex\n'
        mutated = b'\0' + data[1:]
        digest = hashlib.sha256(struct.pack('<IQB', 1, 0, 0)).hexdigest()
        self.assertEqual(subject.oracle(trace(mutated, [(1, 0)]), data, hashlib.sha256(mutated).hexdigest(),
                                        'D-C2', 'corrupt-input', 1, 1, 10), digest)
        with self.assertRaises(ValueError):
            subject.oracle(trace(data, [(1, 0)]), data, hashlib.sha256(data).hexdigest(),
                           'D-C2', 'corrupt-input', 1, 1, 10)

    def test_zero_events_only_for_admission_rejections(self):
        data = b'dex\n'; digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(subject.oracle(trace(data, []), data, digest, 'D-Z0', 'invalid-lease', 0, 0, 10),
                         hashlib.sha256(b'').hexdigest())
        with self.assertRaises(ValueError):
            subject.oracle(trace(data, []), data, digest, 'D-Z0', 'normal', 0, 0, 10)
        with self.assertRaises(ValueError):
            subject.oracle(trace(data, [(1, 0)]), data, digest, 'D-C0', 'borrow-budget', 1, 1, 10)

    def test_rejects_truncation_extra_out_of_range_and_bad_sites(self):
        data = b'dex\n'; digest = hashlib.sha256(data).hexdigest()
        samples = [trace(data, [(1, 0)])[:-1], trace(data, [(1, 0)]) + b'E 1 1\n',
                   trace(data, [(32, 0)]), trace(data, [(1, len(data))])]
        for value in samples:
            with self.subTest(value=value[-20:]), self.assertRaises(ValueError):
                subject.oracle(value, data, digest, 'D-C1', 'normal', 1, 1, 10)

    def test_rejects_bool_or_forged_counts_and_unknown_case(self):
        data = b'dex\n'; digest = hashlib.sha256(data).hexdigest()
        for variant, case, count, sites in [('D-C1', 'normal', True, 1), ('D-C1', 'normal', 1, True),
                                            ('invalid', 'normal', 1, 1), ('D-C1', 'unknown', 1, 1),
                                            ('D-C1', 'normal', 1, 2)]:
            with self.subTest(variant=variant, case=case), self.assertRaises(ValueError):
                subject.oracle(trace(data, [(1, 0)]), data, digest, variant, case, count, sites, 10)

    def test_worker_report_does_not_promote_unknown_cleanup(self):
        value = {'schema_version': 1, 'state': 'PASS_SCOPED_READER_BYTE_WORKER', 'variant': 'D-C1',
                 'case': 'normal', 'events': 1, 'checksum_sha256': 'a' * 64,
                 'core_use': 'REQUESTED_SHARED_CORE_NOT_LOAD_ATTESTED',
                 'cleanup_outcome': 'CORE_CLOSED_ZERO_AND_DESTROYED', 'device': 'NOT_RUN',
                 'performance': 'NOT_MEASURED'}
        subject.verify_worker_report(value, 'D-C1', 'normal', 1, 'a' * 64)
        for change in ({'cleanup_outcome': 'UNKNOWN_QUARANTINED'}, {'events': True},
                       {'core_use': 'NOT_APPLICABLE'}, {'checksum_sha256': 'b' * 64}, {'extra': 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                subject.verify_worker_report(value | change, 'D-C1', 'normal', 1, 'a' * 64)

    def test_bound_file_rejects_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'capture.stdout'; path.write_bytes(b'one')
            sha = hashlib.sha256(b'one').hexdigest()
            self.assertEqual(subject.verified_file(path, 16, 3, sha), b'one')
            path.write_bytes(b'two')
            with self.assertRaises(ValueError): subject.verified_file(path, 16, 3, sha)

    def test_failed_capture_or_worker_cannot_use_a_success_report(self):
        # Even a complete-looking saved receipt or stdout does not replace the
        # caller's observation that the child exited successfully.
        for code, stderr in ((1, b''), (True, b''), (0, b'failure')):
            with self.subTest(code=code), self.assertRaises(ValueError):
                subject.verify_child_exit(code, stderr, 'child_incomplete')
        subject.verify_child_exit(0, b'', 'child_incomplete')

    def test_failed_capture_with_saved_pass_artifacts_does_not_publish_replay(self):
        build = subject.prepare_consumer.build_root()
        with tempfile.TemporaryDirectory(dir=build) as tmp:
            args = self.fake_workspace(tmp)
            def failed_capture(command, *unused, **kwargs):
                directory = Path(command[command.index('--out-dir') + 1]); directory.mkdir()
                (directory / 'receipt.json').write_text('{"state":"PASS_SCOPED_PAIRED_READER_CAPTURE"}')
                (directory / subject.paired.BINDING_NAME).write_text('{"state":"PAIR_OUTPUT_FILES_BOUND"}')
                return 7, expected_progress(), b''
            with mock.patch.object(subject, 'preflight_sdk', return_value=('a' * 40, 'b' * 64)), \
                 mock.patch.object(subject.prepare_consumer, 'verify_prepared'), \
                 mock.patch.object(subject.paired, 'verify_paired'), \
                 mock.patch.object(subject.capture, 'run_bounded', side_effect=failed_capture):
                with self.assertRaisesRegex(ValueError, 'capture_process_incomplete'):
                    subject.run(args)
            self.assertFalse((args.out_dir / 'replay-receipt.json').exists())

    def test_failed_worker_with_success_json_does_not_publish_replay(self):
        build = subject.prepare_consumer.build_root()
        with tempfile.TemporaryDirectory(dir=build) as tmp:
            args = self.fake_workspace(tmp)
            values = {key: None for key in subject.paired.STABLE}
            def run_child(command, *unused, **kwargs):
                if str(subject.PRODUCER) in command:
                    directory = Path(command[command.index('--out-dir') + 1]); directory.mkdir()
                    input_sha = subject.capture.sha(args.input)
                    file_entry = lambda name: {'name': name, 'bytes': 1, 'sha256': hashlib.sha256(b'x').hexdigest()}
                    pair = {'variant': 'D-C1', 'case': 'normal', 'snapshot_sha256': input_sha,
                            'events': 1, 'sites': 1}
                    for mode in ('baseline', 'traced'):
                        name = mode + '-D-C1-normal.stdout'
                        err = mode + '-D-C1-normal.stderr'
                        (directory / name).write_bytes(b'x'); (directory / err).write_bytes(b'')
                        pair[mode] = {'stdout': file_entry(name), 'stderr': file_entry(err)}
                    binding = {'input_sha256': input_sha, 'input_bytes': 4,
                        'corrupt_snapshot_sha256': hashlib.sha256(b'\0ex\n').hexdigest(),
                        'producer_script_sha256': subject.capture.sha(subject.PRODUCER),
                        'baseline_executable_sha256': subject.capture.sha(args.baseline),
                        'traced_executable_sha256': subject.capture.sha(args.traced),
                        'core_file_sha256': subject.capture.sha(args.core),
                        'source_lock_sha256': subject.capture.sha(args.source_lock), 'event_limit': 10,
                        'candidate_source_commit': 'a' * 40, 'candidate_sha256': 'b' * 64, 'pairs': [pair]}
                    receipt = {'state': 'PASS_SCOPED_PAIRED_READER_CAPTURE', 'scope': subject.PAIR_SCOPE,
                        'performance_qualified': False, 'input_sha256': input_sha, 'input_bytes': 4,
                        'candidate_source_commit': 'a' * 40, 'candidate_sha256': 'b' * 64}
                    (directory / subject.paired.BINDING_NAME).write_text(json.dumps(binding))
                    (directory / 'receipt.json').write_text(json.dumps(receipt))
                    (directory / 'progress.json').write_text('{}')
                    return 0, expected_progress(), b''
                # A worker can emit a success-shaped JSON yet still fail.
                return 9, b'{"state":"PASS_SCOPED_READER_BYTE_WORKER"}\n', b''
            with mock.patch.object(subject, 'preflight_sdk', return_value=('a' * 40, 'b' * 64)), \
                 mock.patch.object(subject.prepare_consumer, 'verify_prepared'), \
                 mock.patch.object(subject.paired, 'verify_paired'), \
                 mock.patch.object(subject.paired, 'validate_pair_binding', side_effect=lambda value: value), \
                 mock.patch.object(subject.paired, 'verify_bound_outputs'), \
                 mock.patch.object(subject.paired, 'parse_output', side_effect=lambda raw, traced, *unused:
                     (values, {'events': 1, 'site_count': 1} if traced else None)), \
                 mock.patch.object(subject, 'oracle', return_value='a' * 64), \
                 mock.patch.object(subject.capture, 'run_bounded', side_effect=run_child):
                with self.assertRaisesRegex(ValueError, 'worker_process_incomplete'):
                    subject.run(args)
            self.assertFalse((args.out_dir / 'replay-receipt.json').exists())

    def test_helper_identity_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / 'trace_replay.py'; helper.write_bytes(b'old')
            locked = {str(helper): hashlib.sha256(b'old').hexdigest()}
            subject.verify_identities(locked)
            helper.write_bytes(b'new')
            with self.assertRaisesRegex(ValueError, 'replay_input_or_binary_drift'):
                subject.verify_identities(locked)

    def test_identity_hash_has_a_fixed_cap_and_checks_deadline(self):
        with mock.patch.object(subject.capture, 'input_identity', return_value=(4, 'a' * 64)) as digest:
            self.assertEqual(subject.identity_hash(Path('helper')), 'a' * 64)
            digest.assert_called_once_with(Path('helper'), maximum=256 * 1024 * 1024)
        with mock.patch.object(subject.time, 'monotonic', return_value=10):
            with self.assertRaisesRegex(ValueError, 'overall_timeout'):
                subject.verify_identities({'helper': 'a' * 64}, deadline=10)

    def test_child_environment_and_python_flags_reject_injection(self):
        fake = {'SYSTEMROOT': 'safe-root', 'TEMP': 'safe-temp', 'PATH': 'malicious',
                'PYTHONPATH': 'malicious', 'PYTHONHOME': 'malicious', 'LD_PRELOAD': 'malicious',
                'LD_LIBRARY_PATH': 'malicious', 'GITHUB_TOKEN': 'secret'}
        env = subject.child_environment(fake)
        self.assertNotIn('PYTHONPATH', env); self.assertNotIn('PYTHONHOME', env)
        self.assertNotIn('LD_PRELOAD', env); self.assertNotIn('GITHUB_TOKEN', env)
        self.assertNotIn('malicious', env['PATH'])
        self.assertNotIn('LD_LIBRARY_PATH', env)
        self.assertNotIn('controlled', env['PATH'])
        command = [sys.executable, '-E', '-s', '-S', '-c',
                   'import json,sys; print(json.dumps([sys.flags.ignore_environment,sys.flags.no_user_site,sys.flags.no_site]))']
        # Use actual platform variables for launching Python; the caller's
        # injected values above are never copied to this environment.
        code, out, err = subject.capture.run_bounded(command, 5, stdout_limit=1024, stderr_limit=1024,
                                                      env=subject.child_environment())
        self.assertEqual(code, 0); self.assertEqual(err, b'')
        self.assertEqual(json.loads(out), [1, 1, 1])
        self.assertEqual(subject.python_command('sample.py')[:4], [sys.executable, '-E', '-s', '-S'])

    def test_unverified_sdk_or_wrong_core_rejected_before_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); sdk = root / 'sdk'
            rel = Path('bin/mgmem.dll') if os.name == 'nt' else Path('lib/libmgmem.so')
            core = sdk / rel; core.parent.mkdir(parents=True); core.write_bytes(b'core')
            archive = root / 'candidate.zip'
            with zipfile.ZipFile(archive, 'w') as out:
                out.writestr('sdk/' + rel.as_posix(), b'core')
            evidence = root / 'evidence.json'
            evidence.write_text(json.dumps({'state': 'PASS_SCOPED_ENGINEERING_CANDIDATES',
                'source_commit': 'a' * 40,
                'windows': {'bytes': archive.stat().st_size, 'sha256': subject.capture.sha(archive)}}),
                encoding='utf-8')
            self.assertEqual(subject.preflight_sdk(archive, evidence, sdk, core)[0], 'a' * 40)
            other = root / 'other.dll'; other.write_bytes(b'core')
            with self.assertRaisesRegex(ValueError, 'selected_core_mismatch'):
                subject.preflight_sdk(archive, evidence, sdk, other)
            (core.parent / 'extra').write_bytes(b'injected')
            with mock.patch.object(subject.capture, 'run_bounded') as launch:
                with self.assertRaisesRegex(ValueError, 'installed_sdk_file_set_mismatch'):
                    subject.preflight_sdk(archive, evidence, sdk, core)
                launch.assert_not_called()

    def test_oracle_respects_cooperative_deadline(self):
        data = b'dex\n'; digest = hashlib.sha256(data).hexdigest()
        with mock.patch.object(subject.time, 'monotonic', return_value=10):
            with self.assertRaisesRegex(ValueError, 'overall_timeout'):
                subject.oracle(trace(data, [(1, 0)]), data, digest, 'D-C1', 'normal', 1, 1, 10, deadline=10)

    def test_atomic_receipt_write_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'replay-receipt.json'
            with mock.patch.object(subject.os, 'fsync', side_effect=OSError('forced')):
                with self.assertRaises(OSError): subject.publish_receipt(target, b'{"state":"PASS"}\n')
            self.assertFalse(target.exists())
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'replay-receipt.json'
            subject.publish_receipt(target, b'first\n')
            self.assertEqual(target.read_bytes(), b'first\n')
            with self.assertRaises(OSError): subject.publish_receipt(target, b'second\n')
            self.assertEqual(target.read_bytes(), b'first\n')


if __name__ == '__main__':
    unittest.main()
