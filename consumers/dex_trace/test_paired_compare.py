#!/usr/bin/env python3
"""Focused offline tests for paired reader trace admission and reporting."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest import mock
import zipfile

import paired_compare as subject
import prepare_paired


class PairedContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.core = self.root / 'mgmem.dll'
        self.core.write_bytes(b'core')
        self.input_hash = hashlib.sha256(b'dex\n').hexdigest()
        self.module = str(self.core.resolve()).encode('utf-8').hex()

    def value(self, case='normal'):
        value = {key: 0 for key in subject.STABLE}
        value.update(schema_version=2, workload='dex_source_strategy_complete_owner_v2', variant='D-C1',
                     case=case, success=True, diagnostic=False, failure_reason=None,
                     source_sha256=self.input_hash if case in ('normal', 'cross-thread') else None,
                     input_bytes=4, method_count=1 if case in ('normal', 'cross-thread') else 0,
                     expected_status=subject.EXPECTED_STATUS[case], observed_status=subject.EXPECTED_STATUS[case],
                     lease_final_correct=True, snapshot_released_before_last_read=case in ('normal', 'cross-thread'),
                     source_final_released=True, final_release_on_other_thread=case == 'cross-thread',
                     callback_retains=0 if case in ('invalid-lease', 'borrow-budget') else 1,
                     callback_releases_including_caller=1 if case in ('invalid-lease', 'borrow-budget') else 2,
                     closed_backing_bytes=0, reserved_final_bytes=0, final_resource_state='CLOSED_ZERO',
                     logical_requested_bytes=4, input_budget_bytes=32 * 1024 * 1024,
                     parser_budget_bytes=256 * 1024 * 1024,
                     resource_scope='source_capacity_plus_unique_input_backing_parser_separate_metadata_RSS_unknown',
                     product_scope='independent_DexMeguviz_not_BIL_DexKit')
        return value

    def output(self, value, events=('E 1 0',), digest=None):
        prefix = 'MGMEM_CORE_MODULE_HEX ' + self.module + '\n' + json.dumps(value) + '\n'
        header = 'MGMEM_DEX_READER_TRACE_V1 ' + str(len(events)) + ' ' + (digest or self.input_hash) + '\n'
        return (prefix + header + ''.join(x + '\n' for x in events)).encode()

    def parse(self, value, events=('E 1 0',)):
        return subject.parse_output(self.output(value, events), True, 'D-C1', value['case'], 4,
                                    self.input_hash, self.input_hash, self.core, 10)

    def binding(self):
        pairs = []
        for variant in subject.VARIANTS:
            for case in subject.CASES:
                entries = {}
                for mode in ('baseline', 'traced'):
                    files = {}
                    for kind in ('stdout', 'stderr'):
                        name = mode + '-' + variant + '-' + case + '.' + kind
                        data = (mode + ':' + case).encode() if kind == 'stdout' else b''
                        (self.root / name).write_bytes(data)
                        files[kind] = subject.output_binding(name, data)
                    entries[mode] = {'exit_code': 0, **files}
                count = 0 if case in ('invalid-lease', 'borrow-budget') else 1
                pairs.append({'variant': variant, 'case': case,
                              'snapshot_sha256': 'b' * 64 if case == 'corrupt-input' else self.input_hash,
                              'snapshot_digest_evidence': 'TRACED_SCOPE_HEADER_REPORTED',
                              'events': count, 'sites': count, **entries})
        return {'schema_version': 2, 'state': 'PAIR_OUTPUT_FILES_BOUND', 'scope': subject.BINDING_SCOPE,
                'producer_exit': 'NOT_ATTESTED', 'candidate_source_commit': 'a' * 40,
                'candidate_sha256': 'a' * 64, 'producer_script_sha256': 'a' * 64,
                'baseline_executable_sha256': 'a' * 64, 'traced_executable_sha256': 'a' * 64,
                'core_file_sha256': 'a' * 64, 'source_lock_sha256': 'a' * 64,
                'input_bytes': 4, 'input_sha256': self.input_hash, 'corrupt_snapshot_sha256': 'b' * 64,
                'event_limit': 10, 'stdout_limit_bytes': subject.capture.MAX_STDOUT,
                'stderr_limit_bytes': subject.capture.MAX_STDERR, 'pairs': pairs}

    def test_v2_binding_requires_complete_outputs_and_does_not_attest_producer_exit(self):
        value = self.binding()
        self.assertEqual(subject.validate_pair_binding(value), value)
        subject.verify_bound_outputs(self.root, value)
        output = subject.publish_pair_binding(self.root, value)
        actual = subject.unique_json(output.read_text(encoding='utf-8'))
        self.assertEqual(actual, value)
        self.assertEqual(actual['producer_exit'], 'NOT_ATTESTED')
        with self.assertRaises(FileExistsError): subject.publish_pair_binding(self.root, value)

    def test_v2_binding_rejects_field_forgery_and_bad_relative_names(self):
        for changed in ('extra', 'boolean', 'duplicate', 'relative', 'corrupt', 'zero_events', 'exit', 'size'):
            value = self.binding()
            pair = value['pairs'][0]
            if changed == 'extra': value['unexpected'] = True
            elif changed == 'boolean': value['input_bytes'] = True
            elif changed == 'duplicate': value['pairs'][1] = dict(pair)
            elif changed == 'relative': pair['traced']['stdout']['name'] = '../outside.stdout'
            elif changed == 'corrupt': value['pairs'][4]['snapshot_sha256'] = self.input_hash
            elif changed == 'zero_events': pair['events'] = pair['sites'] = 0
            elif changed == 'exit': pair['baseline']['exit_code'] = False
            elif changed == 'size': pair['traced']['stderr']['bytes'] = 1
            with self.subTest(changed=changed), self.assertRaises(ValueError): subject.validate_pair_binding(value)

    def test_v2_binding_does_not_publish_on_output_drift(self):
        value = self.binding()
        output = self.root / value['pairs'][0]['traced']['stdout']['name']
        output.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'binding_output_drift'):
            subject.publish_pair_binding(self.root, value)
        self.assertFalse((self.root / subject.BINDING_NAME).exists())

    def test_v2_binding_zero_event_cases_require_zero_sites(self):
        value = self.binding()
        pair = value['pairs'][2]
        self.assertEqual(pair['case'], 'invalid-lease')
        self.assertEqual((pair['events'], pair['sites']), (0, 0))
        pair['sites'] = 1
        with self.assertRaises(ValueError): subject.validate_pair_binding(value)

    def test_valid_reader_and_expected_zero_event_rejection(self):
        self.assertEqual(self.parse(self.value())[1]['events'], 1)
        self.assertEqual(self.parse(self.value('invalid-lease'), ())[1]['events'], 0)
        with self.assertRaisesRegex(ValueError, 'trace_count_invalid'):
            self.parse(self.value(), ())

    def test_wrong_or_bool_status_and_accounting_rejected(self):
        for key, fake in [('expected_status', None), ('expected_status', False),
                          ('observed_status', False), ('closed_backing_bytes', False),
                          ('reserved_final_bytes', False), ('method_count', True)]:
            value = self.value(); value[key] = fake
            with self.subTest(key=key), self.assertRaises(ValueError): self.parse(value)
        value = self.value('invalid-lease'); value['expected_status'] = value['observed_status'] = 0
        with self.assertRaises(ValueError): self.parse(value, ())
        for key, fake in [('callback_retains', -1), ('callback_releases_including_caller', -1),
                          ('result_checksum', -1), ('parser_peak_owned_bytes', True),
                          ('unique_owned_backing_peak_bytes', 2**64), ('diagnostic', 0)]:
            value = self.value(); value[key] = fake
            with self.subTest(key=key), self.assertRaises(ValueError): self.parse(value)
        value = self.value(); value['callback_retains'] = 0; value['callback_releases_including_caller'] = 1
        with self.assertRaises(ValueError): self.parse(value)

    def test_truncated_bad_site_and_foreign_module_rejected(self):
        good = self.output(self.value())
        with self.assertRaises(ValueError):
            subject.parse_output(good[:-1], True, 'D-C1', 'normal', 4, self.input_hash, self.input_hash, self.core, 10)
        with self.assertRaises(ValueError): self.parse(self.value(), ('E 32 0',))
        with self.assertRaises(ValueError): self.parse(self.value(), ('E 1 4',))
        other = self.root / 'other.dll'; other.write_bytes(b'core')
        with self.assertRaises(ValueError):
            subject.parse_output(good, True, 'D-C1', 'normal', 4, self.input_hash, self.input_hash, other, 10)

    def test_deadline_enforced(self):
        with mock.patch.object(subject.time, 'monotonic', return_value=10):
            self.assertEqual(subject.remaining(12), 2)
            with self.assertRaisesRegex(ValueError, 'overall_timeout'): subject.remaining(10)

    def sdk_fixture(self):
        sdk = self.root / 'sdk'; (sdk / 'lib').mkdir(parents=True)
        (sdk / 'lib' / 'one').write_bytes(b'abcd'); (sdk / 'lib' / 'two').write_bytes(b'efgh')
        archive = self.root / 'candidate.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('sdk/lib/one', b'abcd'); z.writestr('sdk/lib/two', b'efgh')
        metadata = {'state': 'PASS_SCOPED_ENGINEERING_CANDIDATES', 'source_commit': 'a' * 40,
                    'windows': {'sha256': subject.capture.sha(archive), 'bytes': archive.stat().st_size}}
        evidence = self.root / 'evidence.json'; evidence.write_text(json.dumps(metadata), encoding='utf-8')
        return archive, evidence, sdk

    def test_sdk_archive_total_limit_before_second_member(self):
        archive, evidence, sdk = self.sdk_fixture()
        with self.assertRaisesRegex(ValueError, 'candidate_member_invalid'):
            subject.verify_sdk(archive, evidence, sdk, max_total=7)
        self.assertEqual(subject.verify_sdk(archive, evidence, sdk, max_total=8)[0], 'a' * 40)

    def test_linux_archive_total_limit(self):
        sdk = self.root / 'sdk'; (sdk / 'lib').mkdir(parents=True)
        (sdk / 'lib' / 'one').write_bytes(b'abcd'); (sdk / 'lib' / 'two').write_bytes(b'efgh')
        archive = self.root / 'candidate.tar.gz'
        with tarfile.open(archive, 'w:gz') as t:
            for name, data in [('sdk/lib/one', b'abcd'), ('sdk/lib/two', b'efgh')]:
                item = tarfile.TarInfo(name); item.size = len(data); t.addfile(item, io.BytesIO(data))
        metadata = {'state': 'PASS_SCOPED_LINUX_HOST_ENGINEERING_CANDIDATE', 'source_commit': 'a' * 40,
                    'candidate_sha256': subject.capture.sha(archive), 'candidate_bytes': archive.stat().st_size}
        evidence = self.root / 'evidence.json'; evidence.write_text(json.dumps(metadata), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'candidate_member_invalid'):
            subject.verify_sdk(archive, evidence, sdk, max_total=7)
        self.assertEqual(subject.verify_sdk(archive, evidence, sdk, max_total=8)[0], 'a' * 40)

    def test_linux_candidate_relative_file_link(self):
        sdk = self.root / 'sdk'; (sdk / 'lib').mkdir(parents=True)
        (sdk / 'lib' / 'one').write_bytes(b'abcd')
        try: (sdk / 'lib' / 'link').symlink_to('one')
        except OSError as error: self.skipTest('symbolic links unavailable: ' + type(error).__name__)
        archive = self.root / 'candidate.tar.gz'
        with tarfile.open(archive, 'w:gz') as t:
            item = tarfile.TarInfo('sdk/lib/one'); item.size = 4; t.addfile(item, io.BytesIO(b'abcd'))
            link = tarfile.TarInfo('sdk/lib/link'); link.type = tarfile.SYMTYPE; link.linkname = 'one'; t.addfile(link)
        metadata = {'state': 'PASS_SCOPED_LINUX_HOST_ENGINEERING_CANDIDATE', 'source_commit': 'a' * 40,
                    'candidate_sha256': subject.capture.sha(archive), 'candidate_bytes': archive.stat().st_size}
        evidence = self.root / 'evidence.json'; evidence.write_text(json.dumps(metadata), encoding='utf-8')
        self.assertEqual(subject.verify_sdk(archive, evidence, sdk)[0], 'a' * 40)

    def test_sdk_directory_link_rejected(self):
        archive, evidence, sdk = self.sdk_fixture()
        lib = sdk / 'lib'; original = subject.prepare_consumer.is_link; inode = lib.lstat().st_ino
        with mock.patch.object(subject.prepare_consumer, 'is_link', side_effect=lambda st: original(st) or st.st_ino == inode):
            with self.assertRaisesRegex(ValueError, 'installed_sdk_link_or_junction'):
                subject.verify_sdk(archive, evidence, sdk)

    def test_path_traversal_and_windows_device_name_rejected(self):
        for name in ('sdk/../x', 'sdk/CON', 'sdk/x:stream', 'sdk/x\\y'):
            with self.subTest(name=name), self.assertRaises(ValueError): subject.safe_path(name)

    def test_paired_source_drift_rejected(self):
        with self.assertRaisesRegex(ValueError, 'paired_source_identity_mismatch'):
            prepare_paired.transform(b'not the frozen source')

    def test_paired_stage_extra_file_and_reparse_rejected(self):
        build = subject.prepare_consumer.build_root()
        with tempfile.TemporaryDirectory(dir=build) as temp:
            source = subject.ROOT / 'consumers/dex/paired_v2.cpp'
            extra = Path(temp) / 'extra'; prepare_paired.create(source, extra)
            (extra / 'unlisted').write_bytes(b'x')
            with self.assertRaisesRegex(ValueError, 'paired_stage_file_set_mismatch'): subject.verify_paired(extra)
            linked = Path(temp) / 'linked'; prepare_paired.create(source, linked)
            target = linked / 'paired_diagnostic.cpp'; inode = target.lstat().st_ino
            original = subject.prepare_consumer.is_link
            with mock.patch.object(subject.prepare_consumer, 'is_link', side_effect=lambda st: original(st) or st.st_ino == inode):
                with self.assertRaisesRegex(ValueError, 'source_file_invalid'): subject.verify_paired(linked)
            bad_manifest = Path(temp) / 'bad_manifest'; prepare_paired.create(source, bad_manifest)
            manifest_path = bad_manifest / 'manifest.json'
            value = json.loads(manifest_path.read_text(encoding='utf-8')); value['unexpected'] = True
            manifest_path.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'paired_stage_manifest_invalid'): subject.verify_paired(bad_manifest)


if __name__ == '__main__':
    unittest.main()
