"""Offline, host-only contracts for Android candidate assembly; no NDK or ADB."""
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import build_android_candidate as subject
from source_lock import LOCAL_DIRECTORIES, scoped_files


class CandidateContracts(unittest.TestCase):
    def fixture(self, directory):
        root = directory / 'source'
        root.mkdir()
        names = ['CMakeLists.txt', 'src/mgmem.cpp', 'cmake/MeguvizMemoryConfig.cmake.in',
                 'adapters/android/CMakeLists.txt', 'adapters/android/native/pressure.cpp',
                 'adapters/android/native/pressure_jni.cpp', 'adapters/android/native/borrow_jni.cpp',
                 'adapters/android/native/workspace_jni.cpp', 'adapters/android/consumer-rules.pro',
                 'third_party_notices/llvm-NOTICE.txt']
        names += [subject.JAVA_BASE + name + '.java' for name in subject.REQUIRED_JAVA]
        for name in names:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('fixture:' + name).encode())
        lock = directory / 'source-lock.json'
        records = [{'root': 'MeguvizMemory', 'path': p.relative_to(root).as_posix(), **subject.file_identity(p)}
                   for p in scoped_files(root, LOCAL_DIRECTORIES, lock)]
        records.sort(key=lambda row: (row['root'], row['path']))
        lock.write_bytes(subject.json_bytes({'schema_version': 1, 'scope': 'SCOPED_SOURCE_TREE_LOCK',
                                            'file_count': len(records), 'files': records,
                                            'source_set_sha256': subject.sha(subject.canonical(records))}))
        return root, lock

    def test_source_lock_copy_and_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp); root, lock = self.fixture(directory)
            raw, records = subject.verify_lock(root, lock)
            self.assertTrue(raw)
            snapshot = directory / 'copy'
            subject.copy_snapshot(root, snapshot, records)
            subject.verify_snapshot(snapshot, records)
            (snapshot / 'src/mgmem.cpp').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'snapshot_changed'):
                subject.verify_snapshot(snapshot, records)
            (root / 'src/mgmem.cpp').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'source_lock_mismatch'):
                subject.verify_lock(root, lock)

    def test_source_lock_rejects_added_and_tampered_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp); root, lock = self.fixture(directory)
            (root / 'src/unrecorded.cpp').write_bytes(b'extra')
            with self.assertRaisesRegex(ValueError, 'source_set_mismatch'):
                subject.verify_lock(root, lock)
            data = json.loads(lock.read_text(encoding='utf-8'))
            data['files'][0]['sha256'] = 'a' * 64
            lock.write_bytes(subject.json_bytes(data))
            with self.assertRaisesRegex(ValueError, 'source_lock_hash'):
                subject.verify_lock(root, lock)

    def test_archive_names_and_roundtrip(self):
        archive = subject.zip_bytes({'a/file.txt': b'a', 'b/file.bin': b'b'})
        self.assertEqual(subject.archive_members(archive), {'a/file.txt': b'a', 'b/file.bin': b'b'})
        for name in ('../escape', '/absolute', 'C:/outside', 'x\\bad'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                subject.zip_bytes({name: b'bad'})

    def test_recursive_privacy_and_unsafe_members(self):
        nested = subject.zip_bytes({'inner.txt': b'private-needle'})
        outer = subject.zip_bytes({'nested.zip': nested})
        with self.assertRaisesRegex(ValueError, 'private_identifier'):
            subject.Scanner(['private-needle']).archive(outer, 'outer')
        nested = subject.zip_bytes({'inner.txt': b'C:/Users/private/file'})
        with self.assertRaisesRegex(ValueError, 'machine_path'):
            subject.Scanner(['never-match']).archive(nested, 'path')

    def test_jni_exports_reject_old_or_test_symbols(self):
        expected = {'Java_io_github_jichuo1_meguviz_memory_android_ManagedMemoryDomain_nativeAttachPressure'}
        symbol = next(iter(expected))
        subject.validate_jni_symbols('  1: 00000010  16 FUNC GLOBAL DEFAULT 12 ' + symbol, expected)
        with self.assertRaisesRegex(ValueError, 'jni_export_missing'):
            subject.validate_jni_symbols('  1: 00000000 0 FUNC GLOBAL DEFAULT UND ' + symbol, expected)
        with self.assertRaisesRegex(ValueError, 'jni_export_missing'):
            subject.validate_jni_symbols('  1: 00000010 16 FUNC WEAK DEFAULT 12 ' + symbol, expected)
        with self.assertRaisesRegex(ValueError, 'test_native_export'):
            subject.validate_jni_symbols('  1: 00000010 16 FUNC GLOBAL DEFAULT 12 ' + symbol +
                '\n  2: 00000020 16 FUNC GLOBAL DEFAULT 12 mgmem_pressure_test_x', expected)
        with self.assertRaisesRegex(ValueError, 'unresolved_pressure_symbol'):
            subject.validate_jni_symbols('  1: 00000010 16 FUNC GLOBAL DEFAULT 12 ' + symbol +
                '\n  2: 00000000 0 FUNC GLOBAL DEFAULT UND mgmem_pressure_create_v2', expected)

    def test_keep_rules_reject_missing_pressure_class(self):
        rules = b'\n'.join(('-keep class io.github.jichuo1.meguviz.memory.android.' + n + ' { *; }').encode()
                           for n in (*subject.REQUIRED_JAVA, 'ManagedMemoryDomain$Allocation',
                                     'ManagedMemoryDomain$Lease', 'ManagedMemoryDomain$Pressure',
                                     'ManagedMemoryDomain$ManagedException'))
        subject.validate_keep_rules(rules)
        with self.assertRaisesRegex(ValueError, 'keep_rule_missing'):
            subject.validate_keep_rules(rules.replace(b'ManagedMemoryDomain$Pressure', b'MissingPressure'))

    def test_sdk_exact_member_allowlist(self):
        files = {'include/mgmem/a.h': b'h', 'lib/libmgmem.so': b'a', 'lib/libmgmem_pressure.a': b'b',
                 'lib/cmake/MeguvizMemory/MeguvizMemoryConfig.cmake': b'',
                 'lib/cmake/MeguvizMemory/MeguvizMemoryConfigVersion.cmake': b'',
                 'lib/cmake/MeguvizMemory/MeguvizMemoryTargets.cmake': b'',
                 'lib/cmake/MeguvizMemory/MeguvizMemoryTargets-release.cmake': b'',
                 'share/MeguvizMemory/licenses/llvm-NOTICE.txt': b''}
        subject.validate_sdk_members(files, {'a.h': b'h'})
        with self.assertRaisesRegex(ValueError, 'sdk_unexpected_or_missing_member'):
            subject.validate_sdk_members({**files, 'bin/unexpected': b'x'}, {'a.h': b'h'})
        del files['lib/libmgmem.so']
        with self.assertRaisesRegex(ValueError, 'sdk_unexpected_or_missing_member'):
            subject.validate_sdk_members(files, {'a.h': b'h'})

    def test_aar_sdk_independent_readback_rejects_mixed_binaries(self):
        abi = 'arm64-v8a'
        aar = subject.zip_bytes({'jni/' + abi + '/libmgmem.so': b'core',
                                 'jni/' + abi + '/libmgmem_pressure_jni.so': b'jni'})
        sdk = {'lib/libmgmem.so': b'core', 'lib/libmgmem_pressure_jni.so': b'jni',
               'lib/libmgmem_pressure.a': b'pressure'}
        contracts = {'core': {'candidate': subject.ident(b'core')},
                     'jni': {'candidate': subject.ident(b'jni')},
                     'static': {'candidate': subject.ident(b'pressure')}}
        subject.validate_output_identities(aar, subject.zip_bytes(sdk), abi, contracts)
        for key, reason in (('lib/libmgmem.so', 'sdk_aar_core_mismatch'),
                            ('lib/libmgmem_pressure_jni.so', 'sdk_aar_jni_mismatch'),
                            ('lib/libmgmem_pressure.a', 'sdk_static_mismatch')):
            bad = {**sdk, key: b'stale'}
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, reason):
                subject.validate_output_identities(aar, subject.zip_bytes(bad), abi, contracts)

    def test_wrong_and_truncated_elf_rejected(self):
        with self.assertRaisesRegex(ValueError, 'invalid_elf'):
            subject.elf_parts(b'not-elf', 'arm64-v8a')
        raw = bytearray(64); raw[:4] = b'\x7fELF'; raw[4] = 2; raw[5] = 1
        raw[16:18] = (3).to_bytes(2, 'little'); raw[18:20] = (62).to_bytes(2, 'little')
        with self.assertRaisesRegex(ValueError, 'elf_wrong_abi'):
            subject.elf_parts(bytes(raw), 'arm64-v8a')
        with self.assertRaisesRegex(ValueError, 'elf_bad_tables'):
            subject.elf_parts(bytes(raw), 'x86_64')

    def test_load_alignment_and_congruence(self):
        raw = bytearray(512); raw[:4] = b'\x7fELF'; raw[4] = 2; raw[5] = 1
        struct.pack_into('<HH', raw, 16, 3, 183)
        struct.pack_into('<Q', raw, 32, 64); struct.pack_into('<Q', raw, 40, 128)
        struct.pack_into('<HH', raw, 54, 56, 1); struct.pack_into('<HHH', raw, 58, 64, 2, 1)
        struct.pack_into('<IIQQQQQQ', raw, 64, 1, 5, 0, 0, 0, 128, 128, 16384)
        names = b'\x00.shstrtab\x00'; raw[320:320 + len(names)] = names
        struct.pack_into('<IIQQQQIIQQ', raw, 192, 1, 3, 0, 0, 320, len(names), 0, 0, 1, 0)
        subject.verify_stripped_sections(bytes(raw), bytes(raw), 'arm64-v8a')
        struct.pack_into('<Q', raw, 64 + 48, 4096)
        with self.assertRaisesRegex(ValueError, 'elf_not_16k_load_aligned'):
            subject.elf_parts(bytes(raw), 'arm64-v8a')
        struct.pack_into('<Q', raw, 64 + 48, 65536)
        struct.pack_into('<Q', raw, 64 + 16, 16384)
        with self.assertRaisesRegex(ValueError, 'elf_not_16k_load_aligned'):
            subject.elf_parts(bytes(raw), 'arm64-v8a')
        struct.pack_into('<Q', raw, 64 + 48, 16384)
        struct.pack_into('<Q', raw, 64 + 16, 4096)
        with self.assertRaisesRegex(ValueError, 'elf_not_16k_load_aligned'):
            subject.elf_parts(bytes(raw), 'arm64-v8a')

    def test_generator_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'tool.py'; file.write_bytes(b'known')
            records = {'tools/tool.py': subject.file_identity(file)}
            subject.verify_generator_bindings(records, {'tool': file})
            file.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'generator_source_mismatch'):
                subject.verify_generator_bindings(records, {'tool': file})

    def test_atomic_publication_refuses_overwrite_and_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); temp = root / 'temp'; dest = root / 'result.zip'
            temp.write_bytes(b'first')
            subject.publish_no_replace(temp, dest)
            self.assertFalse(temp.exists())
            self.assertEqual(dest.read_bytes(), b'first')
            next_temp = root / 'next'; next_temp.write_bytes(b'second')
            with self.assertRaises(OSError):
                subject.publish_no_replace(next_temp, dest)
            self.assertEqual(next_temp.read_bytes(), b'second')
            self.assertEqual(dest.read_bytes(), b'first')

    def test_timeout_retains_partial_output_and_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); operations = []
            timeout = subprocess.TimeoutExpired('fake', 1, output=b'partial', stderr=b'incomplete')
            with patch.object(subject.subprocess, 'run', side_effect=timeout):
                with self.assertRaisesRegex(ValueError, 'tool_timeout_step'):
                    subject.run_tool(['fake'], root, operations, 'step', timeout=1)
            self.assertEqual((root / 'logs/step.stdout').read_bytes(), b'partial')
            self.assertEqual((root / 'logs/step.stderr').read_bytes(), b'incomplete')
            self.assertEqual(operations[0]['state'], 'TIMEOUT')
            self.assertEqual(json.loads((root / 'operations.jsonl').read_text(encoding='utf-8'))['state'], 'TIMEOUT')

    def test_workspace_roots_created_inside_and_external_link_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'; root.mkdir()
            outside = Path(tmp) / 'external'; outside.mkdir()
            with patch.object(subject, 'ROOT', root):
                self.assertEqual(subject.workspace_directory('build'), (root / 'build').resolve())
                original_resolve = Path.resolve
                def fake_resolve(path, *args, **kwargs):
                    if path == root / 'dist': return original_resolve(outside)
                    return original_resolve(path, *args, **kwargs)
                with patch.object(Path, 'resolve', fake_resolve):
                    with self.assertRaisesRegex(ValueError, 'dist_root_outside_workspace'):
                        subject.workspace_directory('dist')


if __name__ == '__main__':
    unittest.main()
