#!/usr/bin/env python3
"""Compare selected reader trace with an uninstrumented reader; output is private."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

import prepare

ROOT = prepare.ROOT
MAX_STDOUT = 32 * 1024 * 1024
MAX_STDERR = 8192
MAX_INPUT = 32 * 1024 * 1024


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_input(path, maximum=MAX_INPUT):
    """Return at most the input cap; one extra byte detects growth/oversize."""
    prepare.require(maximum > 0, 'input_size_invalid')
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    prepare.require(1 <= len(data) <= maximum, 'input_size_invalid')
    return data


def input_identity(path, maximum=MAX_INPUT):
    """Hash incrementally and stop after at most one byte beyond the cap."""
    prepare.require(maximum > 0, 'input_size_invalid')
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        while True:
            block = stream.read(min(1024 * 1024, maximum + 1 - size))
            if not block:
                break
            size += len(block)
            prepare.require(size <= maximum, 'input_size_invalid')
            digest.update(block)
    prepare.require(size > 0, 'input_size_invalid')
    return size, digest.hexdigest()


def run_bounded(command, timeout, stdout_limit=MAX_STDOUT, stderr_limit=MAX_STDERR):
    """Drain both pipes concurrently with bounded memory and terminate our child on failure."""
    prepare.require(timeout > 0 and stdout_limit >= 0 and stderr_limit >= 0, 'invalid_process_limits')
    failed = threading.Event()
    failures = []
    outputs = [bytearray(), bytearray()]
    finished = [threading.Event(), threading.Event()]
    limits = [stdout_limit, stderr_limit]
    names = ['stdout', 'stderr']
    threads = []
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    streams = [child.stdout, child.stderr]

    def reader(index):
        try:
            while True:
                block = os.read(streams[index].fileno(), 65536)
                if not block:
                    return
                room = limits[index] - len(outputs[index])
                if len(block) > room:
                    outputs[index].extend(block[:room])
                    failures.append('process_' + names[index] + '_limit')
                    failed.set()
                    return
                outputs[index].extend(block)
        except (OSError, ValueError):
            failures.append('process_pipe_error')
            failed.set()
        finally:
            finished[index].set()

    try:
        for index in range(2):
            thread = threading.Thread(target=reader, args=(index,), daemon=True)
            thread.start()
            threads.append(thread)
        deadline = time.monotonic() + timeout
        while True:
            if failed.is_set():
                raise ValueError(failures[0] if failures else 'process_pipe_error')
            if child.poll() is not None and all(done.is_set() for done in finished):
                break
            if time.monotonic() >= deadline:
                raise ValueError('process_timeout')
            failed.wait(min(0.05, max(0, deadline - time.monotonic())))
        if failed.is_set():
            raise ValueError(failures[0] if failures else 'process_pipe_error')
        return child.returncode, bytes(outputs[0]), bytes(outputs[1])
    except BaseException:
        # Only the process launched by this function is targeted; a descendant
        # retaining a pipe may outlive it and cannot be declared terminated.
        if child.poll() is None:
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
        raise
    finally:
        for thread in threads:
            thread.join(timeout=0.25)
        for index, stream in enumerate(streams):
            if stream is not None and (index >= len(threads) or not threads[index].is_alive()):
                stream.close()


def parse(data, mode, expected_size, expected_sha, limit, sites):
    lines = data.splitlines()
    prepare.require(data.endswith('\n') and lines and all(lines) and len(lines[0]) < 512, 'missing_result')
    parts = lines[0].split()
    prepare.require(len(parts) == 8 and parts[0] == 'R' and
                    re.fullmatch(r'(?:OK|ERROR_[0-2]_[0-9]+)', parts[1]) and
                    (parts[2] == '-' if parts[1] != 'OK' else re.fullmatch(r'[0-9a-f]{64}', parts[2])) and
                    parts[3] == expected_sha and parts[4].isdigit() and int(parts[4]) == expected_size and
                    parts[5].isdigit() and parts[6].isdigit(), 'invalid_result')
    attempted, written = int(parts[5]), int(parts[6])
    prepare.require(attempted == written and written <= limit and len(lines) == written + 1, 'trace_count_mismatch')
    prepare.require(parts[7] == ('NOT_INSTRUMENTED' if mode == 'baseline' else 'COMPLETE'), 'trace_incomplete')
    if mode == 'baseline':
        prepare.require(written == 0, 'baseline_instrumented')
    elif parts[1] == 'OK':
        prepare.require(written > 0, 'instrumented_trace_empty')
    hits = set()
    for line in lines[1:]:
        values = line.split()
        prepare.require(len(values) == 3 and values[0] == 'E' and values[1].isdigit() and values[2].isdigit(), 'event_invalid')
        site, offset = int(values[1]), int(values[2])
        prepare.require(site in sites and 0 <= offset < expected_size, 'event_out_of_scope')
        hits.add(site)
    return {'status': parts[1], 'facts_sha256': parts[2], 'event_count': written, 'site_hits': sorted(hits)}


def capture(args):
    out = args.out_dir.resolve()
    build = (ROOT / 'build').resolve()
    prepare.require(out.is_relative_to(build) and out != build and not out.exists(), 'invalid_output_directory')
    paths = [args.input.resolve(strict=True), args.baseline.resolve(strict=True),
             args.instrumented.resolve(strict=True), args.manifest.resolve(strict=True)]
    prepare.require(all(p.is_file() and p.is_relative_to(ROOT) for p in paths) and
                    1 <= args.max_events <= 1000000 and 1 <= args.timeout_seconds <= 300, 'invalid_inputs')
    source, baseline, instrumented, manifest_path = paths
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    prepare.require(manifest.get('state') == 'PREPARED_NOT_BUILT' and
                    manifest.get('scope') == 'DEX_METADATA_READER_ONLY', 'manifest_scope_invalid')
    original = {}
    for name, (digest, length, _) in prepare.FILES.items():
        baseline_source = manifest_path.parent / 'baseline' / name
        prepare.require(baseline_source.stat().st_size == length and sha(baseline_source) == digest and
                        not baseline_source.is_symlink(), 'prepared_source_drift')
        original[name] = baseline_source.read_bytes()
    transformed, expected_sites = prepare.instrument(original)
    prepare.require(manifest['sites'] == expected_sites and
                    {entry['id'] for entry in expected_sites} == set(range(1, 32)) | {1001}, 'site_manifest_invalid')
    sites = {entry['id'] for entry in expected_sites}
    for name, expected in transformed.items():
        changed_source = manifest_path.parent / 'instrumented' / name
        prepare.require(not changed_source.is_symlink() and changed_source.read_bytes() == expected and
                        sha(changed_source) == manifest['instrumented_sha256'][name], 'prepared_source_drift')
    size, input_sha = input_identity(source)
    identities = {str(p): sha(p) for p in paths[1:]}
    identities[str(source)] = input_sha
    out.mkdir(parents=True, exist_ok=False)
    reports = {}
    for mode, exe in [('baseline', baseline), ('instrumented', instrumented)]:
        code, stdout, stderr = run_bounded([str(exe), str(source), str(args.max_events)], args.timeout_seconds)
        prepare.require(code == 0 and not stderr, mode + '_process_incomplete')
        text = stdout.decode('ascii', 'strict')
        reports[mode] = parse(text, mode, size, identities[str(source)], args.max_events, sites)
        if mode == 'instrumented':
            with (out / 'events.txt').open('x', encoding='ascii', newline='\n') as stream:
                stream.write(text)
    prepare.require(reports['baseline']['status'] == reports['instrumented']['status'] and
                    reports['baseline']['facts_sha256'] == reports['instrumented']['facts_sha256'],
                    'reader_result_mismatch')
    prepare.require(input_identity(source) == (size, input_sha) and
                    all(sha(p) == identities[str(p)] for p in paths[1:]), 'input_or_executable_drift')
    for name, (digest, _, _) in prepare.FILES.items():
        prepare.require(sha(manifest_path.parent / 'baseline' / name) == digest and
                        (manifest_path.parent / 'instrumented' / name).read_bytes() == transformed[name] and
                        sha(manifest_path.parent / 'instrumented' / name) == manifest['instrumented_sha256'][name],
                        'prepared_source_drift')
    result = {'state': 'PASS_SCOPED_READER_CAPTURE', 'scope': 'DEX_METADATA_READER_ONLY',
              'input_sha256': identities[str(source)], 'input_bytes': size,
              'baseline_executable_sha256': identities[str(baseline)],
              'instrumented_executable_sha256': identities[str(instrumented)],
              'prepared_manifest_sha256': identities[str(manifest_path)],
              'reader_status': reports['baseline']['status'],
              'facts_sha256': reports['baseline']['facts_sha256'],
              'events': reports['instrumented']['event_count'], 'site_hits': reports['instrumented']['site_hits'],
              'trace_sha256': sha(out / 'events.txt'),
              'limits': ['No proof of executable provenance or mapped binary identity.',
                         'Reader accesses only; excludes SHA, query, instructions, libc and allocator.',
                         'Instrumented source semantics are not hardware ordering or performance evidence.']}
    with (out / 'receipt.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, indent=2); stream.write('\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'baseline', 'instrumented', 'manifest', 'out-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--max-events', type=int, default=250000)
    parser.add_argument('--timeout-seconds', type=int, default=60)
    try:
        value = capture(parser.parse_args())
        print(json.dumps({'state': value['state'], 'reader_status': value['reader_status'],
                          'events': value['events'], 'covered_sites': len(value['site_hits'])}))
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({'state': 'INCOMPLETE', 'reason': type(error).__name__}))
        raise SystemExit(2)
