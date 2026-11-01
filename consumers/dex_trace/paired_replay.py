#!/usr/bin/env python3
"""Capture and replay bounded DEX reader offsets; all outputs remain private.

This process itself starts the paired capture and verifies its successful exit.
An older receipt or an externally supplied trace cannot be promoted to replay
qualification through this entry point.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import sys
import time

import capture
import paired_compare as paired
import prepare
import prepare_consumer

ROOT = prepare.ROOT
WORKER = ROOT / 'consumers/dex_trace/paired_replay_worker.py'
PRODUCER = ROOT / 'consumers/dex_trace/paired_compare.py'
IMAGE_CORE_HELPER = ROOT / 'consumers/image/trace_replay.py'
MAX_CONTROL_OUTPUT = 65536
EMPTY_HASH = hashlib.sha256(b'').hexdigest()
PAIR_SCOPE = 'SNAPSHOT_OPEN_CALLER_THREAD_METADATA_READER_ONLY'
REPLAY_SCOPE = 'BOUND_READER_BYTE_READS_SINGLE_THREAD_SYNTHETIC_LIFECYCLE'


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def write_new(path, data):
    with Path(path).open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def publish_receipt(path, data):
    """Publish only a fully fsynced file, without replacing an existing path."""
    path = Path(path)
    pending = path.with_suffix('.pending')
    write_new(pending, data)
    if os.name == 'nt':
        # Python's MoveFile-based rename rejects an existing destination.
        os.rename(pending, path)
    else:
        prepare.require(sys.platform.startswith('linux'), 'atomic_noreplace_unsupported')
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, 'renameat2', None)
        prepare.require(rename is not None, 'atomic_noreplace_unsupported')
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(pending), -100, os.fsencode(path), 1) != 0:
            raise OSError(ctypes.get_errno(), 'atomic_noreplace_failed')
    prepare.require(prepare_consumer.read_regular(path, len(data)) == data, 'receipt_readback_mismatch')


def child_environment(environ=None):
    """Do not load an SDK or inherit Python/loader injections at interpreter startup."""
    source = os.environ if environ is None else environ
    if os.name == 'nt':
        system_root = source.get('SYSTEMROOT') or source.get('WINDIR')
        env = {'PATH': str(Path(system_root) / 'System32') if system_root else ''}
    else:
        env = {'PATH': '/usr/bin:/bin'}
    for key in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'):
        if key in source:
            env[key] = source[key]
    return env


def python_command(script, *args):
    # -E ignores Python env injections; -s/-S disable user and global site
    # initialization while retaining the script directory for local imports.
    return [sys.executable, '-E', '-s', '-S', str(script), *map(str, args)]


def verified_file(path, maximum, expected_bytes, expected_sha):
    data = prepare_consumer.read_regular(Path(path), maximum)
    prepare.require(type(expected_bytes) is int and len(data) == expected_bytes and
                    isinstance(expected_sha, str) and hashlib.sha256(data).hexdigest() == expected_sha,
                    'bound_file_mismatch')
    return data


def identity_hash(path):
    # A file growing while hashed cannot extend this scan without bound.
    return capture.input_identity(Path(path), maximum=256 * 1024 * 1024)[1]


def verify_identities(identities, deadline=None):
    prepare.require(isinstance(identities, dict) and identities, 'replay_input_or_binary_drift')
    for path, value in identities.items():
        if deadline is not None:
            remaining(deadline)
        prepare.require(identity_hash(path) == value, 'replay_input_or_binary_drift')


def verify_child_exit(code, stderr, reason):
    prepare.require(type(code) is int and code == 0 and not stderr, reason)


def verify_capture_stdout(data):
    """The paired process must exit zero *and* emit its unique terminal result."""
    text = data.decode('utf-8', 'strict').replace('\r\n', '\n')
    prepare.require(text.endswith('\n'), 'capture_terminal_incomplete')
    lines = text.splitlines()
    prepare.require(len(lines) == 5 and all(lines), 'capture_terminal_lines_invalid')
    for line, count in zip(lines[:4], (5, 10, 15, 20)):
        value = paired.unique_json(line)
        prepare.require(isinstance(value, dict) and set(value) == {'state', 'completed_pairs', 'total_pairs'} and
                        value['state'] == 'RUNNING' and type(value['completed_pairs']) is int and
                        value['completed_pairs'] == count and type(value['total_pairs']) is int and
                        value['total_pairs'] == 20, 'capture_progress_invalid')
    terminal = paired.unique_json(lines[-1])
    prepare.require(isinstance(terminal, dict) and set(terminal) == {
        'state', 'pair_count', 'scope', 'performance_qualified'} and
        terminal['state'] == 'PASS_SCOPED_PAIRED_READER_CAPTURE' and
        type(terminal['pair_count']) is int and terminal['pair_count'] == 20 and
        terminal['scope'] == PAIR_SCOPE and terminal['performance_qualified'] is False,
        'capture_terminal_invalid')
    return terminal


def verified_trace_incomplete(capture_dir, stdout, stderr, code, event_limit):
    """Accept only the producer's matching fixed failure sidecar and terminal JSON."""
    if type(code) is not int or code != 2 or stderr or not isinstance(stdout, bytes):
        return None
    try:
        text = stdout.decode('ascii', 'strict').replace('\r\n', '\n')
        if not text.endswith('\n'):
            return None
        lines = text.splitlines()
        if not 1 <= len(lines) <= 5 or not all(lines):
            return None
        for index, line in enumerate(lines[:-1], 1):
            value = paired.unique_json(line)
            if not (isinstance(value, dict) and set(value) == {'state', 'completed_pairs', 'total_pairs'} and
                    value['state'] == 'RUNNING' and type(value['completed_pairs']) is int and
                    value['completed_pairs'] == index * 5 and type(value['total_pairs']) is int and
                    value['total_pairs'] == 20):
                return None
        terminal = paired.unique_json(lines[-1])
        capture_dir = prepare_consumer.confined(capture_dir, prepare_consumer.build_root(), must_exist=True)
        sidecar = paired.unique_json(prepare_consumer.read_regular(capture_dir / 'trace-incomplete.json', 4096)
                                     .decode('ascii', 'strict'))
        fields = {'state', 'scope', 'evidence_scope', 'variant', 'case', 'reason', 'attempted_lower_bound',
                  'attempted_capped', 'recorded', 'event_limit'}
        if not (isinstance(sidecar, dict) and set(sidecar) == fields and
                json.dumps(terminal, sort_keys=True, separators=(',', ':'), allow_nan=False) ==
                json.dumps(sidecar, sort_keys=True, separators=(',', ':'), allow_nan=False) and
                sidecar['state'] == 'INCOMPLETE' and sidecar['scope'] == PAIR_SCOPE and
                sidecar['evidence_scope'] == 'CHILD_REPORTED_NOT_INDEPENDENTLY_VERIFIED' and
                sidecar['variant'] in paired.VARIANTS and sidecar['case'] in paired.CASES):
            return None
        completed = 5 * (len(lines) - 1)
        pair_index = paired.VARIANTS.index(sidecar['variant']) * len(paired.CASES) + paired.CASES.index(sidecar['case'])
        if not completed <= pair_index < min(completed + 5, len(paired.VARIANTS) * len(paired.CASES)):
            return None
        diagnostic = {key: sidecar[key] for key in ('reason', 'attempted_lower_bound',
                      'attempted_capped', 'recorded', 'event_limit')}
        encoded = (paired.TRACE_FAILURE_PREFIX + json.dumps(diagnostic, allow_nan=False) + '\n').encode('ascii')
        return sidecar if paired.parse_trace_failure(encoded, event_limit) == diagnostic else None
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None


