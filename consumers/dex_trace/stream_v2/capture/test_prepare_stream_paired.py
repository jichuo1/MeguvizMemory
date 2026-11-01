#!/usr/bin/env python3
"""Offline contracts for the isolated V2 paired-source generator."""
import hashlib
import json
from pathlib import Path
import secrets
import unittest
from unittest import mock

import prepare_stream_paired as subject


class StreamPairedContracts(unittest.TestCase):
    def setUp(self):
        self.source = subject.ROOT / 'consumers/dex/paired_v2.cpp'
        self.original = self.source.read_bytes()
        self.stage = subject.ROOT / 'build' / ('stream-v2-paired-contract-' + secrets.token_hex(12))

    def test_transform_scope_and_order(self):
        output = subject.transform(self.original).decode('utf-8')
        self.assertEqual(output.count('MGMEM_DEX_CONSUMER_TRACE_STREAM_V2'), 4)
        self.assertEqual(output.count('mgmem_dex_trace_v2::Scope capture('), 1)
        self.assertLess(output.index('configure_stdout_binary()'), output.index('Metrics m;'))
        self.assertLess(output.index('trace.finish()'), output.index('MGMEM_CORE_MODULE_HEX'))
        self.assertLess(output.index('MGMEM_CORE_MODULE_HEX'), output.index('m.success = true; print(m); std::cout.flush()'))
        self.assertIn('m.test_case == "invalid-lease"', output)
        self.assertIn('m.test_case == "borrow-budget"', output)
        self.assertIn('if (m.test_case == "corrupt-input") source[0] = 0;', output)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_wrong_source_identity_rejected(self):
        with self.assertRaisesRegex(ValueError, 'paired_source_identity_mismatch'):
            subject.transform(self.original + b'x')

    def test_anchor_drift_is_rejected_even_when_identity_is_replaced(self):
        changed = self.original.replace(b'dmg_snapshot_open_borrowed(', b'xmg_snapshot_open_borrowed(', 1)
        self.assertEqual(len(changed), len(self.original))
        with mock.patch.object(subject, 'PAIRED_SHA', hashlib.sha256(changed).hexdigest()):
            with self.assertRaisesRegex(ValueError, 'paired_patch_anchor_mismatch'):
                subject.transform(changed)

    def test_create_and_verify_and_no_overwrite(self):
        manifest = subject.create(self.source, self.stage)
        self.assertEqual(subject.verify_prepared(self.stage, self.source), manifest)
        with self.assertRaisesRegex(ValueError, 'invalid_paths'):
            subject.create(self.source, self.stage)

    def test_forged_hash_cannot_hide_changed_generated_code(self):
        subject.create(self.source, self.stage)
        output = self.stage / 'paired_stream.cpp'
        changed = output.read_bytes() + b'// forged\n'
        output.write_bytes(changed)
        path = self.stage / 'manifest.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value['patched_sha256'] = hashlib.sha256(changed).hexdigest()
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'paired_stage_drift'):
            subject.verify_prepared(self.stage, self.source)

    def test_additional_stage_member_is_rejected(self):
        subject.create(self.source, self.stage)
        (self.stage / 'unexpected').write_bytes(b'x')
        with self.assertRaisesRegex(ValueError, 'paired_stage_set_mismatch'):
            subject.verify_prepared(self.stage, self.source)


if __name__ == '__main__':
    unittest.main()
