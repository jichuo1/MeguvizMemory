#!/usr/bin/env python3
"""Bounded private stdout spooling for the independent DEX stream profile.

This is a transport helper, not a trace validator or a success receipt.
Only a successfully exited child and later full frame validation can qualify
the spool. A failed or timed-out run leaves its partial file in build/.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_consumer as paths

BLOCK = 65536
MAX_STDERR = 262144
ALLOWED_ENV = frozenset({'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP',
                         'LD_LIBRARY_PATH', 'MGMEM_DEX_TRACE_MAX_EVENTS'})


class SpoolError(RuntimeError):
    """Fixed failure code; no child output or local path is exposed."""


class Budget:
    """Explicit aggregate byte budget for sequential or concurrent spools."""
    def __init__(self, maximum):
        if type(maximum) is not int or maximum < 1:
            raise ValueError('aggregate_budget_invalid')
        self.maximum = maximum
        self.used = 0
        self._lock = threading.Lock()

    def take(self, size):
        with self._lock:
            if type(size) is not int or size < 0 or size > self.maximum - self.used:
                return False
            self.used += size
            return True


def _stop_own_child(child):
    if child.poll() is not None:
        return
    try:
        child.terminate()
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
            child.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass


def run_spooled(command, target, *, max_bytes, budget, timeout, env, stderr_limit=MAX_STDERR):
    """Spool child stdout incrementally without trusting it as a valid trace.

    `target` is a fresh path below the non-linked workspace build directory.
    The caller must first verify the SDK and provide a minimal explicit env;
    the helper refuses unknown keys rather than inheriting the parent env.
    stdout and stderr are drained concurrently; no output contents are returned.
    Disk and event validation beyond byte budgets belong to the caller. A blocked
    filesystem call may exceed the cooperative deadline; descendants are never
    claimed stopped. This function never removes partial files or force-stops a
    remote process.
    """
    if (not isinstance(command, (list, tuple)) or not command or
            type(max_bytes) is not int or max_bytes < 1 or
            not isinstance(budget, Budget) or type(stderr_limit) is not int or
            stderr_limit < 0 or isinstance(timeout, bool) or
            not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600 or
            not isinstance(env, dict) or 'PATH' not in env or
            any(not isinstance(k, str) or k not in ALLOWED_ENV or
                not isinstance(v, str) or '\x00' in v for k, v in env.items()) or
            (os.name == 'nt' and 'LD_LIBRARY_PATH' in env)):
        raise ValueError('spool_arguments_invalid')
    root = paths.build_root()
    target = paths.confined(target, root)
    if not target.parent.is_dir():
        raise ValueError('spool_parent_missing')
    deadline = time.monotonic() + timeout
    failed = threading.Event()
    errors = []
    done = [threading.Event(), threading.Event()]
    result = {'bytes': 0, 'sha256': None, 'stderr_bytes': 0, 'exit_code': None}
    child = None
    threads = []

    # Opening before launch prevents overwriting any existing file, including a
    # dangling link. The file remains private and non-authoritative on failure.
    with target.open('xb') as output:
        try:
            child = subprocess.Popen([str(x) for x in command], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0, env=env)
        except OSError as error:
            raise SpoolError('CHILD_LAUNCH_FAILED') from error

        def fail(code):
            errors.append(code)
            failed.set()

        def read_stdout():
            digest = hashlib.sha256()
            count = 0
            try:
                while True:
                    block = os.read(child.stdout.fileno(), BLOCK)
                    if not block:
                        break
                    if len(block) > max_bytes - count or not budget.take(len(block)):
                        fail('SPOOL_BUDGET_EXCEEDED')
                        return
                    written = output.write(block)
                    if written != len(block):
                        fail('SPOOL_WRITE_FAILED')
                        return
                    count += written
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
                result['bytes'] = count
                result['sha256'] = digest.hexdigest()
            except BaseException:
                fail('SPOOL_IO_FAILED')
            finally:
                done[0].set()

        def read_stderr():
            count = 0
            try:
                while True:
                    block = os.read(child.stderr.fileno(), BLOCK)
                    if not block:
                        break
                    if len(block) > stderr_limit - count:
                        fail('STDERR_LIMIT_EXCEEDED')
                        return
                    count += len(block)
                result['stderr_bytes'] = count
            except BaseException:
                fail('STDERR_IO_FAILED')
            finally:
                done[1].set()

        try:
            for action in (read_stdout, read_stderr):
                worker = threading.Thread(target=action, daemon=True)
                worker.start()
                threads.append(worker)
            while True:
                if failed.is_set():
                    raise SpoolError(errors[0] if errors else 'SPOOL_IO_FAILED')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SpoolError('CHILD_TIMEOUT')
                if child.poll() is not None and all(event.is_set() for event in done):
                    break
                failed.wait(min(.05, remaining))
            if failed.is_set():
                raise SpoolError(errors[0] if errors else 'SPOOL_IO_FAILED')
            result['exit_code'] = child.returncode
            if child.returncode != 0:
                raise SpoolError('CHILD_EXIT_NONZERO')
            if result['stderr_bytes'] != 0:
                raise SpoolError('CHILD_STDERR_NONEMPTY')
            if time.monotonic() >= deadline:
                raise SpoolError('CHILD_TIMEOUT')
        except BaseException:
            _stop_own_child(child)
            raise
        finally:
            for worker in threads:
                worker.join(timeout=.25)
            for index, pipe in enumerate((child.stdout, child.stderr)):
                if pipe is not None and (index >= len(threads) or not threads[index].is_alive()):
                    pipe.close()
    # The final join and file close above can themselves cross the deadline;
    # observing completion only afterwards does not qualify as an on-time run.
    if time.monotonic() >= deadline:
        raise SpoolError('CHILD_TIMEOUT')
    return result
