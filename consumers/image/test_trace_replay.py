#!/usr/bin/env python3
"""Functional capture/replay and adversarial fixture tests, never performance."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import trace_replay as replay
import trace_runner


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_input(path, width, height):
    payload = bytes((n * 37 + 11) % 256 for n in range(3 * width * height))
    # Original P6 parser accepts 32-digit zero-prefixed values and comments.
    header = f'P6\n# synthetic fixture\n{width:032d} {height:032d}\n0255\n'.encode('ascii')
    path.write_bytes(header + payload)


def run(args):
    exe = args.trace_exe.resolve(strict=True)
    core = args.core.resolve(strict=True)
    runtime = args.runtime_dir.resolve(strict=True)
    output = args.out_dir.resolve(); output.mkdir(parents=True, exist_ok=False)
    runner = Path(__file__).resolve().with_name('trace_runner.py')
    positive = []
    for provider in ('direct', 'system', 'arena'):
        for layout in ('full', 'ring'):
            for close in (False, True):
                index = len(positive)
                width, height = ((1, 1), (4, 2), (7, 5))[index % 3]
                source = output / f'source-{index}.ppm'; make_input(source, width, height)
                target = output / f'case-{index}'
                command = [sys.executable, str(runner), '--capture-exe', str(exe), '--runtime-dir', str(runtime),
                           '--input', str(source), '--input-origin', 'SYNTHETIC', '--out-dir', str(target),
                           '--provider', provider, '--layout', layout, '--frames', '2' if close else '1',
                           '--max-events', '10000', '--timeout-seconds', '120']
                if provider != 'direct': command += ['--core', str(core)]
                if close: command.append('--close-race')
                proc = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
                assert proc.returncode == 0, (provider, layout, close, proc.stderr)
                result = json.loads(proc.stdout)
                assert result['state'] == 'PASS_BOUND_TRACE_FUNCTIONAL_REPLAY' and result['input_origin'] == 'SYNTHETIC'
                details = json.loads((target / 'replay-receipt.json').read_text(encoding='utf-8'))
                assert details['provider'] == provider and details['layout'] == layout and details['close_race'] is close
                assert details['replay_threads'] == 'SINGLE_THREAD_LOGICAL_ACTOR_ORDER_ONLY'
                assert details['loaded_module_identity'] == ('NOT_APPLICABLE' if provider == 'direct' else 'NOT_VERIFIED')
                assert details['core_use'] == ('NOT_APPLICABLE' if provider == 'direct' else 'REQUESTED_EXPLICIT_SHARED_CORE')
                positive.append(target)

    base = next(p for p in positive if json.loads((p/'capture-receipt.json').read_text())['provider'] == 'arena')
    original_trace = (base/'events/events.txt').read_bytes()
    original_input = (base/'input.ppm').read_bytes()
    original_receipt = json.loads((base/'capture-receipt.json').read_text(encoding='utf-8'))
    negative = 0

    def bad_trace(name, payload, *, rewrite_hash=True, expected=None):
        nonlocal negative
        directory = output / ('bad-' + name); directory.mkdir()
        trace = directory/'events.txt'; trace.write_bytes(payload)
        source = directory/'input.ppm'; source.write_bytes(original_input)
        receipt = dict(original_receipt)
        if rewrite_hash: receipt['trace_sha256'] = sha(payload)
        receipt_file = directory/'receipt.json'; receipt_file.write_text(json.dumps(receipt),encoding='utf-8')
        try:
            replay.replay(receipt_file, trace, source, core)
            raise AssertionError('invalid trace was accepted: ' + name)
        except (ValueError, OSError) as error:
            if expected: assert expected in str(error), (name, error)
            if name in ('sequence', 'range', 'duplicate'):
                assert getattr(error, 'cleanup_outcome', None) == 'CORE_CLOSED_ZERO_AND_DESTROYED'
            negative += 1

    lines = original_trace.splitlines(keepends=True)
    first = next(i for i, value in enumerate(lines) if value.startswith(b'E ') and b' RGB_READ ' in value)
    event = lines[first].decode('ascii').split(' ')
    bad = list(lines); bad[first] = (' '.join(event[:4] + ['0','999999999999','1\n'])).encode('ascii')
    bad_trace('range', b''.join(bad), expected='trace_event_sequence_or_range')
    bad = list(lines); bad[first], bad[first+1] = bad[first+1], bad[first]
    bad_trace('sequence', b''.join(bad), expected='trace_event_syntax')
    bad = list(lines); bad.insert(first, bad[first])
    bad_trace('duplicate', b''.join(bad))
    bad_trace('missing-footer', b''.join(lines[:-1]))
    bad_trace('truncated', original_trace[:-8])
    bad_trace('trailing', original_trace + b'extra\n')
    bad_trace('hash-mismatch', original_trace + b'x', rewrite_hash=False, expected='capture_identity_mismatch')
    bad = list(lines); bad[2] = b'I ' + b'0'*64 + b'\n'
    bad_trace('rgb-digest', b''.join(bad), expected='trace_rgb_identity_mismatch')
    bad = list(lines); bad[-1] = bad[-1].replace(b'END COMPLETE', b'END INCOMPLETE')
    bad_trace('incomplete', b''.join(bad), expected='trace_footer_mismatch')

    def bad_receipt(name, mutate):
        nonlocal negative
        path = output / ('bad-receipt-' + name + '.json')
        value = dict(original_receipt); mutate(value); path.write_text(json.dumps(value),encoding='utf-8')
        try:
            replay.replay(path, base/'events/events.txt', base/'input.ppm', core)
            raise AssertionError('invalid receipt was accepted: ' + name)
        except ValueError:
            negative += 1
    bad_receipt('missing-version', lambda x: x.pop('schema_version'))
    bad_receipt('future-version', lambda x: x.update(schema_version=999))
    bad_receipt('bool-version', lambda x: x.update(schema_version=True))
    bad_receipt('bool-exit', lambda x: x.update(capture_exit_code=False))
    bad_receipt('bool-width', lambda x: x.update(width=True))
    bad_receipt('bool-event-limit', lambda x: x.update(trace_max_events=True))
    bad_receipt('incorrect-input', lambda x: x.update(input_sha256='0'*64))
    bad_receipt('capture-incomplete', lambda x: x.update(trace_status='INCOMPLETE'))
    bad_receipt('invalid-hash', lambda x: x.update(trace_sha256='not-hash'))

    captured_report = json.loads((base/'capture.stdout').read_text(encoding='utf-8'))
    parameters = (original_receipt['width'], original_receipt['height'], original_receipt['frames'],
                  original_receipt['provider'], original_receipt['layout'], original_receipt['close_race'],
                  captured_report['checksum'])
    trace_runner.validate_capture_report(captured_report, *parameters)
    for name, value in (('schema_version', True), ('checksum', False), ('checksum', captured_report['checksum']+1),
                        ('gray_conversions', False), ('error', 'contradiction'), ('success', False),
                        ('performance_qualification', 'PASS'),
                        ('closed_backing_bytes', False), ('whole_allocation_exclusive', False),
                        ('close_while_leased_verified', not original_receipt['close_race'])):
        invalid_report = dict(captured_report); invalid_report[name] = value
        try:
            trace_runner.validate_capture_report(invalid_report, *parameters)
            raise AssertionError('contradictory report accepted: ' + name)
        except ValueError:
            negative += 1

    for provider in ('direct', 'arena'):
        selected = next(p for p in positive if json.loads((p/'capture-receipt.json').read_text())['provider'] == provider)
        capture_info = json.loads((selected/'capture-receipt.json').read_text())
        worker_report = json.loads((selected/'replay.stdout').read_text())
        replay_parameters = (capture_info['trace_sha256'], capture_info['input_sha256'], provider,
                             capture_info['layout'], capture_info['frames'], capture_info['close_race'],
                             worker_report['checksum'], None if provider == 'direct' else replay.file_hash(core, replay.MAX_CORE))
        trace_runner.validate_replay_report(worker_report, *replay_parameters)
        for name, value in (('requested_core_file_sha256', '0'*64), ('loaded_module_identity', 'VERIFIED'),
                            ('core_use', 'WRONG'), ('checksum', False), ('replay_threads', 'PARALLEL'),
                            ('device', 'PASS'), ('tlb_mechanism', 'ESTABLISHED')):
            invalid_report = dict(worker_report); invalid_report[name] = value
            try:
                trace_runner.validate_replay_report(invalid_report, *replay_parameters)
                raise AssertionError('contradictory worker report accepted: ' + name)
            except ValueError:
                negative += 1

    # External watchdog kills only a spawned diagnostic child; it never means
    # a domain was observed as normally closed.
    _, _, _, timed_out, overflow = trace_runner.bounded_capture(
        [sys.executable, '-c', 'import time; time.sleep(2)'], os.environ.copy(), time.monotonic() + 0.1)
    assert timed_out and not overflow
    negative += 1

    def runner_args(name):
        return SimpleNamespace(capture_exe=exe, core=None, runtime_dir=runtime,
                               input=output/'source-0.ppm', input_origin='SYNTHETIC',
                               out_dir=output/name, provider='direct', layout='full', frames=1,
                               close_race=False, max_events=10000, timeout_seconds=120)
    original_bounded = trace_runner.bounded_capture
    def capture_timeout(_command, _environment, _deadline):
        return 1, b'', b'', True, False
    args_timeout = runner_args('capture-timeout')
    with patch.object(trace_runner, 'bounded_capture', capture_timeout):
        try:
            trace_runner.run(args_timeout)
            raise AssertionError('capture timeout accepted')
        except trace_runner.RunFailure:
            status = json.loads((args_timeout.out_dir/'status.json').read_text(encoding='utf-8'))
            assert status['state'] == 'INCOMPLETE_OR_FAILED' and status['cleanup_outcome'] == 'UNKNOWN_CAPTURE_PROCESS_TERMINATED'
            negative += 1
    calls = [0]
    def replay_timeout(command, environment, deadline):
        calls[0] += 1
        if calls[0] == 1: return original_bounded(command, environment, deadline)
        return 1, b'', b'', True, False
    args_timeout = runner_args('replay-timeout')
    with patch.object(trace_runner, 'bounded_capture', replay_timeout):
        try:
            trace_runner.run(args_timeout)
            raise AssertionError('replay timeout accepted')
        except trace_runner.RunFailure:
            status = json.loads((args_timeout.out_dir/'status.json').read_text(encoding='utf-8'))
            assert status['state'] == 'INCOMPLETE_OR_FAILED' and status['cleanup_outcome'] == 'UNKNOWN_PROCESS_TERMINATED'
            assert not (args_timeout.out_dir/'replay-receipt.json').exists()
            negative += 1

    calls = [0]
    def invalid_worker_output(command, environment, deadline):
        calls[0] += 1
        if calls[0] == 1: return original_bounded(command, environment, deadline)
        return 0, b'not-json', b'', False, False
    args_invalid = runner_args('invalid-worker-output')
    with patch.object(trace_runner, 'bounded_capture', invalid_worker_output):
        try:
            trace_runner.run(args_invalid)
            raise AssertionError('invalid replay output accepted')
        except ValueError:
            status = json.loads((args_invalid.out_dir/'status.json').read_text(encoding='utf-8'))
            assert status['cleanup_outcome'] == 'UNKNOWN_REPLAY_PROCESS_STATE'
            negative += 1

    post_input = output/'posthash-source.ppm'; make_input(post_input, 1, 1)
    calls = [0]
    def change_after_replay(command, environment, deadline):
        calls[0] += 1
        result = original_bounded(command, environment, deadline)
        if calls[0] == 2 and result[0] == 0:
            post_input.write_bytes(post_input.read_bytes()[:-1] + b'\x00')
        return result
    args_changed = runner_args('changed-after-replay'); args_changed.input = post_input
    with patch.object(trace_runner, 'bounded_capture', change_after_replay):
        try:
            trace_runner.run(args_changed)
            raise AssertionError('changed original input accepted')
        except ValueError as error:
            assert str(error) == 'input_changed_after_replay'
            status = json.loads((args_changed.out_dir/'status.json').read_text(encoding='utf-8'))
            assert status['cleanup_outcome'] == 'REPLAY_PROCESS_REPORTED_CLOSED_NOT_INDEPENDENTLY_VERIFIED'
            negative += 1

    # Simulate A→B→A path reads: pre/post path hashes see A but actual P6
    # reader gets B. A pixel changes 0→1 in red, which can leave gray=0, so
    # relying on output equality alone would miss the substitution.
    identity_dir = output/'identity-swap'; identity_dir.mkdir()
    identity_input = identity_dir/'input.ppm'; make_input(identity_input, 1, 1)
    original = identity_input.read_bytes()
    changed = bytearray(original); changed[-3:] = bytes((0, 0, 0))
    clean = bytearray(original); clean[-3:] = bytes((1, 0, 0))
    identity_input.write_bytes(clean)
    assert replay.oracle(1, 1, bytes(changed[-3:])) == replay.oracle(1, 1, bytes(clean[-3:]))
    real_open = Path.open
    read_count = [0]
    def swapped(path, *args, **kwargs):
        if path == identity_input and args and args[0] == 'rb':
            read_count[0] += 1
            if read_count[0] == 2: return io.BytesIO(changed)
        return real_open(path, *args, **kwargs)
    with patch.object(Path, 'open', swapped):
        expected_sha = replay.file_hash(identity_input, replay.MAX_INPUT)
        try:
            replay.read_ppm(identity_input, expected_sha)
            raise AssertionError('read substitution accepted')
        except ValueError as error:
            assert str(error) == 'input_consumed_identity_mismatch'
            assert replay.file_hash(identity_input, replay.MAX_INPUT) == expected_sha
            negative += 1

    # Path hash sees trace A, parser consumes B with a different but otherwise
    # valid H event-limit, and a later path read would again see A.  Match the
    # fixture receipt's H value to B while retaining A's hash so the streaming
    # consumed-byte digest, rather than semantic syntax, detects substitution.
    trace_swap_dir = output/'trace-identity-swap'; trace_swap_dir.mkdir()
    swapped_trace = trace_swap_dir/'events.txt'; swapped_trace.write_bytes(original_trace)
    swapped_input = trace_swap_dir/'input.ppm'; swapped_input.write_bytes(original_input)
    changed_lines = list(lines)
    header = changed_lines[1].decode('ascii').strip().split(' ')
    assert header[-1] == '10000'; header[-1] = '10001'
    changed_lines[1] = (' '.join(header) + '\n').encode('ascii')
    changed_trace = b''.join(changed_lines)
    swapped_receipt = dict(original_receipt); swapped_receipt['trace_max_events'] = 10001
    swapped_receipt_path = trace_swap_dir/'receipt.json'
    swapped_receipt_path.write_text(json.dumps(swapped_receipt),encoding='utf-8')
    trace_reads = [0]
    def swapped_stream(path, *args, **kwargs):
        if path == swapped_trace and args and args[0] == 'rb':
            trace_reads[0] += 1
            if trace_reads[0] == 2: return io.BytesIO(changed_trace)
        return real_open(path, *args, **kwargs)
    with patch.object(Path, 'open', swapped_stream):
        try:
            replay.replay(swapped_receipt_path, swapped_trace, swapped_input, core)
            raise AssertionError('trace read substitution accepted')
        except replay.ReplayFailure as error:
            assert str(error) == 'trace_consumed_identity_mismatch'
            assert error.cleanup_outcome == 'CORE_ALREADY_CLOSED_AND_DESTROYED'
            negative += 1

    class FakeCore:
        def __init__(self, status, invalid=False, throw=False):
            self.status, self.invalid, self.throw, self.release_calls = status, invalid, throw, 0
        def mgmem_borrow_acquire_v1(self, _d, _owner, _offset, _length, _access, output):
            if self.throw: raise RuntimeError('injected')
            if self.invalid:
                value = ctypes.cast(output, ctypes.POINTER(replay.Borrow)).contents
                value.lease = replay.Handle(1, 9, 3); value.data = 1; value.length = 1
            return self.status
        def mgmem_borrow_release_v1(self, *_):
            self.release_calls += 1; return 0
    for name, status, invalid, throw, operation in (
            ('success-malformed', 0, False, False, 'ACQUIRE'),
            ('busy-dirty', 6, True, False, 'ACQUIRE_BUSY'),
            ('busy-unexpected-success', 0, True, False, 'ACQUIRE_BUSY'),
            ('call-exception', 0, False, True, 'ACQUIRE')):
        fake = FakeCore(status, invalid, throw)
        workspace = replay.ReplayWorkspace(fake, 'arena', (1,1,1))
        workspace.domain = ctypes.c_void_p(1); workspace.owners[1] = replay.Handle(1,2,3)
        try:
            workspace.apply('M', operation, 1, 0, 1, b'', {})
            raise AssertionError('fault accepted: ' + name)
        except (ValueError, RuntimeError):
            assert workspace.uncertain and workspace.cleanup() == 'UNKNOWN_QUARANTINED'
            assert fake.release_calls == 0
            negative += 1

    print(json.dumps({'state':'PASS_SCOPED_CAPTURE_AND_REPLAY', 'positive':len(positive),
                      'negative':negative, 'fixture':'SYNTHETIC_INPUT_REAL_IMAGE_ALGORITHM',
                      'device':'NOT_RUN','performance':'NOT_MEASURED'}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace-exe', type=Path, required=True)
    parser.add_argument('--core', type=Path, required=True)
    parser.add_argument('--runtime-dir', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    run(parser.parse_args())


if __name__ == '__main__':
    main()
