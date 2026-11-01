#!/usr/bin/env python3
"""Capture and replay one bounded image task; artifacts remain local in build/.

The P6 input may be private. This tool never publishes it or treats its own
instrumentation as performance, Android device, or hardware mechanism evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import trace_replay as replay

ROOT = Path(__file__).resolve().parents[2]
MAX_OUTPUT = 64 * 1024
WORKER = Path(__file__).resolve().with_name('trace_replay_worker.py')
REPLAY_SOURCE = Path(__file__).resolve().with_name('trace_replay.py')


class RunFailure(ValueError):
    def __init__(self, reason, cleanup_outcome, cleanup_outcome_source='RUNNER_OBSERVATION'):
        super().__init__(reason)
        self.cleanup_outcome = cleanup_outcome
        self.cleanup_outcome_source = cleanup_outcome_source


def write_new(path, payload):
    with path.open('xb') as target:
        target.write(payload)
        target.flush()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def validate_capture_report(result, width, height, frames, provider, layout, close_race, checksum):
    replay.require(isinstance(result, dict) and type(result.get('schema_version')) is int and
                   result['schema_version'] == 1 and result.get('consumer') == 'image_workspace_v2' and
                   result.get('scope') == 'HOST_FUNCTIONAL_RESOURCE_DIAGNOSTIC' and
                   result.get('performance_qualification') == 'NOT_STARTED' and result.get('error') == '' and
                   result.get('success') is True and result.get('trace_status') == 'COMPLETE' and
                   type(result.get('width')) is int and result['width'] == width and
                   type(result.get('height')) is int and result['height'] == height and
                   type(result.get('frames')) is int and result['frames'] == frames and
                   type(result.get('completed_frames')) is int and result['completed_frames'] == frames and
                   result.get('provider') == provider and result.get('layout') == layout and
                   result.get('close_race_requested') is close_race and result.get('closed') is True and
                   result.get('cleanup_ok') is True and type(result.get('closed_backing_bytes')) is int and
                   result['closed_backing_bytes'] == 0 and type(result.get('pixels')) is int and
                   result['pixels'] == width * height and type(result.get('gray_conversions')) is int and
                   result['gray_conversions'] == width * height * frames and
                   type(result.get('checksum')) is int and result['checksum'] == checksum and
                   'whole_allocation_exclusive' in result and 'close_while_leased_verified' in result and
                   (result.get('whole_allocation_exclusive') is None if provider == 'direct' else
                    result.get('whole_allocation_exclusive') is True) and
                   (result.get('close_while_leased_verified') is None if provider == 'direct' else
                    result.get('close_while_leased_verified') is close_race), 'capture_report_mismatch')


def validate_replay_report(result, trace_sha, input_sha, provider, layout, frames, close_race, checksum, core_sha):
    replay.require(isinstance(result, dict) and result.get('state') == 'PASS_BOUND_TRACE_FUNCTIONAL_REPLAY' and
                   type(result.get('events')) is int and result['events'] > 0 and
                   result.get('trace_sha256') == trace_sha and result.get('input_sha256') == input_sha and
                   result.get('provider') == provider and result.get('layout') == layout and
                   type(result.get('frames')) is int and result['frames'] == frames and
                   result.get('close_race') is close_race and type(result.get('checksum')) is int and
                   result['checksum'] == checksum and result.get('requested_core_file_sha256') == core_sha and
                   result.get('loaded_module_identity') == ('NOT_APPLICABLE' if core_sha is None else 'NOT_VERIFIED') and
                   result.get('core_use') == ('NOT_APPLICABLE' if core_sha is None else 'REQUESTED_EXPLICIT_SHARED_CORE') and
                   result.get('replay_threads') == 'SINGLE_THREAD_LOGICAL_ACTOR_ORDER_ONLY' and
                   result.get('source_scope') == 'INSTRUMENTED_SOURCE_SEMANTIC_OPERATIONS_ONLY' and
                   result.get('device') == 'NOT_RUN' and result.get('tlb_mechanism') == 'NOT_ESTABLISHED' and
                   result.get('performance') == 'NOT_MEASURED', 'replay_report_mismatch')


def bounded_capture(command, environment, deadline):
    """Bound child output and timeout; only the exact child may be terminated."""
    process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()
    io_failed = threading.Event()
    def consume(stream, target):
        try:
            while True:
                part = stream.read(4096)
                if not part: break
                remaining = MAX_OUTPUT - len(target)
                if remaining > 0: target.extend(part[:remaining])
                if len(part) > remaining:
                    overflow.set()
                    try: process.kill()
                    except OSError: pass
        except (OSError, ValueError):
            io_failed.set()
            try: process.kill()
            except OSError: pass
        finally:
            try: stream.close()
            except OSError: io_failed.set()
    threads = [threading.Thread(target=consume, args=(process.stdout, buffers[0]), daemon=True),
               threading.Thread(target=consume, args=(process.stderr, buffers[1]), daemon=True)]
    try:
        for worker in threads: worker.start()
        timed_out = False
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            try: process.kill()
            except OSError:
                if process.poll() is None: raise
            process.wait(timeout=5)
        for worker in threads: worker.join(5)
        if any(worker.is_alive() for worker in threads) or io_failed.is_set():
            raise RunFailure('child_output_drain_unknown', 'UNKNOWN_PROCESS_STATE')
        return process.returncode, bytes(buffers[0]), bytes(buffers[1]), timed_out, overflow.is_set()
    except BaseException as error:
        try:
            if process.poll() is None: process.kill()
            process.wait(timeout=5)
        except BaseException:
            pass
        raise RunFailure('child_control_unknown', 'UNKNOWN_PROCESS_STATE') from error


def run(args):
    started = time.monotonic()
    deadline = started + args.timeout_seconds
    root = ROOT.resolve(strict=True)
    build = (root / 'build').resolve(strict=True)
    replay.require(build.is_relative_to(root), 'build_root_outside_workspace')
    out = args.out_dir.resolve()
    replay.require(out != build and out.is_relative_to(build) and not out.exists(), 'output_not_fresh_build_child')
    capture = args.capture_exe.resolve(strict=True)
    source = args.input.resolve(strict=True)
    replay.require(capture.is_file() and source.is_file(), 'input_or_capture_missing')
    if args.provider != 'direct':
        replay.require(args.core is not None and args.core.is_file(), 'core_path_required')
    out.mkdir(parents=True, exist_ok=False)
    capture_attempted = False
    capture_reported_cleanup = False
    replay_attempted = False
    replay_reported_cleanup = False
    try:
        capture_sha = replay.file_hash(capture, replay.MAX_CORE, deadline)
        original_sha = replay.file_hash(source, replay.MAX_INPUT, deadline)
        with source.open('rb') as stream:
            data = stream.read(replay.MAX_INPUT + 1)
            replay.require(len(data) <= replay.MAX_INPUT and stream.read(1) == b'', 'input_file_limit')
        replay.require(hashlib.sha256(data).hexdigest() == original_sha, 'source_consumed_identity_mismatch')
        snapshot = out / 'input.ppm'; write_new(snapshot, data)
        replay.require(replay.file_hash(snapshot, replay.MAX_INPUT, deadline) == original_sha, 'snapshot_identity_mismatch')
        width, height, rgb = replay.read_ppm(snapshot, original_sha)
        rgb_sha = hashlib.sha256(rgb).hexdigest()
        expected_pixels = replay.oracle(width, height, rgb, deadline)
        expected_output = f'P5\n{width} {height}\n255\n'.encode('ascii') + expected_pixels
        output = out / 'capture-output.pgm'
        trace_dir = out / 'events'
        command = [str(capture), str(snapshot), str(output), '--provider', args.provider,
                   '--layout', args.layout, '--frames', str(args.frames)]
        if args.close_race: command.append('--close-race')
        command += ['--trace-dir', str(trace_dir), '--trace-max-events', str(args.max_events)]
        environment = os.environ.copy()
        if args.runtime_dir:
            runtime = args.runtime_dir.resolve(strict=True)
            replay.require(runtime.is_dir(), 'runtime_directory_missing')
            variable = 'PATH' if os.name == 'nt' else 'LD_LIBRARY_PATH'
            environment[variable] = str(runtime) + os.pathsep + environment.get(variable, '')
        capture_attempted = True
        exit_code, stdout, stderr, timed_out, overflow = bounded_capture(command, environment, deadline)
        write_new(out / 'capture.stdout', stdout); write_new(out / 'capture.stderr', stderr)
        if timed_out or overflow:
            raise RunFailure('capture_process_timeout_or_output_limit', 'UNKNOWN_CAPTURE_PROCESS_TERMINATED')
        if exit_code != 0:
            raise RunFailure('capture_process_incomplete', 'UNKNOWN_CAPTURE_PROCESS_EXIT')
        result = replay.strict_json(stdout.decode('utf-8'))
        validate_capture_report(result, width, height, args.frames, args.provider, args.layout,
                                args.close_race, sum(expected_pixels) * args.frames)
        capture_reported_cleanup = True
        with output.open('rb') as stream:
            actual = stream.read(len(expected_output) + 1)
            replay.require(actual == expected_output and stream.read(1) == b'', 'capture_oracle_mismatch')
        trace = trace_dir / 'events.txt'
        trace_sha = replay.file_hash(trace, replay.MAX_TRACE, deadline)
        replay.require(replay.file_hash(source, replay.MAX_INPUT, deadline) == original_sha and
                       replay.file_hash(snapshot, replay.MAX_INPUT, deadline) == original_sha and
                       replay.file_hash(capture, replay.MAX_CORE, deadline) == capture_sha,
                       'capture_input_changed')
        # Check the payload digest reported by the instrumented consumer itself.
        with trace.open('rb') as raw:
            prefix = replay.HashingReader(raw)
            replay.require(replay.line(prefix) == 'MGMEM_IMAGE_TRACE_V1', 'trace_magic')
            header = replay.line(prefix).split(' ')
            identity = replay.line(prefix).split(' ')
            replay.require(len(header) == 8 and header[0] == 'H' and
                           header[1:7] == [str(width), str(height), str(args.frames), args.provider,
                                            args.layout, '1' if args.close_race else '0'] and
                           replay.uint(header[7], replay.MAX_EVENTS) == args.max_events and
                           identity == ['I', rgb_sha], 'capture_payload_identity_mismatch')
        receipt = {'schema_version': 1, 'state': 'CAPTURE_PROCESS_AND_IDENTITY_CONFIRMED', 'capture_exit_code': 0,
                   'capture_success': True, 'trace_status': 'COMPLETE', 'requested_capture_file_sha256': capture_sha,
                   'capture_executable_identity': 'PATH_PRE_POST_ONLY',
                   'input_sha256': original_sha, 'rgb_sha256': rgb_sha, 'trace_sha256': trace_sha,
                   'output_sha256': hashlib.sha256(expected_output).hexdigest(),
                   'capture_stdout_sha256': hashlib.sha256(stdout).hexdigest(),
                   'width': width, 'height': height, 'frames': args.frames, 'provider': args.provider,
                   'layout': args.layout, 'close_race': args.close_race, 'trace_max_events': args.max_events,
                   'input_file_scope': 'RUNNER_SNAPSHOT_PRE_POST_HASH_CAPTURE_HEADER_BYTES_NOT_ATTESTED',
                   'decoded_payload_scope': 'CAPTURE_RGB_AND_DIMENSIONS_MATCH_RUNNER_SNAPSHOT',
                   'input_origin': args.input_origin,
                   'event_structure': 'NOT_YET_VALIDATED_REPLAY_REQUIRED',
                   'trace_scope': 'INSTRUMENTED_SOURCE_SEMANTIC_OPERATIONS_ONLY',
                   'device': 'NOT_RUN', 'performance': 'NOT_MEASURED'}
        receipt_path = out / 'capture-receipt.json'; write_new(receipt_path, json_bytes(receipt))
        worker_sha = replay.file_hash(WORKER, replay.MAX_INPUT, deadline)
        replay_sha = replay.file_hash(REPLAY_SOURCE, replay.MAX_INPUT, deadline)
        selected_core = None if args.provider == 'direct' else args.core.resolve(strict=True)
        selected_core_sha = None if selected_core is None else replay.file_hash(selected_core, replay.MAX_CORE, deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RunFailure('replay_deadline_before_start', 'NOT_STARTED')
        worker_command = [sys.executable, str(WORKER), '--receipt', str(receipt_path), '--trace', str(trace),
                          '--input', str(snapshot), '--timeout-seconds', str(remaining)]
        if selected_core is not None: worker_command += ['--core', str(selected_core)]
        replay_attempted = True
        worker_exit, worker_stdout, worker_stderr, worker_timeout, worker_overflow = bounded_capture(
            worker_command, environment, deadline)
        write_new(out / 'replay.stdout', worker_stdout); write_new(out / 'replay.stderr', worker_stderr)
        if worker_timeout or worker_overflow:
            raise RunFailure('replay_process_timeout_or_output_limit', 'UNKNOWN_PROCESS_TERMINATED')
        if worker_exit != 0:
            try:
                error_result = replay.strict_json(worker_stderr.decode('utf-8'))
                outcome = error_result.get('cleanup_outcome', 'UNKNOWN_OR_UNAVAILABLE')
                if outcome not in ('NOT_STARTED', 'NOT_STARTED_OR_UNAVAILABLE', 'UNKNOWN_QUARANTINED',
                                   'CORE_ALREADY_CLOSED_AND_DESTROYED', 'CORE_CLOSED_ZERO_AND_DESTROYED',
                                   'DIRECT_REFERENCES_CLEARED_NOT_OS_RECLAIM_PROOF'):
                    outcome = 'UNKNOWN_OR_UNAVAILABLE'
            except Exception:
                outcome = 'UNKNOWN_OR_UNAVAILABLE'
            raise RunFailure('replay_process_failed', outcome, 'REPLAY_WORKER_SELF_REPORT_NOT_INDEPENDENTLY_VERIFIED')
        result = replay.strict_json(worker_stdout.decode('utf-8'))
        validate_replay_report(result, trace_sha, original_sha, args.provider, args.layout, args.frames,
                               args.close_race, sum(expected_pixels) * args.frames, selected_core_sha)
        replay_reported_cleanup = True
        replay.require(replay.file_hash(source, replay.MAX_INPUT, deadline) == original_sha and
                       replay.file_hash(capture, replay.MAX_CORE, deadline) == capture_sha and
                       replay.file_hash(WORKER, replay.MAX_INPUT, deadline) == worker_sha and
                       replay.file_hash(REPLAY_SOURCE, replay.MAX_INPUT, deadline) == replay_sha and
                       (selected_core is None or replay.file_hash(selected_core, replay.MAX_CORE, deadline) == selected_core_sha),
                       'input_changed_after_replay')
        result['capture_receipt_sha256'] = replay.file_hash(receipt_path, 16384, deadline)
        result['replay_worker_source_sha256'] = worker_sha
        result['replay_source_sha256'] = replay_sha
        result['replay_source_identity'] = 'REQUESTED_PATH_PRE_POST_HASH_ONLY'
        result['replay_worker_stdout_sha256'] = hashlib.sha256(worker_stdout).hexdigest()
        result['input_origin'] = args.input_origin
        result['capture_loaded_module_identity'] = 'NOT_VERIFIED'
        result['capture_executable_identity'] = 'PATH_PRE_POST_ONLY'
        result['input_file_scope'] = receipt['input_file_scope']
        result['decoded_payload_scope'] = receipt['decoded_payload_scope']
        write_new(out / 'replay-receipt.json', json_bytes(result))
        write_new(out / 'status.json', json_bytes({'state': 'PASS_BOUND_TRACE_FUNCTIONAL_REPLAY',
                                                     'performance': 'NOT_MEASURED', 'device': 'NOT_RUN'}))
        return result
    except BaseException as error:
        reason = type(error).__name__
        message = str(error)
        if message and len(message) <= 100 and all(c.isascii() and (c.isalnum() or c == '_') for c in message):
            reason = message
        try:
            default_cleanup = ('REPLAY_PROCESS_REPORTED_CLOSED_NOT_INDEPENDENTLY_VERIFIED' if replay_reported_cleanup else
                               'UNKNOWN_REPLAY_PROCESS_STATE' if replay_attempted else
                               'CAPTURE_PROCESS_REPORTED_CLOSED_NOT_INDEPENDENTLY_VERIFIED' if capture_reported_cleanup else
                               'UNKNOWN_CAPTURE_PROCESS_STATE' if capture_attempted else 'NOT_STARTED')
            write_new(out / 'status.json', json_bytes({'state': 'INCOMPLETE_OR_FAILED', 'reason': reason,
                                                       'cleanup_outcome': getattr(error, 'cleanup_outcome', default_cleanup),
                                                       'cleanup_outcome_source': getattr(error, 'cleanup_outcome_source', 'RUNNER_OBSERVATION'),
                                                       'performance': 'NOT_MEASURED', 'device': 'NOT_RUN'}))
        except OSError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-exe', type=Path, required=True)
    parser.add_argument('--core', type=Path, help='Explicit shared core required for system/arena replay; not loaded for direct replay')
    parser.add_argument('--runtime-dir', type=Path, help='Capture child dynamic-library directory')
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--input-origin', choices=('SYNTHETIC', 'USER_SUPPLIED'), required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--provider', choices=('direct', 'system', 'arena'), required=True)
    parser.add_argument('--layout', choices=('full', 'ring'), required=True)
    parser.add_argument('--frames', type=int, default=1)
    parser.add_argument('--close-race', action='store_true')
    parser.add_argument('--max-events', type=int, default=250000)
    parser.add_argument('--timeout-seconds', type=int, default=1200)
    args = parser.parse_args()
    if not 1 <= args.frames <= 32 or not 1 <= args.max_events <= replay.MAX_EVENTS or not 1 <= args.timeout_seconds <= 7200:
        parser.error('frames, max-events or timeout out of bounds')
    try:
        result = run(args)
        print(json.dumps({'state': result['state'], 'events': result['events'],
                          'input_origin': result['input_origin'], 'device': 'NOT_RUN',
                          'performance': 'NOT_MEASURED'}))
        return 0
    except Exception as error:
        reason = str(error)
        if not reason or len(reason) > 100 or not all(c.isascii() and (c.isalnum() or c == '_') for c in reason):
            reason = type(error).__name__
        print(json.dumps({'state': 'INCOMPLETE_OR_FAILED', 'reason': reason}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
