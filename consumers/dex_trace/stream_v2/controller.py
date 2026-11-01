#!/usr/bin/env python3
"""Private bounded V2 capture and synthetic reader replay."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import time

HERE = Path(__file__).resolve().parent
DEX = HERE.parent
sys.path[:0] = [str(DEX), str(HERE / 'capture'), str(HERE / 'replay'), str(HERE)]
import capture
import collector
import paired_compare as paired
import paired_replay as old
import prepare
import prepare_consumer as safe
import prepare_stream_paired as maker
import stream_parser as wire

ROOT = prepare.ROOT
WORKER = HERE / 'replay/stream_worker.py'
SOURCE = ROOT / 'consumers/dex/paired_v2.cpp'
CONTROL = 65536


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def remaining(deadline):
    value = deadline - time.monotonic()
    require(value > 0, 'overall_timeout')
    return value


def write_budgeted(path, data, budget):
    require(budget.take(len(data)), 'total_disk_budget_exceeded')
    old.write_new(path, data)


def publish_final(path, data, budget, deadline):
    require(budget.take(len(data)), 'total_disk_budget_exceeded')
    old.publish_receipt(path, data)
    # A file can exist even if publication/readback crossed the deadline.
    # The caller must observe exit=0 in addition to the final receipt.
    remaining(deadline)


def worker_terminal(stdout):
    text = stdout.decode('utf-8', 'strict').replace('\r\n', '\n')
    require(text.endswith('\n') and len(text.splitlines()) == 1, 'worker_terminal_invalid')
    return paired.unique_json(text)


def tool_hashes(scripts, identities):
    result = {p.relative_to(ROOT).as_posix(): identities[str(p)] for p in scripts}
    require(len(result) == len(scripts) and all(wire.valid_sha(value) for value in result.values()),
            'tool_identity_invalid')
    return result


def verify_file(path, maximum, expected_size, expected_hash, deadline):
    path = safe.confined(path, safe.build_root(), must_exist=True)
    before = path.lstat()
    require(not safe.is_link(before) and stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
            type(expected_size) is int and 0 <= expected_size <= maximum and before.st_size == expected_size,
            'output_identity_invalid')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    def sig(s): return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_nlink)
    with os.fdopen(fd, 'rb') as stream:
        require(sig(os.fstat(stream.fileno())) == sig(before), 'output_identity_invalid')
        digest = hashlib.sha256()
        count = 0
        while True:
            remaining(deadline)
            block = stream.read(min(65536, maximum + 1 - count))
            if not block: break
            count += len(block)
            require(count <= maximum, 'output_identity_invalid')
            digest.update(block)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    require(not safe.is_link(current) and sig(before) == sig(after) == sig(current) and
            count == expected_size and digest.hexdigest() == expected_hash, 'output_identity_invalid')
    remaining(deadline)


def verify_cache(build, mode, dex_stage, stage, sdk):
    text = safe.read_regular(build / 'CMakeCache.txt', 1024 * 1024).decode('utf-8')
    expected = {'DEX_TRACE_MODE': mode, 'DEX_TRACE_STAGE': str(dex_stage),
                'PAIRED_TRACE_STAGE': str(stage), 'MeguvizMemory_DIR': str(sdk / 'lib/cmake/MeguvizMemory'),
                'CMAKE_HOME_DIRECTORY': str(HERE / 'capture/paired')}
    for key, value in expected.items():
        found = re.findall(r'(?m)^' + re.escape(key) + r':[^=]+=(.*)$', text)
        require(len(found) == 1 and (found[0].strip() == value if key == 'DEX_TRACE_MODE' else
                Path(found[0].strip()).resolve() == Path(value).resolve()), 'build_cache_mismatch')


def check_worker(value, variant, case, scan, checksum, core_hash):
    fields = {'schema_version', 'state', 'variant', 'case', 'events', 'sites', 'trace_sha256',
              'trace_bytes', 'core_sha256', 'checksum_sha256', 'core_use', 'cleanup_outcome', 'device', 'performance'}
    managed = variant in ('D-C1', 'D-C2')
    require(isinstance(value, dict) and set(value) == fields and type(value['schema_version']) is int and
            value['schema_version'] == 2 and value['state'] == 'PASS_STREAM_V2_READER_BYTE_WORKER' and
            value['variant'] == variant and value['case'] == case and type(value['events']) is int and
            value['events'] == scan['event_count'] and type(value['sites']) is int and value['sites'] == scan['site_count'] and
            value['trace_sha256'] == scan['trace_sha256'] and type(value['trace_bytes']) is int and
            value['trace_bytes'] == scan['trace_bytes'] and value['core_sha256'] == core_hash and
            value['checksum_sha256'] == checksum and
            value['core_use'] == ('REQUESTED_SHARED_CORE_NOT_LOAD_ATTESTED' if managed else 'NOT_APPLICABLE') and
            value['cleanup_outcome'] == ('CORE_CLOSED_ZERO_AND_DESTROYED' if managed else
                                          'DIRECT_REFERENCES_CLEARED_NOT_OS_RECLAIM_PROOF') and
            value['device'] == 'NOT_RUN' and value['performance'] == 'NOT_MEASURED', 'worker_report_invalid')


def run(args):
    require(type(args.max_events) is int and 1 <= args.max_events <= wire.MAX_EVENTS and
            type(args.max_trace_bytes) is int and wire.HEADER_BYTES + 9 <= args.max_trace_bytes <= wire.MAX_TRACE_BYTES and
            type(args.total_disk_bytes) is int and 1 <= args.total_disk_bytes <= 16 * 1024**3 and
            type(args.timeout_seconds) is int and 1 <= args.timeout_seconds <= 3600 and
            type(args.overall_timeout_seconds) is int and 1 <= args.overall_timeout_seconds <= 86400, 'options_invalid')
    deadline = time.monotonic() + args.overall_timeout_seconds
    build = safe.build_root()
    out = safe.confined(args.out_dir, build)
    require(not out.exists(), 'output_exists')
    source = old.controlled_path(args.input, ROOT, capture.MAX_INPUT)
    baseline = old.controlled_path(args.baseline, build, safe.MAX_FILE)
    traced = old.controlled_path(args.traced, build, safe.MAX_FILE)
    candidate = old.controlled_path(args.candidate, ROOT, 256 * 1024 * 1024)
    evidence = old.controlled_path(args.candidate_evidence, ROOT, 1024 * 1024)
    lock = old.controlled_path(args.source_lock, ROOT, safe.MAX_LOCK)
    dex_stage, stage, sdk = (safe.confined(p, build, must_exist=True) for p in
                             (args.dex_stage, args.paired_stage, args.sdk_prefix))
    core = Path(args.core).resolve(strict=True)
    require(all(p.is_dir() for p in (dex_stage, stage, sdk)) and core.is_file() and
            core.is_relative_to(sdk) and baseline != traced and
            all(not out.is_relative_to(p) and not p.is_relative_to(out)
                for p in (dex_stage, stage, sdk, baseline.parent, traced.parent)), 'inputs_invalid')
    commit, archive_hash = old.preflight_sdk(candidate, evidence, sdk, core)
    dex_manifest = safe.verify_prepared(dex_stage, lock)
    stage_manifest = maker.verify_prepared(stage, SOURCE)
    for exe, mode in ((baseline, 'baseline'), (traced, 'traced')):
        verify_cache(exe.parent, mode, dex_stage, stage, sdk)
    payload = safe.read_regular(source, capture.MAX_INPUT)
    require(payload and payload[:4] == b'dex\n', 'input_invalid')
    source_hash = hashlib.sha256(payload).hexdigest()
    corrupt_hash = hashlib.sha256(b'\0' + payload[1:]).hexdigest()
    scripts = [Path(__file__), Path(collector.__file__), Path(wire.__file__), WORKER,
               Path(old.__file__), Path(paired.__file__), Path(capture.__file__), Path(safe.__file__),
               Path(prepare.__file__), Path(maker.__file__), DEX / 'paired_replay_worker.py',
               ROOT / 'consumers/image/trace_replay.py', SOURCE]
    scripts = [old.controlled_path(path, ROOT, safe.MAX_FILE) for path in scripts]
    interpreter = Path(sys.executable).resolve(strict=True)
    require(interpreter.is_file(), 'python_identity_missing')
    identified = [source, baseline, traced, core, candidate, evidence, lock,
                  baseline.parent / 'CMakeCache.txt', traced.parent / 'CMakeCache.txt',
                  dex_stage / 'manifest.json', stage / 'manifest.json', interpreter, *scripts]
    identities = {}
    for path in identified:
        remaining(deadline)
        identities[str(path)] = old.identity_hash(path)
    require(identities[str(source)] == source_hash, 'input_drift')
    core_hash = identities[str(core)]
    out.mkdir(parents=True, exist_ok=False)
    budget = collector.Budget(args.total_disk_bytes)
    snapshot = out / 'input.dex'
    write_budgeted(snapshot, payload, budget)
    require(safe.read_regular(snapshot, capture.MAX_INPUT) == payload, 'snapshot_drift')
    outputs = [(snapshot, capture.MAX_INPUT, len(payload), source_hash)]
    env = {'PATH': str(core.parent) + os.pathsep + os.defpath,
           'MGMEM_DEX_TRACE_MAX_EVENTS': str(args.max_events)}
    for key in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'):
        if key in os.environ: env[key] = os.environ[key]
    if os.name != 'nt': env['LD_LIBRARY_PATH'] = str(core.parent)
    child_env = old.child_environment()
    results = []
    for variant in paired.VARIANTS:
        for case in paired.CASES:
            remaining(deadline)
            name = variant + '-' + case
            base_path, trace_path = out / (name + '.base'), out / (name + '.trace')
            base = collector.run_spooled([baseline, snapshot, '--variant', variant, '--case', case], base_path,
                max_bytes=CONTROL, budget=budget, timeout=min(args.timeout_seconds, remaining(deadline)), env=env)
            trace = collector.run_spooled([traced, snapshot, '--variant', variant, '--case', case], trace_path,
                max_bytes=args.max_trace_bytes, budget=budget, timeout=min(args.timeout_seconds, remaining(deadline)), env=env)
            base_data = safe.read_regular(base_path, CONTROL)
            require(len(base_data) == base['bytes'] and hashlib.sha256(base_data).hexdigest() == base['sha256'], 'baseline_drift')
            outputs.extend([(base_path, CONTROL, base['bytes'], base['sha256']),
                            (trace_path, args.max_trace_bytes, trace['bytes'], trace['sha256'])])
            consumed = corrupt_hash if case == 'corrupt-input' else source_hash
            base_lines = base_data.replace(b'\r\n', b'\n').splitlines(keepends=True)
            require(len(base_lines) == 2 and all(x.endswith(b'\n') for x in base_lines), 'baseline_output_invalid')
            left, base_core_hash = wire._consumer_tail(base_lines[0], base_lines[1], variant, case,
                                                       len(payload), source_hash, core, deadline)
            require(base_core_hash == core_hash, 'baseline_core_identity_mismatch')
            content = (b'\0' + payload[1:]) if case == 'corrupt-input' else payload
            checksum = hashlib.sha256()
            def observed(site, offset):
                checksum.update(struct.pack('<IQB', site, offset, content[offset]))
            scan = wire.scan_file(trace_path, size=len(payload), file_sha256=source_hash, consumed_sha256=consumed,
                limit=args.max_events, variant=variant, case=case, core_path=core, expected_trace_sha256=trace['sha256'],
                max_trace_bytes=args.max_trace_bytes, deadline=deadline, on_event=observed)
            require(scan['trace_bytes'] == trace['bytes'] and scan['core_sha256'] == core_hash and
                    all(left[k] == scan['consumer'][k] for k in paired.STABLE), 'paired_result_mismatch')
            request = {'schema_version':2, 'input_path':str(snapshot), 'trace_path':str(trace_path),
                'variant':variant, 'case':case, 'input_sha256':source_hash, 'input_bytes':len(payload),
                'consumed_sha256':consumed, 'trace_sha256':scan['trace_sha256'], 'trace_bytes':scan['trace_bytes'],
                'event_limit':args.max_events, 'event_count':scan['event_count'], 'core_path':str(core),
                'core_sha256':core_hash, 'max_trace_bytes':args.max_trace_bytes}
            request_path = out / (name + '.request.json')
            request_data = old.json_bytes(request)
            write_budgeted(request_path, request_data, budget)
            outputs.append((request_path, CONTROL, len(request_data), hashlib.sha256(request_data).hexdigest()))
            code, stdout, stderr = capture.run_bounded(old.python_command(WORKER, '--request', str(request_path),
                '--timeout-seconds', str(min(args.timeout_seconds, max(1, int(remaining(deadline)))))),
                min(args.timeout_seconds, remaining(deadline)), stdout_limit=CONTROL, stderr_limit=CONTROL, env=child_env)
            require(type(code) is int and code == 0 and not stderr, 'worker_process_incomplete')
            for suffix, raw in (('.worker.stdout', stdout), ('.worker.stderr', stderr)):
                output_path = out / (name + suffix)
                write_budgeted(output_path, raw, budget)
                outputs.append((output_path, CONTROL, len(raw), hashlib.sha256(raw).hexdigest()))
            value = worker_terminal(stdout)
            check_worker(value, variant, case, scan, checksum.hexdigest(), core_hash)
            results.append({'variant':variant, 'case':case, 'events':scan['event_count'], 'sites':scan['site_count'],
                            'baseline_exit_code':base['exit_code'], 'baseline_bytes':base['bytes'],
                            'baseline_sha256':base['sha256'], 'traced_exit_code':trace['exit_code'],
                            'trace_bytes':scan['trace_bytes'], 'trace_sha256':scan['trace_sha256'],
                            'checksum_sha256':checksum.hexdigest(), 'memory_domain':'PRESENT' if variant in ('D-C1','D-C2') else 'NOT_APPLICABLE',
                            'cleanup_outcome':value['cleanup_outcome'],
                            'request_bytes':len(request_data), 'request_sha256':hashlib.sha256(request_data).hexdigest(),
                            'worker_exit_code':code,
                            'worker_stdout_bytes':len(stdout), 'worker_stdout_sha256':hashlib.sha256(stdout).hexdigest(),
                            'worker_stderr_bytes':len(stderr), 'worker_stderr_sha256':hashlib.sha256(stderr).hexdigest()})
            if len(results) % 5 == 0:
                print(json.dumps({'state':'RUNNING','completed_pairs':len(results),'total_pairs':20}), flush=True)
    require({p.name for p in out.iterdir()} == {p.name for p, _, _, _ in outputs}, 'output_file_set_mismatch')
    for entry in outputs:
        verify_file(*entry, deadline)
    require(len(results) == 20 and safe.read_regular(snapshot, capture.MAX_INPUT) == payload and
            all(old.identity_hash(Path(p)) == value for p, value in identities.items()) and
            safe.verify_prepared(dex_stage, lock) == dex_manifest and maker.verify_prepared(stage, SOURCE) == stage_manifest and
            old.preflight_sdk(candidate, evidence, sdk, core) == (commit, archive_hash), 'postflight_drift')
    remaining(deadline)
    result = {'schema_version':2, 'state':'PASS_STREAM_V2_PAIRED_READER_REPLAY',
              'scope':'SNAPSHOT_OPEN_READER_SYNTHETIC_SINGLE_THREAD_REPLAY',
              'candidate_source_commit':commit, 'candidate_sha256':archive_hash, 'input_sha256':source_hash,
              'input_bytes':len(payload), 'core_sha256':core_hash,
              'baseline_sha256':identities[str(baseline)], 'traced_sha256':identities[str(traced)],
              'source_lock_sha256':identities[str(lock)], 'requested_python_sha256':identities[str(interpreter)],
              'dex_stage_manifest_sha256':identities[str(dex_stage / 'manifest.json')],
              'paired_stage_manifest_sha256':identities[str(stage / 'manifest.json')],
              'tool_sha256':tool_hashes(scripts, identities),
              'event_limit':args.max_events,
              'max_trace_bytes':args.max_trace_bytes, 'total_disk_bytes':args.total_disk_bytes,
              'disk_bytes_before_receipt':budget.used, 'cases':results, 'device':'NOT_RUN', 'performance':'NOT_MEASURED',
              'limits':['No complete query, concurrent lifetime, product performance, hardware access or mapped-byte proof.',
                        'Disk budget covers controller persistent files, not filesystem metadata or child temporary files.',
                        'Deadlines are cooperative and cannot interrupt a blocked system call.']}
    data = old.json_bytes(result)
    publish_final(out / 'receipt.json', data, budget, deadline)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input','baseline','traced','core','candidate','candidate-evidence','source-lock',
                 'dex-stage','paired-stage','sdk-prefix','out-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--max-events', type=int, required=True)
    parser.add_argument('--max-trace-bytes', type=int, required=True)
    parser.add_argument('--total-disk-bytes', type=int, required=True)
    parser.add_argument('--timeout-seconds', type=int, default=300)
    parser.add_argument('--overall-timeout-seconds', type=int, default=3600)
    args = parser.parse_args()
    try:
        result = run(args)
        print(json.dumps({'state':result['state'], 'pairs':len(result['cases']), 'device':'NOT_RUN','performance':'NOT_MEASURED'}))
        return 0
    except Exception as error:
        print(json.dumps({'state':'INCOMPLETE','reason':type(error).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
