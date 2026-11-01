#!/usr/bin/env python3
"""Host functional contracts for owned multi-arena diagnostics; no TLB claim."""

import argparse
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', required=True, type=Path)
    parser.add_argument('--runtime-dir', type=Path)
    args = parser.parse_args()
    exe = args.exe.resolve(strict=True)
    env = os.environ.copy()
    if args.runtime_dir:
        runtime = str(args.runtime_dir.resolve(strict=True))
        key = 'PATH' if os.name == 'nt' else 'LD_LIBRARY_PATH'
        env[key] = runtime + os.pathsep + env.get(key, '')

    def call(arguments, success):
        result = subprocess.run([str(exe)] + arguments, env=env, capture_output=True,
                                text=True, timeout=30, check=False)
        lines = result.stdout.splitlines()
        assert len(lines) == 1, ('one_json_record_required', arguments, result.returncode)
        record = json.loads(lines[0])
        assert (result.returncode == 0) == success, (arguments, result.returncode, record)
        assert record['success'] is success, (arguments, record)
        assert record['evidence_type'] == 'HOST_DIAGNOSTIC'
        assert record['performance_qualification'] == 'NOT_STARTED'
        assert record['mechanism_evidence'] == 'INCONCLUSIVE'
        return record

    records = []
    for order in ('grouped', 'interleaved'):
        for offset in ('fixed', 'rotate'):
            record = call(['--arenas', '3', '--slots', '4', '--pages', '7', '--passes', '5',
                           '--order', order, '--offset', offset], True)
            assert record['protocol_id'] == 'MM-021-multi-arena-functional-v1'
            assert record['order'] == order and record['offset_policy'] == offset
            assert record['provider'] == 'System' and record['domain_mode'] == 'ARENA'
            assert record['placement'] == record['page_hint'] == 'NOT_SELECTED'
            assert record['cache_line_bytes_assumed'] == 64 and not record['cache_geometry_verified']
            assert record['arena_count'] == 3 and record['slots_per_arena'] == 4
            assert record['pages_per_slot'] == 7 and record['passes'] == 5
            assert record['touched_pages'] == 84 and record['access_count'] == 420
            assert record['touched_payload_bytes'] == 84 * 8
            initial = sum(1 + a * 100000 + s * 1000 + p
                          for a in range(3) for s in range(4) for p in range(7))
            expected = initial * 5 + 84 * sum(sum(range(1, k + 1)) for k in range(1, 6))
            final = initial + 84 * sum(range(1, 6))
            assert record['initial_sum'] == initial
            assert record['warm_access_count'] == 84 and record['warm_checksum'] == initial
            assert record['checksum'] == record['expected_checksum'] == expected
            assert record['final_checksum'] == record['final_expected_checksum'] == final
            assert record['bytes_per_domain_budget'] == 4 * 7 * record['actual_base_page_bytes']
            assert record['unique_backing_bytes'] == 3 * record['bytes_per_domain_budget']
            assert record['initial_live_bytes'] == record['peak_backing_bytes'] == record['unique_backing_bytes']
            assert record['initial_retiring_bytes'] == record['initial_retained_bytes'] == 0
            assert record['mid_live_bytes'] == record['bytes_per_domain_budget']
            assert record['mid_retiring_bytes'] == 2 * record['bytes_per_domain_budget']
            assert record['mid_retained_bytes'] == 0
            assert record['budget_rejections'] == record['close_blocked_by_live_leases'] == 3
            assert record['closed_during_access'] == 2 and record['final_domains_closed_zero'] == 3
            assert not record['phases_overlap']
            assert set(record['phase_ns']) == {'allocation_and_acquire', 'initialization_and_first_touch', 'warm_access',
                                               'pre_close_access', 'begin_close_subset',
                                               'post_close_access', 'final_release'}
            assert record['physical_contiguity'] == record['tlb_geometry'] == 'UNKNOWN'
            assert record['pmu'] == record['pfn_visibility'] == 'UNAVAILABLE'
            records.append(record)
    assert len({(r['initial_sum'], r['checksum'], r['final_checksum'], r['access_count']) for r in records}) == 1

    other = call(['--arenas', '2', '--slots', '1', '--pages', '1', '--passes', '2'], True)
    assert other['touched_pages'] == 2 and other['access_count'] == 4
    assert other['closed_during_access'] == 1 and other['final_domains_closed_zero'] == 2
    cases = [
        (['--arenas', '1'], 'invalid_workload_parameters'),
        (['--slots', '0'], 'invalid_workload_parameters'),
        (['--pages', '65'], 'invalid_workload_parameters'),
        (['--passes', '1'], 'invalid_workload_parameters'),
        (['--arenas', '8', '--slots', '16', '--pages', '9'], 'workload_exceeds_budget'),
        (['--arenas', '18446744073709551616'], 'size_add_overflow'),
        (['--order', 'unknown'], 'unknown_option_or_value'),
        (['--slots'], 'missing_option_value'),
    ]
    for arguments, reason in cases:
        assert call(arguments, False)['failure_reason'] == reason
    print(json.dumps({'state': 'PASS_HOST_FUNCTIONAL', 'positive': 5, 'negative': len(cases),
                      'performance_qualification': 'NOT_STARTED', 'mechanism_evidence': 'INCONCLUSIVE'}))


if __name__ == '__main__':
    main()