def oracle(stdout, input_bytes, consumed_sha, variant, case, expected_events, expected_sites, limit, deadline=None):
    """Independent byte oracle for exactly the bound reader event sequence."""
    prepare.require(variant in paired.VARIANTS and case in paired.CASES and
                    type(expected_events) is int and type(expected_sites) is int and
                    0 <= expected_events <= limit and 0 <= expected_sites <= 32,
                    'oracle_count_invalid')
    payload = (b'\0' + input_bytes[1:]) if case == 'corrupt-input' else input_bytes
    prepare.require(hashlib.sha256(payload).hexdigest() == consumed_sha, 'oracle_input_mismatch')
    stream = io.BytesIO(stdout)
    module = stream.readline(capture.MAX_STDOUT + 1)
    report = stream.readline(capture.MAX_STDOUT + 1)
    header = stream.readline(257)
    prepare.require(module.endswith(b'\n') and report.endswith(b'\n') and header.endswith(b'\n'),
                    'oracle_header_incomplete')
    match = re.fullmatch(rb'MGMEM_DEX_READER_TRACE_V1 ([0-9]+) ([0-9a-f]{64})\r?\n', header)
    prepare.require(match is not None and int(match.group(1)) == expected_events and
                    match.group(2).decode('ascii') == consumed_sha, 'oracle_header_mismatch')
    digest = hashlib.sha256()
    sites = set()
    for index in range(expected_events):
        if deadline is not None and index % 4096 == 0:
            remaining(deadline)
        line = stream.readline(129)
        event = re.fullmatch(rb'E ([0-9]+) ([0-9]+)\r?\n', line)
        prepare.require(event is not None, 'oracle_event_invalid')
        site, offset = int(event.group(1)), int(event.group(2))
        prepare.require((1 <= site <= 31 or site == 1001) and offset < len(payload), 'oracle_event_range')
        sites.add(site)
        digest.update(struct.pack('<IQB', site, offset, payload[offset]))
    prepare.require(stream.read(1) == b'' and len(sites) == expected_sites and
                    ((expected_events == 0) if case in ('invalid-lease', 'borrow-budget')
                     else expected_events > 0), 'oracle_trace_mismatch')
    return digest.hexdigest()


