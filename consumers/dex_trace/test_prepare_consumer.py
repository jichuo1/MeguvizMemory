#!/usr/bin/env python3
"""Contract checks for locked private DEX consumer source preparation."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import unittest
from unittest import mock

import prepare as reader_prepare
import prepare_consumer as subject


def digest(data):
    return hashlib.sha256(data).hexdigest()


def lock_bytes(files):
    records = [{'root': 'DexMeguviz', 'path': name, 'bytes': len(data), 'sha256': digest(data)}
               for name, data in sorted(files.items())]
    canonical = json.dumps(records, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')
    return (json.dumps({'schema_version': 1, 'scope': 'SCOPED_SOURCE_TREE_LOCK',
                        'external_parser_source': 'LOCKED', 'file_count': len(records),
                        'source_set_sha256': digest(canonical), 'files': records}) + '\n').encode()


class ConsumerPreparationTests(unittest.TestCase):
    def setUp(self):
        build = subject.ROOT / 'build'
        build.mkdir(exist_ok=True)
        self.base = build / ('dex-consumer-contract-' + secrets.token_hex(12))
        self.base.mkdir()
        self.source = self.base / 'source'
        self.source.mkdir()
        self.out = self.base / 'out'
        self.lock = self.base / 'lock.json'
        reader = ('#include "checked_leb128.hpp"\n' +
                  'auto value = ' + ' + '.join('bytes[' + str(i) + ']' for i in range(31)) + ';\n' +
                  'detail::read_uleb32(bytes,cursor,end);\n' +
                  'detail::read_sleb32(bytes,cursor,end);\n' * 2).encode()
        header = b'#pragma once\n'
        leb = (b'#include "reader.hpp"\nnamespace dexmeguviz::detail {\n'
               b'auto leb_byte() { return bytes[cursor++]; }\n}\n')
        self.files = {'CMakeLists.txt': b'cmake_minimum_required(VERSION 3.25)\n',
                      subject.READER: reader, subject.HEADER: header, subject.LEB: leb,
                      'src/other.cpp': b'// separate translation unit\n'}
        for name, data in self.files.items():
            path = self.source.joinpath(*name.split('/'))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.lock.write_bytes(lock_bytes(self.files))
        self.reader_files = {'reader.cpp': (digest(reader), len(reader), 31),
                             'reader.hpp': (digest(header), len(header), 0),
                             'checked_leb128.hpp': (digest(leb), len(leb), 1)}
        self.patcher = mock.patch.object(reader_prepare, 'FILES', self.reader_files)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def run_prepare(self):
        return subject.prepare(self.source, self.lock, self.out)

    def test_isolated_patches_and_revalidation(self):
        result = self.run_prepare()
        self.assertEqual(len(result['source_files']), len(self.files))
        self.assertEqual(len(result['sites']), 32)
        self.assertEqual(subject.verify_prepared(self.out, self.lock), result)
        baseline = self.out / 'baseline' / subject.LEB
        traced = self.out / 'traced' / subject.LEB
        private = self.out / 'traced' / subject.TRACE_HEADER
        self.assertEqual(baseline.read_bytes(), self.files[subject.LEB])
        self.assertEqual(traced.read_bytes(), self.files[subject.LEB])
        self.assertIn(b'namespace dexmeguviz::mgmem_trace_detail {', private.read_bytes())
        self.assertNotIn(b'namespace dexmeguviz::detail {', private.read_bytes())
        text = (self.out / 'traced' / subject.READER).read_text(encoding='utf-8')
        self.assertEqual(text.count('mgmem_trace_detail::read_uleb32('), 1)
        self.assertEqual(text.count('mgmem_trace_detail::read_sleb32('), 2)
        self.assertEqual(text.count('mgmem_dex_trace::load('), 31)
        self.assertEqual((self.source / subject.READER).read_bytes(), self.files[subject.READER])

    def test_extra_build_input_rejected_before_output(self):
        (self.source / 'src' / 'unexpected.cpp').write_text('// unexpected', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'source_file_set_mismatch'):
            self.run_prepare()
        self.assertFalse(self.out.exists())

    def test_test_fixtures_are_not_copied_or_claimed(self):
        fixture = self.source / 'tests' / 'fixtures' / 'sample.dex'
        fixture.parent.mkdir(parents=True)
        fixture.write_bytes(b'unlocked fixture')
        result = self.run_prepare()
        self.assertNotIn('tests/fixtures/sample.dex', result['source_files'])
        self.assertFalse((self.out / 'baseline' / 'tests').exists())

    def test_source_drift_rejected_before_output(self):
        (self.source / 'src' / 'other.cpp').write_bytes(b'drift')
        with self.assertRaisesRegex(ValueError, 'source_identity_mismatch'):
            self.run_prepare()
        self.assertFalse(self.out.exists())

    def test_forged_patch_hash_does_not_mask_changed_source(self):
        self.run_prepare()
        path = self.out / 'traced' / subject.READER
        data = path.read_bytes() + b'// tamper\n'
        path.write_bytes(data)
        manifest_path = self.out / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['patched_files'][subject.READER] = subject.record(data)
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'prepared_trace_drift'):
            subject.verify_prepared(self.out, self.lock)

    def test_extra_prepared_root_file_rejected(self):
        self.run_prepare()
        (self.out / 'unexpected.txt').write_text('unexpected', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'prepared_root_set_mismatch'):
            subject.verify_prepared(self.out, self.lock)

    def test_manifest_not_published_on_failed_revalidation(self):
        with mock.patch.object(subject, 'verify_trees', side_effect=ValueError('forced_failure')):
            with self.assertRaisesRegex(ValueError, 'forced_failure'):
                self.run_prepare()
        self.assertFalse((self.out / 'manifest.json').exists())

    def test_existing_output_is_not_overwritten(self):
        self.out.mkdir()
        marker = self.out / 'keep.txt'
        marker.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'invalid_paths'):
            self.run_prepare()
        self.assertEqual(marker.read_bytes(), b'keep')

    def test_lock_duplicate_unsafe_and_bad_digest_rejected(self):
        raw = json.loads(self.lock.read_text(encoding='utf-8'))
        for changed in ('duplicate', 'path', 'digest'):
            value = json.loads(json.dumps(raw))
            if changed == 'duplicate': value['files'].append(value['files'][0]); value['file_count'] += 1
            elif changed == 'path': value['files'][0]['path'] = '../escape'
            else: value['source_set_sha256'] = '0' * 64
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                subject.lock_records(json.dumps(value).encode())
        for name in ('a/../b', 'C:/secret', 'a\\b', 'a/NUL.txt', 'a/trailing. '):
            with self.subTest(name=name), self.assertRaises(ValueError):
                subject.checked_path(Path('.'), name)

    def test_secret_lock_entries_are_rejected(self):
        files = dict(self.files)
        files['src/private.env.key'] = b'not a real key'
        with self.assertRaisesRegex(ValueError, 'dex_secret_path_rejected'):
            subject.lock_records(lock_bytes(files))

    def test_linked_source_file_rejected(self):
        link = self.source / 'src' / 'linked.cpp'
        try:
            os.symlink('other.cpp', link)
        except (OSError, NotImplementedError):
            self.skipTest('creating symbolic links is unavailable')
        with self.assertRaisesRegex(ValueError, 'source_link_or_special'):
            self.run_prepare()
        self.assertFalse(self.out.exists())

    def test_linked_output_parent_rejected_even_if_target_inside_build(self):
        link = self.base / 'linked-parent'
        target = self.base / 'target'
        target.mkdir()
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('creating symbolic links is unavailable')
        with self.assertRaisesRegex(ValueError, 'path_link_or_junction'):
            subject.prepare(self.source, self.lock, link / 'out')
        self.assertFalse((target / 'out').exists())


if __name__ == '__main__':
    unittest.main()
