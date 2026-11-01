#!/usr/bin/env python3
"""Focused V2 outer admission and evidence contracts; no device."""
import hashlib
from pathlib import Path
import secrets
import time
import unittest
from unittest import mock
import controller


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.directory = controller.safe.build_root() / ('stream-controller-test-' + secrets.token_hex(10))
        self.directory.mkdir()
        self.path = self.directory / 'item'
        self.path.write_bytes(b'abc')
        self.digest = hashlib.sha256(b'abc').hexdigest()
        self.scan = {'event_count':2,'site_count':2,'trace_sha256':'a'*64,'trace_bytes':200}
        self.report = {'schema_version':2,'state':'PASS_STREAM_V2_READER_BYTE_WORKER','variant':'D-C1','case':'normal',
                       'events':2,'sites':2,'trace_sha256':'a'*64,'trace_bytes':200,'core_sha256':'b'*64,
                       'checksum_sha256':'c'*64,'core_use':'REQUESTED_SHARED_CORE_NOT_LOAD_ATTESTED',
                       'cleanup_outcome':'CORE_CLOSED_ZERO_AND_DESTROYED','device':'NOT_RUN','performance':'NOT_MEASURED'}

    def test_identity_and_drift(self):
        controller.verify_file(self.path, 3, 3, self.digest, time.monotonic() + 10)
        self.path.write_bytes(b'bad')
        with self.assertRaises(ValueError):
            controller.verify_file(self.path, 3, 3, self.digest, time.monotonic() + 10)

    def test_identity_deadline_and_size(self):
        with self.assertRaisesRegex(ValueError, 'overall_timeout'):
            controller.verify_file(self.path, 3, 3, self.digest, time.monotonic() - 1)
        with self.assertRaises(ValueError):
            controller.verify_file(self.path, 2, 3, self.digest, time.monotonic() + 10)

    def test_output_budget_is_reserved_before_write(self):
        budget = controller.collector.Budget(2)
        output = self.directory / 'new'
        with self.assertRaisesRegex(ValueError, 'total_disk_budget_exceeded'):
            controller.write_budgeted(output, b'abc', budget)
        self.assertFalse(output.exists())

    def test_worker_result_strict(self):
        controller.check_worker(self.report, 'D-C1', 'normal', self.scan, 'c'*64, 'b'*64)
        for key, value in [('events', True), ('sites', False), ('trace_bytes', True),
                           ('core_sha256','d'*64), ('checksum_sha256','d'*64),
                           ('cleanup_outcome','UNKNOWN_QUARANTINED'), ('performance','PASS')]:
            altered = dict(self.report); altered[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                controller.check_worker(altered, 'D-C1', 'normal', self.scan, 'c'*64, 'b'*64)

    def test_direct_cases_cannot_claim_domain(self):
        value = dict(self.report, variant='D-Z0')
        with self.assertRaises(ValueError):
            controller.check_worker(value, 'D-Z0', 'normal', self.scan, 'c'*64, 'b'*64)
        value.update(core_use='NOT_APPLICABLE', cleanup_outcome='DIRECT_REFERENCES_CLEARED_NOT_OS_RECLAIM_PROOF')
        controller.check_worker(value, 'D-Z0', 'normal', self.scan, 'c'*64, 'b'*64)

    def test_postflight_catches_hardlink(self):
        import os
        try: os.link(self.path, self.directory / 'link')
        except (OSError, NotImplementedError): self.skipTest('hardlink unavailable')
        with self.assertRaises(ValueError):
            controller.verify_file(self.path, 3, 3, self.digest, time.monotonic() + 10)

    def test_worker_terminal_is_unique_and_complete(self):
        self.assertEqual(controller.worker_terminal(b'{"a":1}\n'), {'a': 1})
        for text in (b'{"a":1}', b'{"a":1}\n\n', b'{"a":1}\n{}\n', b'{"a":1,"a":2}\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                controller.worker_terminal(text)

    def test_publication_crossing_deadline_cannot_return_success(self):
        output = self.directory / 'receipt.json'
        def publish(path, data):
            path.write_bytes(data)
        with mock.patch.object(controller.old, 'publish_receipt', side_effect=publish), \
             mock.patch.object(controller.time, 'monotonic', return_value=10):
            with self.assertRaisesRegex(ValueError, 'overall_timeout'):
                controller.publish_final(output, b'{}\n', controller.collector.Budget(3), 10)
        self.assertTrue(output.is_file())  # File alone is never qualification.

    def test_tool_identity_is_relative_unique_and_bound(self):
        files = [controller.ROOT / 'consumers/dex_trace/stream_v2/controller.py',
                 controller.ROOT / 'consumers/dex_trace/stream_v2/collector.py']
        identities = {str(files[0]): 'a' * 64, str(files[1]): 'b' * 64}
        result = controller.tool_hashes(files, identities)
        self.assertEqual(set(result), {'consumers/dex_trace/stream_v2/controller.py',
                                       'consumers/dex_trace/stream_v2/collector.py'})
        self.assertEqual(result['consumers/dex_trace/stream_v2/controller.py'], 'a' * 64)
        with self.assertRaises(ValueError):
            controller.tool_hashes([files[0], files[0]], identities)
        identities[str(files[1])] = 'invalid'
        with self.assertRaises(ValueError):
            controller.tool_hashes(files, identities)


if __name__ == '__main__':
    unittest.main()