def verify_worker_report(value, variant, case, events, checksum):
    fields = {'schema_version', 'state', 'variant', 'case', 'events', 'checksum_sha256',
              'core_use', 'cleanup_outcome', 'device', 'performance'}
    managed = variant in ('D-C1', 'D-C2')
    prepare.require(isinstance(value, dict) and set(value) == fields and
                    type(value['schema_version']) is int and value['schema_version'] == 1 and
                    value['state'] == 'PASS_SCOPED_READER_BYTE_WORKER' and
                    value['variant'] == variant and value['case'] == case and
                    type(value['events']) is int and value['events'] == events and
                    value['checksum_sha256'] == checksum and
                    value['core_use'] == ('REQUESTED_SHARED_CORE_NOT_LOAD_ATTESTED' if managed else 'NOT_APPLICABLE') and
                    value['cleanup_outcome'] == ('CORE_CLOSED_ZERO_AND_DESTROYED' if managed else
                                                 'DIRECT_REFERENCES_CLEARED_NOT_OS_RECLAIM_PROOF') and
                    value['device'] == 'NOT_RUN' and value['performance'] == 'NOT_MEASURED',
                    'worker_report_invalid')
    return value


def remaining(deadline):
    value = deadline - time.monotonic()
    prepare.require(value > 0, 'overall_timeout')
    return value


def controlled_path(path, boundary, maximum):
    result = prepare_consumer.confined(path, boundary, must_exist=True)
    prepare_consumer.read_regular(result, maximum)
    return result


def preflight_sdk(candidate, evidence, sdk, core):
    """Compare the selected SDK with its identified archive before any child starts."""
    identified = paired.verify_sdk(candidate, evidence, sdk)
    expected = (sdk / 'bin' / 'mgmem.dll') if os.name == 'nt' else (sdk / 'lib' / 'libmgmem.so')
    prepare.require(expected.resolve(strict=True) == core.resolve(strict=True), 'selected_core_mismatch')
    return identified


