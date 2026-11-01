#!/usr/bin/env python3
"""Count-only result checks; no device or external parser required."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import run_count as subject


class CountReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = Path(self.temp.name) / 'mgmem.dll'
        self.core.write_bytes(b'core')
        self.input_sha = hashlib.sha256(b'dex\n').hexdigest()
        self.module = str(self.core.resolve()).encode('utf-8').hex()

    def report(self, case='normal'):
        v = {key: 0 for key in subject.paired.STABLE}
        v.update(schema_version=2, workload='dex_source_strategy_complete_owner_v2', variant='D-C1',
                 case=case, success=True, diagnostic=False, failure_reason=None,
                 source_sha256=self.input_sha if case in ('normal', 'cross-thread') else None,
                 input_bytes=4, logical_requested_bytes=4, method_count=1 if case in ('normal', 'cross-thread') else 0,
                 input_budget_bytes=32 * 1024 * 1024, parser_budget_bytes=256 * 1024 * 1024,
                 expected_status=subject.paired.EXPECTED_STATUS[case], observed_status=subject.paired.EXPECTED_STATUS[case],
                 lease_final_correct=True, snapshot_released_before_last_read=case in ('normal', 'cross-thread'),
                 source_final_released=True, final_release_on_other_thread=case == 'cross-thread',
                 callback_retains=0 if case in ('invalid-lease', 'borrow-budget') else 1,
                 callback_releases_including_caller=1 if case in ('invalid-lease', 'borrow-budget') else 2,
                 closed_backing_bytes=0, reserved_final_bytes=0, final_resource_state='CLOSED_ZERO',
                 resource_scope='source_capacity_plus_unique_input_backing_parser_separate_metadata_RSS_unknown',
                 product_scope='independent_DexMeguviz_not_BIL_DexKit')
        return v

    def output(self, line=None, case='normal'):
        data = 'MGMEM_CORE_MODULE_HEX ' + self.module + '\n' + json.dumps(self.report(case)) + '\n'
        if line is not None: data += line + '\n'
        return data.encode()

    def parse(self, line, case='normal'):
        return subject.parse(self.output(line, case), True, 'D-C1', case, 4,
                             self.input_sha, self.input_sha, self.core)[1]

    def test_valid_and_zero_rejection(self):
        self.assertEqual(self.parse('MGMEM_DEX_READER_COUNT_V1 3 7 ' + self.input_sha), {'events': 3, 'sites': 3})
        self.assertEqual(self.parse('MGMEM_DEX_READER_COUNT_V1 0 0 ' + self.input_sha, 'invalid-lease'),
                         {'events': 0, 'sites': 0})

    def test_overflow_bad_mask_and_digest_rejected(self):
        for count, mask, sha in ((2**64, 1, self.input_sha), (1, 2**32, self.input_sha),
                                  (0, 1, self.input_sha), (1, 3, self.input_sha), (1, 1, '0' * 64)):
            with self.subTest(count=count, mask=mask), self.assertRaises(ValueError):
                self.parse(f'MGMEM_DEX_READER_COUNT_V1 {count} {mask} {sha}')

    def test_empty_success_and_nonzero_rejection_rejected(self):
        with self.assertRaises(ValueError): self.parse('MGMEM_DEX_READER_COUNT_V1 0 0 ' + self.input_sha)
        with self.assertRaises(ValueError): self.parse('MGMEM_DEX_READER_COUNT_V1 1 1 ' + self.input_sha, 'borrow-budget')

    def test_baseline_cannot_emit_count(self):
        with self.assertRaises(ValueError):
            subject.parse(self.output('MGMEM_DEX_READER_COUNT_V1 1 1 ' + self.input_sha), False,
                          'D-C1', 'normal', 4, self.input_sha, self.input_sha, self.core)

    def test_late_publication_cannot_return_success(self):
        with mock.patch.object(subject.paired, 'remaining', side_effect=[1.0, ValueError('overall_timeout')]), \
             mock.patch.object(subject.replay, 'publish_receipt') as publish:
            with self.assertRaisesRegex(ValueError, 'overall_timeout'):
                subject.publish_count(self.core.parent / 'receipt.json', b'{}', 1.0)
        publish.assert_called_once()
        with mock.patch.object(subject.paired, 'remaining', side_effect=ValueError('overall_timeout')), \
             mock.patch.object(subject.replay, 'publish_receipt') as publish:
            with self.assertRaisesRegex(ValueError, 'overall_timeout'):
                subject.publish_count(self.core.parent / 'receipt.json', b'{}', 1.0)
        publish.assert_not_called()


if __name__ == '__main__': unittest.main()
