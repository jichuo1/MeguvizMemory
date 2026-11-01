#!/usr/bin/env python3
"""Offline structural tests; no external parser, compiler or device required."""
import hashlib
import io
from pathlib import Path
import secrets
import subprocess
import sys
import time
import unittest
from unittest import mock
import capture
import prepare


class TraceContract(unittest.TestCase):
    def setUp(self):
        self.sha = hashlib.sha256(b'x').hexdigest()
        self.sites = set(range(1, 32)) | {1001}

    def parse(self, text, mode='instrumented', size=1):
        return capture.parse(text, mode, size, self.sha, 4, self.sites)

    def test_valid_and_error_with_no_access(self):
        self.assertEqual(self.parse(f'R OK {self.sha} {self.sha} 1 1 1 COMPLETE\nE 1 0\n')['event_count'], 1)
        self.assertEqual(self.parse(f'R ERROR_0_0 - {self.sha} 1 0 0 COMPLETE\n')['event_count'], 0)

    def test_rejects_unknown_site_overrun_and_bad_count(self):
        for event, count in [('E 32 0', '1 1'), ('E 1 1', '1 1'), ('E 1 0', '2 1')]:
            with self.subTest(event=event, count=count), self.assertRaises(ValueError):
                self.parse(f'R OK {self.sha} {self.sha} 1 {count} COMPLETE\n{event}\n')

    def test_rejects_empty_success_and_incomplete(self):
        for suffix in ['0 0 COMPLETE', '0 0 INCOMPLETE']:
            with self.assertRaises(ValueError): self.parse(f'R OK {self.sha} {self.sha} 1 {suffix}\n')

    def test_truncated_or_extra_blank_line_rejected(self):
        for end in ('', '\n\n'):
            with self.assertRaises(ValueError):
                self.parse(f'R OK {self.sha} {self.sha} 1 1 1 COMPLETE\nE 1 0' + end)

    def test_baseline_must_not_emit_events(self):
        with self.assertRaises(ValueError):
            self.parse(f'R OK {self.sha} {self.sha} 1 1 1 NOT_INSTRUMENTED\nE 1 0\n', 'baseline')

    def test_locked_reader_has_fixed_site_counts(self):
        self.assertEqual(prepare.FILES['reader.cpp'][2], 31)
        self.assertEqual(prepare.FILES['checked_leb128.hpp'][2], 1)
        self.assertEqual(len(prepare.READ.findall('bytes[a] + bytes[cursor++]')), 2)

    def test_changed_source_rejected_before_creating_output(self):
        source = prepare.ROOT / 'build' / 'locked-inputs' / 'DexMeguviz'
        if not source.is_dir(): self.skipTest('optional locked external source unavailable')
        with mock.patch.object(prepare.hashlib, 'sha256') as digest:
            digest.return_value.hexdigest.return_value = 'f' * 64
            with self.assertRaisesRegex(ValueError, 'source_identity_mismatch'):
                prepare.prepare(source, prepare.ROOT / 'build' / ('unused-dex-trace-negative-' + secrets.token_hex(8)))

    def test_smoke_input_read_is_bounded_before_validation(self):
        class Tracking(io.BytesIO):
            def __init__(self, data):
                super().__init__(data)
                self.requests = []
            def read(self, count=-1):
                self.requests.append(count)
                return super().read(count)
        oversized = Tracking(b'abcdefghij')
        with mock.patch.object(Path, 'open', return_value=oversized):
            with self.assertRaisesRegex(ValueError, 'input_size_invalid'):
                capture.read_input(Path('unused'), maximum=4)
        self.assertEqual(oversized.requests, [5])
        exact = Tracking(b'abcd')
        with mock.patch.object(Path, 'open', return_value=exact):
            self.assertEqual(capture.read_input(Path('unused'), maximum=4), b'abcd')
        self.assertEqual(exact.requests, [5])

    def test_input_hash_stops_on_growth_past_limit(self):
        class Growing:
            requests = []
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, count):
                self.requests.append(count)
                return b'ab' if len(self.requests) == 1 else b'cde'
        growing = Growing()
        with mock.patch.object(Path, 'open', return_value=growing):
            with self.assertRaisesRegex(ValueError, 'input_size_invalid'):
                capture.input_identity(Path('unused'), maximum=4)
        self.assertEqual(growing.requests, [5, 3])
        with mock.patch.object(Path, 'open', return_value=io.BytesIO(b'abcd')):
            self.assertEqual(capture.input_identity(Path('unused'), maximum=4), (4, hashlib.sha256(b'abcd').hexdigest()))

    def run_failing_child(self, program, expected, timeout=3):
        original = subprocess.Popen
        processes = []
        def start(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        before = time.monotonic()
        with mock.patch.object(capture.subprocess, 'Popen', side_effect=start):
            with self.assertRaisesRegex(ValueError, expected):
                capture.run_bounded([sys.executable, '-c', program], timeout, stdout_limit=1024, stderr_limit=1024)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll(), 'the spawned child must have exited')
        self.assertLess(time.monotonic() - before, timeout + 5)

    def test_stdout_limit_terminates_own_child(self):
        self.run_failing_child('import os,time;os.write(1,b"x"*65536);time.sleep(10)', 'process_stdout_limit')

    def test_stderr_limit_terminates_own_child(self):
        self.run_failing_child('import os,time;os.write(2,b"x"*65536);time.sleep(10)', 'process_stderr_limit')

    def test_timeout_terminates_own_child(self):
        self.run_failing_child('import time;time.sleep(10)', 'process_timeout', timeout=0.3)

    def test_both_pipes_are_drained_without_deadlock(self):
        program = 'import os;os.write(2,b"e"*16384);os.write(1,b"o"*16384)'
        code, stdout, stderr = capture.run_bounded([sys.executable, '-c', program], 3, 32768, 32768)
        self.assertEqual((code, len(stdout), len(stderr)), (0, 16384, 16384))


if __name__ == '__main__': unittest.main()