def run(args):
    prepare.require(type(args.overall_timeout_seconds) is int and 1 <= args.overall_timeout_seconds <= 3600 and
                    type(args.capture_timeout_seconds) is int and 1 <= args.capture_timeout_seconds <= 3000 and
                    type(args.timeout_seconds) is int and 1 <= args.timeout_seconds <= 300 and
                    type(args.max_events) is int and 1 <= args.max_events <= 1000000,
                    'replay_options_invalid')
    deadline = time.monotonic() + args.overall_timeout_seconds
    build = prepare_consumer.build_root()
    out = prepare_consumer.confined(args.out_dir, build)
    prepare.require(not out.exists(), 'output_directory_invalid')
    source = controlled_path(args.input, ROOT, capture.MAX_INPUT)
    baseline = controlled_path(args.baseline, build, prepare_consumer.MAX_FILE)
    traced = controlled_path(args.traced, build, prepare_consumer.MAX_FILE)
    candidate = controlled_path(args.candidate, ROOT, 256 * 1024 * 1024)
    evidence = controlled_path(args.candidate_evidence, ROOT, 1024 * 1024)
    source_lock = controlled_path(args.source_lock, ROOT, prepare_consumer.MAX_LOCK)
    dex_stage = prepare_consumer.confined(args.dex_stage, build, must_exist=True)
    paired_stage = prepare_consumer.confined(args.paired_stage, build, must_exist=True)
    sdk = prepare_consumer.confined(args.sdk_prefix, build, must_exist=True)
    core = Path(args.core).resolve(strict=True)
    prepare.require(core.is_file() and core.is_relative_to(sdk) and baseline != traced and
                    all(p.is_dir() for p in (dex_stage, paired_stage, sdk)) and
                    all(not out.is_relative_to(p) and not p.is_relative_to(out)
                        for p in (dex_stage, paired_stage, sdk, baseline.parent, traced.parent)),
                    'replay_inputs_invalid')
    candidate_identity = preflight_sdk(candidate, evidence, sdk, core)
    prepare_consumer.verify_prepared(dex_stage, source_lock)
    paired.verify_paired(paired_stage)
    remaining(deadline)
    outer = Path(__file__).resolve(strict=True)
    interpreter = Path(sys.executable).resolve(strict=True)
    prepare.require(interpreter.is_file(), 'python_interpreter_unavailable')
    for path in (outer, PRODUCER, WORKER, IMAGE_CORE_HELPER, ROOT / 'consumers/dex_trace/capture.py',
                 ROOT / 'consumers/dex_trace/prepare.py', ROOT / 'consumers/dex_trace/prepare_consumer.py',
                 ROOT / 'consumers/dex_trace/prepare_paired.py'):
        controlled_path(path, ROOT, prepare_consumer.MAX_FILE)
    input_data = prepare_consumer.read_regular(source, capture.MAX_INPUT)
    prepare.require(input_data and input_data[:4] == b'dex\n', 'input_invalid')
    input_sha = hashlib.sha256(input_data).hexdigest()
    corrupt_sha = hashlib.sha256(b'\0' + input_data[1:]).hexdigest()
    identified = (source, baseline, traced, core, candidate, evidence, source_lock, outer, interpreter, PRODUCER, WORKER, IMAGE_CORE_HELPER,
                  ROOT / 'consumers/dex_trace/capture.py', ROOT / 'consumers/dex_trace/prepare.py',
                  ROOT / 'consumers/dex_trace/prepare_consumer.py', ROOT / 'consumers/dex_trace/prepare_paired.py')
    identities = {}
    for path in identified:
        remaining(deadline)
        identities[str(path)] = identity_hash(path)
    prepare.require(identities[str(source)] == input_sha, 'input_identity_mismatch')
    out.mkdir(parents=True, exist_ok=False)
    snapshot = out / 'input.dex'
    write_new(snapshot, input_data)
    prepare.require(prepare_consumer.read_regular(snapshot, capture.MAX_INPUT) == input_data, 'snapshot_drift')
    capture_dir = out / 'capture'
    command = python_command(PRODUCER, '--input', str(snapshot), '--baseline', str(baseline),
               '--traced', str(traced), '--core', str(core), '--candidate', str(candidate),
               '--candidate-evidence', str(evidence), '--source-lock', str(source_lock),
               '--dex-stage', str(dex_stage), '--paired-stage', str(paired_stage),
               '--sdk-prefix', str(sdk), '--out-dir', str(capture_dir), '--max-events', str(args.max_events),
               '--timeout-seconds', str(args.timeout_seconds), '--overall-timeout-seconds',
               str(min(args.capture_timeout_seconds, max(1, int(remaining(deadline))))))
    environment = child_environment()
    code, stdout, stderr = capture.run_bounded(command, min(args.capture_timeout_seconds, remaining(deadline)),
                                               stdout_limit=MAX_CONTROL_OUTPUT, stderr_limit=MAX_CONTROL_OUTPUT,
                                               env=environment)
    write_new(out / 'capture.stdout', stdout)
    write_new(out / 'capture.stderr', stderr)
    diagnostic = verified_trace_incomplete(capture_dir, stdout, stderr, code, args.max_events)
    if diagnostic is not None:
        write_new(out / 'trace-incomplete.json', json_bytes(diagnostic))
        raise paired.TraceIncomplete(diagnostic)
    verify_child_exit(code, stderr, 'capture_process_incomplete')
    capture_exit_code = code
    verify_capture_stdout(stdout)
    capture_dir = prepare_consumer.confined(capture_dir, build, must_exist=True)
    receipt_path = capture_dir / 'receipt.json'
    binding_path = capture_dir / paired.BINDING_NAME
    receipt_bytes = prepare_consumer.read_regular(receipt_path, 1024 * 1024)
    binding_bytes = prepare_consumer.read_regular(binding_path, 1024 * 1024)
    receipt = paired.unique_json(receipt_bytes.decode('utf-8'))
    binding = paired.validate_pair_binding(paired.unique_json(binding_bytes.decode('utf-8')))
    prepare.require(isinstance(receipt, dict) and receipt.get('state') == 'PASS_SCOPED_PAIRED_READER_CAPTURE' and
                    receipt.get('scope') == PAIR_SCOPE and receipt.get('performance_qualified') is False and
                    receipt.get('input_sha256') == binding['input_sha256'] == input_sha and
                    type(receipt.get('input_bytes')) is int and receipt['input_bytes'] == len(input_data) and
                    binding['input_bytes'] == len(input_data) and binding['corrupt_snapshot_sha256'] == corrupt_sha and
                    binding['producer_script_sha256'] == identities[str(PRODUCER)] and
                    binding['baseline_executable_sha256'] == identities[str(baseline)] and
                    binding['traced_executable_sha256'] == identities[str(traced)] and
                    binding['core_file_sha256'] == identities[str(core)] and
                    binding['source_lock_sha256'] == identities[str(source_lock)] and
                    binding['event_limit'] == args.max_events and
                    receipt.get('candidate_source_commit') == binding['candidate_source_commit'] == candidate_identity[0] and
                    receipt.get('candidate_sha256') == binding['candidate_sha256'] == candidate_identity[1],
                    'capture_binding_mismatch')
    expected_files = {'receipt.json', paired.BINDING_NAME, 'progress.json'}
    for pair in binding['pairs']:
        for mode in ('baseline', 'traced'):
            expected_files.update(pair[mode][stream]['name'] for stream in ('stdout', 'stderr'))
    prepare.require({item.name for item in capture_dir.iterdir()} == expected_files, 'capture_file_set_mismatch')
    paired.verify_bound_outputs(capture_dir, binding)
    binding_sha = hashlib.sha256(binding_bytes).hexdigest()
    capture_receipt_sha = hashlib.sha256(receipt_bytes).hexdigest()
    requests = out / 'requests'; requests.mkdir()
    results = []
    worker_files = []
    for pair in binding['pairs']:
        remaining(deadline)
        variant, case = pair['variant'], pair['case']
        raw = {}
        for mode in ('baseline', 'traced'):
            record = pair[mode]['stdout']
            raw[mode] = verified_file(capture_dir / record['name'], capture.MAX_STDOUT,
                                      record['bytes'], record['sha256'])
        expected_hash = pair['snapshot_sha256']
        left, _ = paired.parse_output(raw['baseline'], False, variant, case, len(input_data), input_sha,
                                      expected_hash, core, args.max_events)
        right, trace = paired.parse_output(raw['traced'], True, variant, case, len(input_data), input_sha,
                                           expected_hash, core, args.max_events)
        prepare.require(all(left[key] == right[key] for key in paired.STABLE) and
                        trace['events'] == pair['events'] and trace['site_count'] == pair['sites'],
                        'bound_pair_mismatch')
        expected_checksum = oracle(raw['traced'], input_data, expected_hash, variant, case,
                                   pair['events'], pair['sites'], args.max_events, deadline)
        request = {'schema_version': 1, 'input_path': str(snapshot),
                   'stdout_path': str(capture_dir / pair['traced']['stdout']['name']),
                   'variant': variant, 'case': case, 'input_sha256': input_sha, 'input_bytes': len(input_data),
                   'stdout_sha256': pair['traced']['stdout']['sha256'],
                   'stdout_bytes': pair['traced']['stdout']['bytes'], 'consumed_sha256': expected_hash,
                   'event_count': pair['events'], 'event_limit': args.max_events, 'core_path': str(core)}
        stem = variant + '-' + case
        request_path = requests / (stem + '.json')
        request_bytes = json_bytes(request)
        write_new(request_path, request_bytes)
        code, worker_stdout, worker_stderr = capture.run_bounded(
            python_command(WORKER, '--request', str(request_path)), min(args.timeout_seconds, remaining(deadline)),
            stdout_limit=MAX_CONTROL_OUTPUT, stderr_limit=MAX_CONTROL_OUTPUT, env=environment)
        worker_stdout_path = out / (stem + '.stdout')
        worker_stderr_path = out / (stem + '.stderr')
        write_new(worker_stdout_path, worker_stdout)
        write_new(worker_stderr_path, worker_stderr)
        verify_child_exit(code, worker_stderr, 'worker_process_incomplete')
        prepare.require(worker_stdout.endswith(b'\n') and len(worker_stdout.splitlines()) == 1,
                        'worker_process_incomplete')
        value = paired.unique_json(worker_stdout.decode('utf-8'))
        verify_worker_report(value, variant, case, pair['events'], expected_checksum)
        worker_files.extend([(request_path, request_bytes), (worker_stdout_path, worker_stdout),
                             (worker_stderr_path, worker_stderr)])
        results.append({'variant': variant, 'case': case, 'events': pair['events'],
                        'checksum_sha256': expected_checksum, 'cleanup_outcome': value['cleanup_outcome'],
                        'worker_request_sha256': hashlib.sha256(request_bytes).hexdigest(),
                        'worker_stdout_sha256': hashlib.sha256(worker_stdout).hexdigest(),
                        'worker_stderr_sha256': hashlib.sha256(worker_stderr).hexdigest()})
        if len(results) % 5 == 0:
            print(json.dumps({'state': 'RUNNING', 'replayed_pairs': len(results), 'total_pairs': 20}), flush=True)
    remaining(deadline)
    paired.verify_bound_outputs(capture_dir, binding)
    prepare.require(prepare_consumer.read_regular(binding_path, 1024 * 1024) == binding_bytes and
                    prepare_consumer.read_regular(receipt_path, 1024 * 1024) == receipt_bytes and
                    prepare_consumer.read_regular(snapshot, capture.MAX_INPUT) == input_data and
                    all(prepare_consumer.read_regular(path, MAX_CONTROL_OUTPUT) == data for path, data in worker_files),
                    'replay_input_or_binary_drift')
    verify_identities(identities, deadline)
    prepare.require(preflight_sdk(candidate, evidence, sdk, core) ==
                    (binding['candidate_source_commit'], binding['candidate_sha256']), 'replay_sdk_drift')
    prepare_consumer.verify_prepared(dex_stage, source_lock)
    paired.verify_paired(paired_stage)
    remaining(deadline)
    result = {'schema_version': 1, 'state': 'PASS_BOUND_PAIRED_READER_REPLAY', 'scope': REPLAY_SCOPE,
              'capture_producer_exit_code': capture_exit_code,
              'capture_stdout_sha256': hashlib.sha256(stdout).hexdigest(),
              'capture_receipt_sha256': capture_receipt_sha, 'capture_binding_sha256': binding_sha,
              'outer_script_sha256': identities[str(outer)], 'worker_script_sha256': identities[str(WORKER)],
              'image_core_helper_sha256': identities[str(IMAGE_CORE_HELPER)],
              'requested_python_file_sha256': identities[str(interpreter)],
              'input_sha256': input_sha, 'input_bytes': len(input_data),
              'candidate_source_commit': binding['candidate_source_commit'], 'candidate_sha256': binding['candidate_sha256'],
              'core_file_sha256': identities[str(core)], 'pairs': results,
              'device': 'NOT_RUN', 'performance': 'NOT_MEASURED', 'product_trace': 'NOT_QUALIFIED',
              'limits': ['Only bound source-level reader byte reads are replayed, with a synthetic single-thread lifecycle.',
                         'No independent oracle proves captured event coverage or original machine/hardware access order.',
                         'No query internals, cross-thread lifetime, real product input or performance is qualified.',
                         'Hashes and child-reported module paths are not signed provenance or mapped-byte proof.',
                         'Same-permission processes can tamper with local files; raw artifacts are private.']}
    publish_receipt(out / 'replay-receipt.json', json_bytes(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'baseline', 'traced', 'core', 'candidate', 'candidate-evidence', 'source-lock',
                 'dex-stage', 'paired-stage', 'sdk-prefix', 'out-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--max-events', type=int, default=250000)
    parser.add_argument('--timeout-seconds', type=int, default=60)
    parser.add_argument('--capture-timeout-seconds', type=int, default=300)
    parser.add_argument('--overall-timeout-seconds', type=int, default=600)
    try:
        result = run(parser.parse_args())
        print(json.dumps({'state': result['state'], 'pairs': len(result['pairs']), 'scope': result['scope'],
                          'device': 'NOT_RUN', 'performance': 'NOT_MEASURED'}))
        return 0
    except paired.TraceIncomplete as error:
        print(json.dumps(error.report, ensure_ascii=False, allow_nan=False))
        return 2
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        print(json.dumps({'state': 'INCOMPLETE', 'reason': type(error).__name__,
                          'cleanup_outcome': 'UNKNOWN_OR_WORKER_REPORTED_LOCALLY'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
