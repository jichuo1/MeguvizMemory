#!/usr/bin/env python3
"""Count reader accesses in the complete paired workload; no trace or timing claim."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import capture
import paired_compare as paired
import paired_replay as replay
import prepare
import prepare_consumer

ROOT = prepare.ROOT
MAX_OUTPUT = 65536
PROFILE = (HERE / 'CMakeLists.txt', HERE / 'consumer_trace.hpp', HERE / 'count_trace.cpp', Path(__file__).resolve())


def publish_count(path, data, deadline):
    # Publication is no-overwrite; a late publication can leave a complete
    # receipt, so callers must also require this process to exit successfully.
    paired.remaining(deadline)
    replay.publish_receipt(path, data)
    paired.remaining(deadline)


def cache_values(build, mode, dex_stage, paired_stage, sdk):
    text = prepare_consumer.read_regular(build / 'CMakeCache.txt', 1024 * 1024).decode('utf-8')
    expected = {'DEX_TRACE_MODE': mode, 'DEX_TRACE_STAGE': str(dex_stage),
                'PAIRED_TRACE_STAGE': str(paired_stage),
                'MeguvizMemory_DIR': str(sdk / 'lib/cmake/MeguvizMemory'),
                'CMAKE_HOME_DIRECTORY': str(HERE)}
    for key, value in expected.items():
        found = re.findall(r'(?m)^' + re.escape(key) + r':[^=]+=(.*)$', text)
        prepare.require(len(found) == 1 and (found[0].strip() == value if key == 'DEX_TRACE_MODE' else
                        Path(found[0].strip()).resolve() == Path(value).resolve()), 'count_build_cache_mismatch')


def parse(data, instrumented, variant, case, size, input_hash, consumed_hash, core):
    text = data.decode('utf-8', 'strict').replace('\r\n', '\n')
    lines = text.splitlines()
    prepare.require(text.endswith('\n') and len(lines) == (3 if instrumented else 2) and all(lines),
                    'count_output_invalid')
    # Reuse the existing strict workload, module, owner, budget and status checks.
    report, unused = paired.parse_output(('\n'.join(lines[:2]) + '\n').encode('utf-8'), False,
        variant, case, size, input_hash, consumed_hash, core, 1)
    prepare.require(unused is None, 'count_baseline_invalid')
    if not instrumented:
        return report, None
    m = re.fullmatch(r'MGMEM_DEX_READER_COUNT_V1 ([0-9]+) ([0-9]+) ([0-9a-f]{64})', lines[2])
    prepare.require(m is not None and m.group(3) == consumed_hash, 'count_identity_mismatch')
    count, mask = int(m.group(1)), int(m.group(2))
    prepare.require(count <= 2**64 - 1 and mask <= 2**32 - 1 and
                    ((count == 0 and mask == 0) if case in ('invalid-lease', 'borrow-budget')
                     else count > 0 and mask > 0 and mask.bit_count() <= count), 'count_invalid')
    return report, {'events': count, 'sites': mask.bit_count()}


def run(args):
    prepare.require(type(args.timeout_seconds) is int and 1 <= args.timeout_seconds <= 300 and
                    type(args.overall_timeout_seconds) is int and 1 <= args.overall_timeout_seconds <= 3600,
                    'count_timeout_invalid')
    deadline = time.monotonic() + args.overall_timeout_seconds
    selected_variants = (args.variant,) if args.variant else paired.VARIANTS
    selected_cases = (args.case,) if args.case else paired.CASES
    prepare.require(all(v in paired.VARIANTS for v in selected_variants) and
                    all(c in paired.CASES for c in selected_cases), 'count_selection_invalid')
    total_pairs = len(selected_variants) * len(selected_cases)
    build = prepare_consumer.build_root()
    out = prepare_consumer.confined(args.out_dir, build)
    prepare.require(not out.exists(), 'output_directory_invalid')
    source = replay.controlled_path(args.input, ROOT, capture.MAX_INPUT)
    baseline = replay.controlled_path(args.baseline, build, prepare_consumer.MAX_FILE)
    traced = replay.controlled_path(args.traced, build, prepare_consumer.MAX_FILE)
    candidate = replay.controlled_path(args.candidate, ROOT, 256 * 1024 * 1024)
    evidence = replay.controlled_path(args.candidate_evidence, ROOT, 1024 * 1024)
    lock = replay.controlled_path(args.source_lock, ROOT, prepare_consumer.MAX_LOCK)
    dex_stage, paired_stage, sdk = (prepare_consumer.confined(p, build, must_exist=True)
                                    for p in (args.dex_stage, args.paired_stage, args.sdk_prefix))
    core = Path(args.core).resolve(strict=True)
    prepare.require(baseline != traced and core.is_file() and core.is_relative_to(sdk) and
                    all(p.is_dir() for p in (dex_stage, paired_stage, sdk)) and
                    all(not out.is_relative_to(p) and not p.is_relative_to(out)
                        for p in (dex_stage, paired_stage, sdk, baseline.parent, traced.parent)), 'count_inputs_invalid')
    commit, candidate_sha = replay.preflight_sdk(candidate, evidence, sdk, core)
    dex_manifest = prepare_consumer.verify_prepared(dex_stage, lock)
    pair_manifest = paired.verify_paired(paired_stage)
    for exe, mode in ((baseline, 'baseline'), (traced, 'traced')):
        cache_values(exe.parent, mode, dex_stage, paired_stage, sdk)
    payload = prepare_consumer.read_regular(source, capture.MAX_INPUT)
    prepare.require(payload and payload[:4] == b'dex\n', 'input_invalid')
    digest = hashlib.sha256(payload).hexdigest()
    corrupt = hashlib.sha256(b'\0' + payload[1:]).hexdigest()
    paths = (source, baseline, traced, core, candidate, evidence, lock, *PROFILE,
             baseline.parent / 'CMakeCache.txt', traced.parent / 'CMakeCache.txt',
             HERE.parent / 'prepare.py', HERE.parent / 'prepare_paired.py', HERE.parent / 'prepare_consumer.py',
             HERE.parent / 'paired_compare.py', HERE.parent / 'paired_replay.py', HERE.parent / 'capture.py',
             HERE.parent / 'trace_runtime.hpp', ROOT / 'consumers/image/trace_sha256.hpp')
    identities = {}
    for path in paths:
        paired.remaining(deadline)
        identities[str(path)] = replay.identity_hash(path)
    prepare.require(identities[str(source)] == digest, 'count_input_drift')
    out.mkdir(parents=True, exist_ok=False)
    snapshot = out / 'input.dex'
    replay.write_new(snapshot, payload)
    prepare.require(prepare_consumer.read_regular(snapshot, capture.MAX_INPUT) == payload, 'count_input_drift')
    env = {'PATH': str(core.parent) + os.pathsep + os.defpath}
    for key in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'):
        if key in os.environ: env[key] = os.environ[key]
    if os.name != 'nt': env['LD_LIBRARY_PATH'] = str(core.parent)
    rows, streams = [], []
    for variant in selected_variants:
        for case in selected_cases:
            results = []
            for mode, exe in (('baseline', baseline), ('traced', traced)):
                timeout = min(args.timeout_seconds, paired.remaining(deadline))
                code, stdout, stderr = capture.run_bounded([str(exe), str(snapshot), '--variant', variant, '--case', case],
                    timeout, stdout_limit=MAX_OUTPUT, stderr_limit=capture.MAX_STDERR, env=env)
                stdout_name = mode + '-' + variant + '-' + case + '.stdout'
                stderr_name = mode + '-' + variant + '-' + case + '.stderr'
                replay.write_new(out / stdout_name, stdout)
                replay.write_new(out / stderr_name, stderr)
                prepare.require(type(code) is int and code == 0 and not stderr, 'count_child_incomplete')
                streams.append({'variant': variant, 'case': case, 'mode': mode, 'exit_code': code,
                                'stdout': {'name': stdout_name, 'bytes': len(stdout), 'sha256': hashlib.sha256(stdout).hexdigest()},
                                'stderr': {'name': stderr_name, 'bytes': len(stderr), 'sha256': hashlib.sha256(stderr).hexdigest()}})
                results.append(parse(stdout, mode == 'traced', variant, case, len(payload), digest,
                                     corrupt if case == 'corrupt-input' else digest, core))
            left, right = results[0][0], results[1][0]
            prepare.require(all(left[k] == right[k] for k in paired.STABLE), 'count_consumer_mismatch')
            rows.append({'variant': variant, 'case': case, 'events': results[1][1]['events'],
                         'sites': results[1][1]['sites'],
                         'memory_domain': 'PRESENT' if variant in ('D-C1', 'D-C2') else 'NOT_APPLICABLE'})
            if len(rows) % 5 == 0 or len(rows) == total_pairs:
                print(json.dumps({'state': 'RUNNING', 'completed_pairs': len(rows), 'total_pairs': total_pairs}), flush=True)
    paired.remaining(deadline)
    expected_files = {'input.dex'} | {s[stream]['name'] for s in streams for stream in ('stdout', 'stderr')}
    prepare.require(len(streams) == total_pairs * 2 and {p.name for p in out.iterdir()} == expected_files, 'count_output_set_mismatch')
    for entry in streams:
        for stream in ('stdout', 'stderr'):
            paired.remaining(deadline)
            record = entry[stream]
            data = prepare_consumer.read_regular(out / record['name'], MAX_OUTPUT if stream == 'stdout' else capture.MAX_STDERR)
            prepare.require(len(data) == record['bytes'] and hashlib.sha256(data).hexdigest() == record['sha256'],
                            'count_output_drift')
    for exe, mode in ((baseline, 'baseline'), (traced, 'traced')):
        cache_values(exe.parent, mode, dex_stage, paired_stage, sdk)
    for path, value in identities.items():
        paired.remaining(deadline)
        prepare.require(replay.identity_hash(path) == value, 'count_identity_drift')
    prepare.require(prepare_consumer.read_regular(snapshot, capture.MAX_INPUT) == payload and
                    prepare_consumer.verify_prepared(dex_stage, lock) == dex_manifest and
                    paired.verify_paired(paired_stage) == pair_manifest and
                    replay.preflight_sdk(candidate, evidence, sdk, core) == (commit, candidate_sha),
                    'count_identity_drift')
    paired.remaining(deadline)
    result = {'schema_version': 1, 'state': 'PASS_SCOPED_COUNT_ONLY_NOT_TRACE',
              'scope': 'SNAPSHOT_OPEN_CALLER_THREAD_METADATA_READER_ONLY', 'pair_count': len(rows),
              'selection': {'variants': list(selected_variants), 'cases': list(selected_cases),
                            'coverage': 'FULL_MATRIX' if total_pairs == 20 else 'EXPLICIT_SUBSET'},
              'candidate_source_commit': commit, 'input_bytes': len(payload), 'input_sha256': digest,
              'baseline_executable_sha256': identities[str(baseline)], 'traced_executable_sha256': identities[str(traced)],
              'core_sha256': identities[str(core)], 'source_lock_sha256': identities[str(lock)],
              'profile_sha256': {p.name: identities[str(p)] for p in PROFILE},
              'results': rows, 'streams': streams, 'device': 'NOT_RUN', 'performance': 'NOT_MEASURED',
              'limits': ['Counting uses constant memory per selected reader event, not constant memory for the workload.',
                         'Selected reader source semantics only; no query, full lifecycle, hardware or performance trace.',
                         'Disk hashes and child-reported module identity are not signed provenance or mapped-byte proof.',
                         'Count-only output is not a trace and cannot be replayed.']}
    publish_count(out / 'count-receipt.json', replay.json_bytes(result), deadline)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'baseline', 'traced', 'core', 'candidate', 'candidate-evidence', 'source-lock',
                 'dex-stage', 'paired-stage', 'sdk-prefix', 'out-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=int, default=60)
    parser.add_argument('--overall-timeout-seconds', type=int, default=300)
    parser.add_argument('--variant', choices=paired.VARIANTS,
                        help='Explicitly select one variant; omitted means all four')
    parser.add_argument('--case', choices=paired.CASES,
                        help='Explicitly select one full-workload case; omitted means all five')
    try:
        result = run(parser.parse_args())
        print(json.dumps({'state': result['state'], 'pair_count': result['pair_count'],
                          'selection': result['selection'], 'scope': result['scope'],
                          'device': 'NOT_RUN', 'performance': 'NOT_MEASURED'}))
        return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        print(json.dumps({'state': 'INCOMPLETE', 'reason': type(error).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
