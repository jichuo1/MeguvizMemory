#!/usr/bin/env python3
"""Bounded local pipe spooling; no Android or private inputs."""
import hashlib
import os
from pathlib import Path
import secrets
import subprocess
import sys
import unittest
from unittest import mock

import collector


class CollectorContracts(unittest.TestCase):
    def setUp(self):
        root = collector.paths.build_root()
        self.directory = root / ('stream-v2-collector-' + secrets.token_hex(12))
        self.directory.mkdir()
        self.target = self.directory / 'spool.bin'
        self.env = {'PATH': os.defpath}
        for key in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'):
            if key in os.environ: self.env[key] = os.environ[key]

    def run_child(self, program, *, limit=1024, total=1024, timeout=3, stderr_limit=1024):
        budget = collector.Budget(total)
        command = [sys.executable, '-E', '-s', '-S', '-c', program]
        return collector.run_spooled(command, self.target, max_bytes=limit, budget=budget,
                                     timeout=timeout, env=self.env, stderr_limit=stderr_limit), budget

    def test_exact_bound_and_identity(self):
        result, budget = self.run_child('import os;os.write(1,b"abcdef")', limit=6, total=6)
        self.assertEqual(self.target.read_bytes(), b'abcdef')
        self.assertEqual(result, {'bytes': 6, 'sha256': hashlib.sha256(b'abcdef').hexdigest(),
                                  'stderr_bytes': 0, 'exit_code': 0})
        self.assertEqual(budget.used, 6)

    def test_stdout_limit_is_incomplete_and_no_overwrite(self):
        with self.assertRaisesRegex(collector.SpoolError, 'SPOOL_BUDGET_EXCEEDED'):
            self.run_child('import os,time;os.write(1,b"x"*65536);time.sleep(10)', limit=16)
        self.assertLessEqual(self.target.stat().st_size, 16)
        with self.assertRaises(FileExistsError):
            self.run_child('print("not reached")')

    def test_aggregate_budget_applies_across_spools(self):
        budget = collector.Budget(7)
        cmd = [sys.executable, '-E', '-s', '-S', '-c', 'import os;os.write(1,b"1234")']
        collector.run_spooled(cmd, self.target, max_bytes=8, budget=budget, timeout=3, env=self.env)
        with self.assertRaisesRegex(collector.SpoolError, 'SPOOL_BUDGET_EXCEEDED'):
            collector.run_spooled(cmd, self.directory / 'second.bin', max_bytes=8, budget=budget, timeout=3, env=self.env)
        self.assertLessEqual(budget.used, 7)

    def test_stderr_flood_and_nonempty_are_incomplete(self):
        with self.assertRaisesRegex(collector.SpoolError, 'STDERR_LIMIT_EXCEEDED'):
            self.run_child('import os,time;os.write(2,b"e"*65536);time.sleep(10)', stderr_limit=16)
        with self.assertRaisesRegex(collector.SpoolError, 'CHILD_STDERR_NONEMPTY'):
            collector.run_spooled([sys.executable, '-E', '-s', '-S', '-c', 'import os;os.write(2,b"e")'],
                                  self.directory / 'second.bin', max_bytes=16, budget=collector.Budget(16), timeout=3, env=self.env)

    def test_nonzero_and_timeout_are_incomplete(self):
        with self.assertRaisesRegex(collector.SpoolError, 'CHILD_EXIT_NONZERO'):
            self.run_child('import os,sys;os.write(1,b"data");sys.exit(4)')
        with self.assertRaisesRegex(collector.SpoolError, 'CHILD_TIMEOUT'):
            collector.run_spooled([sys.executable, '-E', '-s', '-S', '-c', 'import time;time.sleep(10)'],
                                  self.directory / 'timeout.bin', max_bytes=16, budget=collector.Budget(16), timeout=.2, env=self.env)

    def test_completion_first_observed_after_deadline_is_incomplete(self):
        original = subprocess.Popen
        clock = [100.0]
        def completed_before_return(*args, **kwargs):
            child = original(*args, **kwargs)
            child.wait(timeout=3)
            clock[0] = 102.0
            return child
        # Synchronous drain makes the child and both pipes complete before the
        # first observation, whose fake clock is already beyond the deadline.
        with mock.patch.object(collector.subprocess, 'Popen', side_effect=completed_before_return), \
             mock.patch.object(collector.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(collector.threading.Thread, 'start', lambda thread: thread.run()), \
             mock.patch.object(collector.threading.Thread, 'join', lambda thread, timeout=None: None):
            with self.assertRaisesRegex(collector.SpoolError, 'CHILD_TIMEOUT'):
                self.run_child('pass', timeout=1)

    def test_file_close_crossing_deadline_is_incomplete(self):
        original = Path.open
        clock = [0.0]
        class LateClose:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args):
                result = self.stream.__exit__(*args)
                clock[0] = 2.0
                return result
            def write(self, data): return self.stream.write(data)
            def flush(self): return self.stream.flush()
            def fileno(self): return self.stream.fileno()
        def opening(path, mode='r', *args, **kwargs):
            stream = original(path, mode, *args, **kwargs)
            return LateClose(stream) if path == self.target and mode == 'xb' else stream
        with mock.patch.object(Path, 'open', opening), \
             mock.patch.object(collector.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(collector.SpoolError, 'CHILD_TIMEOUT'):
                self.run_child('pass', timeout=1)

    def test_output_failure_is_incomplete(self):
        with mock.patch.object(collector.os, 'fsync', side_effect=OSError('fixture')):
            with self.assertRaisesRegex(collector.SpoolError, 'SPOOL_IO_FAILED'):
                self.run_child('import os;os.write(1,b"data")')

    def test_short_disk_write_is_incomplete(self):
        original = Path.open
        class ShortWriter:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): return self.stream.__exit__(*args)
            def write(self, data): return self.stream.write(data[:-1])
            def flush(self): return self.stream.flush()
            def fileno(self): return self.stream.fileno()
        def opening(path, mode='r', *args, **kwargs):
            stream = original(path, mode, *args, **kwargs)
            return ShortWriter(stream) if path == self.target and mode == 'xb' else stream
        with mock.patch.object(Path, 'open', opening):
            with self.assertRaisesRegex(collector.SpoolError, 'SPOOL_WRITE_FAILED'):
                self.run_child('import os;os.write(1,b"data")')

    def test_both_pipes_drain_and_stderr_is_not_accepted(self):
        command = 'import os;os.write(2,b"e"*4096);os.write(1,b"o"*4096)'
        with self.assertRaisesRegex(collector.SpoolError, 'CHILD_STDERR_NONEMPTY'):
            self.run_child(command, limit=8192, total=8192, stderr_limit=8192)
        self.assertEqual(self.target.stat().st_size, 4096)

    def test_linked_parent_rejected(self):
        link = self.directory / 'linked'
        target = self.directory / 'real'
        target.mkdir()
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('creating symbolic links is unavailable')
        with self.assertRaisesRegex(ValueError, 'path_link_or_junction'):
            collector.run_spooled([sys.executable, '-c', 'pass'], link / 'x', max_bytes=1,
                                  budget=collector.Budget(1), timeout=3, env=self.env)

    def test_explicit_environment_rejects_credentials_and_injection(self):
        command = [sys.executable, '-E', '-s', '-S', '-c', 'pass']
        for key in ('PYTHONPATH', 'LD_PRELOAD', 'GITHUB_TOKEN'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'spool_arguments_invalid'):
                collector.run_spooled(command, self.target, max_bytes=8, budget=collector.Budget(8),
                                      timeout=3, env={**self.env, key: 'fixture'})
        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-parent-token'}):
            result, _ = self.run_child('import os,sys;sys.exit(1 if os.getenv("GITHUB_TOKEN") else 0)')
            self.assertEqual(result['exit_code'], 0)


if __name__ == '__main__':
    unittest.main()
