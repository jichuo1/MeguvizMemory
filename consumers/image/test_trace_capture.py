#!/usr/bin/env python3
"""Check opt-in image trace capture against the uninstrumented real consumer.

Inputs are synthetic P6 fixtures. This is source-level functional evidence,
not a production workload, instruction trace, TLB mechanism or timing test.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


OPS = {'DOMAIN', 'ALLOC', 'ACQUIRE', 'ACQUIRE_BUSY', 'RELEASE_LEASE', 'RELEASE_OWNER',
       'BEGIN_CLOSE', 'COLLECT_BUSY', 'DRAIN_TIMEOUT', 'COLLECT_OK', 'DRAIN_OK', 'DESTROY',
       'DIRECT_ALLOC', 'DIRECT_READY', 'DIRECT_CLOSE_DEFERRED', 'DIRECT_FREE',
       'ENQUEUE', 'ADMIT', 'WORKER_START', 'WORKER_END', 'JOIN', 'COPY_IN', 'RGB_READ',
       'LUMA_WRITE', 'LUMA_READ', 'OUTPUT_WRITE', 'OUTPUT_READ', 'OUTPUT_IO_READ'}


def oracle(width, height, rgb):
    grey = [(77 * rgb[i] + 150 * rgb[i+1] + 29 * rgb[i+2]) // 256
            for i in range(0, len(rgb), 3)]
    result = bytes(sum(grey[yy * width + xx]
                       for yy in (max(0, y-1), y, min(height-1, y+1))
                       for xx in (max(0, x-1), x, min(width-1, x+1))) // 9
                   for y in range(height) for x in range(width))
    return f'P5\n{width} {height}\n255\n'.encode('ascii') + result, sum(result)


def invoke(command, env):
    proc = subprocess.run([str(x) for x in command], env=env, capture_output=True,
                          text=True, timeout=30, check=False)
    if not proc.stdout.strip():
        raise AssertionError('consumer output absent')
    return proc.returncode, json.loads(proc.stdout)


def validate_file(path, provider, layout, checksum, rgb):
    lines = path.read_text(encoding='ascii').splitlines()
    assert lines[0] == 'MGMEM_IMAGE_TRACE_V1'
    assert lines[1] == f'H 7 5 2 {provider} {layout} 1 10000'
    assert lines[2] == 'I ' + hashlib.sha256(rgb).hexdigest()
    events = []
    for expected, line in enumerate(lines[3:-1], 1):
        fields = line.split(' ')
        assert len(fields) == 7 and fields[0] == 'E' and int(fields[1]) == expected
        assert fields[2] in {'M', 'W'} and fields[3] in OPS
        assert int(fields[4]) in (0, 1, 2, 255) and int(fields[5]) >= 0 and int(fields[6]) >= 0
        events.append(fields)
    footer = lines[-1].split(' ')
    assert len(footer) == 7 and footer[:2] == ['END', 'COMPLETE']
    assert int(footer[2]) == int(footer[3]) == len(events)
    assert int(footer[4]) == checksum * 2 and int(footer[5]) == 70 and int(footer[6]) == 2
    names = [event[3] for event in events]
    assert names.count('ENQUEUE') == names.count('ADMIT') == names.count('WORKER_START') == 2
    assert names.count('WORKER_END') == names.count('JOIN') == names.count('COPY_IN') == 2
    assert names.count('RGB_READ') == 3 * 70 and names.count('LUMA_WRITE') == 70
    assert names.count('LUMA_READ') == 9 * 70 and names.count('OUTPUT_WRITE') == names.count('OUTPUT_READ') == 70
    assert names.count('OUTPUT_IO_READ') == 1
    if provider == 'direct':
        assert names.count('DIRECT_ALLOC') == names.count('DIRECT_FREE') == 3
        assert names.count('DIRECT_CLOSE_DEFERRED') == 1
        assert not {'DOMAIN', 'ALLOC', 'ACQUIRE', 'RELEASE_LEASE', 'BEGIN_CLOSE'} & set(names)
    else:
        assert names.count('DOMAIN') == names.count('BEGIN_CLOSE') == names.count('DESTROY') == 1
        assert names.count('ALLOC') == names.count('RELEASE_OWNER') == 3
        assert names.count('COLLECT_BUSY') == names.count('DRAIN_TIMEOUT') == 1
        assert names.count('ACQUIRE') == 8 and names.count('ACQUIRE_BUSY') == 2 and names.count('RELEASE_LEASE') == 8
    return len(events)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plain-exe', type=Path, required=True)
    parser.add_argument('--trace-exe', type=Path, required=True)
    parser.add_argument('--fault-exe', type=Path, required=True)
    parser.add_argument('--runtime-dir', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    env = os.environ.copy()
    variable = 'PATH' if os.name == 'nt' else 'LD_LIBRARY_PATH'
    env[variable] = str(args.runtime_dir.resolve(strict=True)) + os.pathsep + env.get(variable, '')
    plain, trace, fault = (p.resolve(strict=True) for p in (args.plain_exe, args.trace_exe, args.fault_exe))
    directory = args.out_dir.resolve(); directory.mkdir(parents=True, exist_ok=False)
    width, height = 7, 5
    rgb = bytes((i * 37 + 11) % 256 for i in range(width * height * 3))
    source = directory / 'synthetic.ppm'
    source.write_bytes(f'P6\n{width} {height}\n255\n'.encode('ascii') + rgb)
    expected, checksum = oracle(width, height, rgb)
    counts = []
    for provider in ('direct', 'system', 'arena'):
        for layout in ('full', 'ring'):
            name = provider + '-' + layout
            common = ['--provider', provider, '--layout', layout, '--frames', '2', '--close-race']
            plain_out = directory / (name + '-plain.pgm'); trace_out = directory / (name + '-trace.pgm')
            trace_dir = directory / (name + '-events')
            pcode, presult = invoke([plain, source, plain_out, *common], env)
            tcode, tresult = invoke([trace, source, trace_out, *common, '--trace-dir', trace_dir,
                                     '--trace-max-events', '10000'], env)
            assert pcode == tcode == 0 and tresult.pop('trace_status') == 'COMPLETE'
            presult.pop('diagnostic_total_ns'); tresult.pop('diagnostic_total_ns')
            assert presult == tresult and plain_out.read_bytes() == trace_out.read_bytes() == expected
            counts.append(validate_file(trace_dir / 'events.txt', provider, layout, checksum, rgb))

    cap_dir = directory / 'capacity-events'
    code, result = invoke([trace, source, directory / 'capacity.pgm', '--provider', 'arena',
                           '--trace-dir', cap_dir, '--trace-max-events', '10'], env)
    assert code != 0 and result['trace_status'] == 'INCOMPLETE'
    footer = (cap_dir / 'events.txt').read_text(encoding='ascii').splitlines()[-1].split(' ')
    assert footer[:3] == ['END', 'INCOMPLETE', '10'] and int(footer[3]) > 10

    failed_dir = directory / 'failed-events'
    code, result = invoke([fault, source, directory / 'fault.pgm', '--provider', 'arena',
                           '--trace-dir', failed_dir, '--trace-test-fail-at', '5'], env)
    assert code != 0 and result['trace_status'] == 'INCOMPLETE'
    assert not (failed_dir / 'events.txt').read_bytes().endswith(b'END COMPLETE\n')
    assert b'END COMPLETE ' not in (failed_dir / 'events.txt').read_bytes()

    existing = directory / 'existing-events'; existing.mkdir()
    marker = existing / 'marker'; marker.write_bytes(b'unchanged')
    code, result = invoke([trace, source, directory / 'existing.pgm', '--provider', 'arena',
                           '--trace-dir', existing], env)
    assert code != 0 and result['trace_status'] == 'INCOMPLETE' and marker.read_bytes() == b'unchanged'
    assert not (existing / 'events.txt').exists()
    existing_output = directory / 'existing.pgm'; existing_output.write_bytes(b'preserve existing output')
    code, result = invoke([trace, source, existing_output, '--provider', 'arena',
                           '--trace-dir', directory / 'existing-output-events'], env)
    assert code != 0 and result['trace_status'] == 'INCOMPLETE'
    assert existing_output.read_bytes() == b'preserve existing output'
    assert 'END INCOMPLETE ' in (directory / 'existing-output-events/events.txt').read_text(encoding='ascii')
    link_target = directory / 'link-target'; link_target.write_bytes(b'preserve symlink target')
    link = directory / 'output-link.pgm'; link_test = 'UNAVAILABLE'
    try:
        link.symlink_to(link_target)
    except (OSError, NotImplementedError):
        pass
    else:
        code, result = invoke([trace, source, link, '--provider', 'system',
                               '--trace-dir', directory / 'symlink-output-events'], env)
        assert code != 0 and result['trace_status'] == 'INCOMPLETE'
        assert link_target.read_bytes() == b'preserve symlink target' and link.is_symlink()
        link_test = 'PASS'
    print(json.dumps({'state': 'PASS_HOST_CAPTURE_CONTRACT', 'positive': len(counts), 'negative': 4,
                      'symlink_rejection': link_test,
                      'fixture': 'SYNTHETIC_INPUT_REAL_IMAGE_ALGORITHM',
                      'device': 'NOT_RUN', 'performance': 'NOT_MEASURED'}))


if __name__ == '__main__':
    main()
