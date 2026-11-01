#!/usr/bin/env python3
"""Focused contract checks for the follow-up preview selector; no files written."""
from __future__ import annotations

import io
import json
import struct
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

import package_followup_preview as subject


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as package:
        for name, body in entries.items():
            package.writestr(name, body)
    return buffer.getvalue()


def elf(klass, machine, kind=3, endian=1):
    out = bytearray(20)
    out[:4] = b'\x7fELF'
    out[4], out[5] = klass, endian
    struct.pack_into('<H', out, 16, kind)
    struct.pack_into('<H', out, 18, machine)
    return bytes(out)


class FollowupPreviewChecks(unittest.TestCase):
    def test_elf_machine_class_type_and_endian(self):
        for abi, klass, machine in [('arm64-v8a', 2, 183), ('x86_64', 2, 62), ('armeabi-v7a', 1, 40)]:
            with self.subTest(abi=abi):
                subject.check_elf(elf(klass, machine), abi)
                for payload in (b'', elf(klass, machine, kind=1), elf(klass, machine, endian=2),
                                elf(1 if klass == 2 else 2, machine), elf(klass, machine + 1)):
                    with self.assertRaises(ValueError):
                        subject.check_elf(payload, abi)

    def test_source_paths_cannot_escape(self):
        for name in ('../outside', '/outside', 'sub/../../outside', 'C:\\private'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                subject.checked_relative(name)

    def test_source_hash_mismatch_and_empty_rejected(self):
        with self.assertRaises(ValueError):
            subject.expected_source_hashes({})
        with mock.patch.object(subject, 'file_sha', return_value='a' * 64):
            subject.expected_source_hashes({'src/mgmem.cpp': 'a' * 64})
            with self.assertRaises(ValueError):
                subject.expected_source_hashes({'src/mgmem.cpp': 'b' * 64})

    def test_bridge_source_only_without_candidate(self):
        args = SimpleNamespace(bridge_sdk_zip=None, bridge_evidence=None,
                               bridge_test_source=None, bridge_test_cmake=None, bridge_sanitization_evidence=None,
                               bridge_comparison=None, bridge_path_scan=None)
        data = {}
        self.assertFalse(subject.check_bridge(data, args, 'a' * 64))
        self.assertEqual({}, data)
        args.bridge_sdk_zip = Path('only-one-input')
        with self.assertRaises(ValueError):
            subject.check_bridge(data, args, 'a' * 64)

    def test_windows_bundle_binds_sdk_jni_and_observed_sources(self):
        root = subject.ROOT
        java_class = 'io/github/jichuo1/meguviz/memory/android/NativeMemoryLease.class'
        class_bytes = b'synthetic-current-class'
        host_jar = archive({java_class: class_bytes})
        sdk_files = {
            'bin/mgmem.dll': b'core-dll', 'bin/mgmem_tlb_owned.exe': b'owned-exe',
            'lib/mgmem.lib': b'import-lib', 'lib/mgmem_pressure.lib': b'pressure-lib',
            'include/mgmem/android_pressure_jni.h': b'pressure-jni-h',
            'include/mgmem/pressure.h': b'pressure-h',
        }
        for name in ('android_borrow_jni.h', 'memory.hpp', 'mgmem.h'):
            sdk_files['include/mgmem/' + name] = subject.file_data(root / 'include/mgmem' / name)
        for name in ('MeguvizMemoryConfig.cmake', 'MeguvizMemoryConfigVersion.cmake',
                     'MeguvizMemoryTargets-release.cmake', 'MeguvizMemoryTargets.cmake'):
            sdk_files['lib/cmake/MeguvizMemory/' + name] = b'cmake'
        sdk = archive(sdk_files)
        jni_dll, transformed, test_cmake = b'jni-dll', b'test-source', b'test-cmake'
        jni = archive({'bin/mgmem_pressure_jni.dll': jni_dll, 'lib/mgmem_pressure_jni.lib': b'lib',
                       'java/NativeMemoryLease.jar': host_jar, 'README.md': b'host jni'})
        comparison = encoded({'source_preserved': True, 'untouched_files_byte_identical': True,
                              'private_marker_scan': 'PASS_ASCII_UTF16LE',
                              'pressure_archive': {'after_sha256': subject.sha(b'pressure-lib')}})
        evidence = encoded({'state': 'PASS_SCOPED_SANITIZED_WINDOWS_SDK_AND_HOST_JNI',
            'scope': 'HOST_INSTALL_CONSUMERS_AND_JVM_NO_DEVICE_NO_PERFORMANCE_NO_RELEASE',
            'sdk_zip': subject.identity(sdk), 'host_jni_zip': subject.identity(jni),
            'sanitization_proof_sha256': subject.sha(comparison),
            'privacy': 'BOUNDED_RECURSIVE_KNOWN_IDENTIFIERS_AND_GENERIC_PATH_SCAN_PASSED',
            'jni_imports_matching_shared_core': True, 'installed_tests': {'count': 10},
            'core_dll_sha256': subject.sha(b'core-dll'), 'jni_dll_sha256': subject.sha(jni_dll),
            'java_jar_sha256': subject.sha(host_jar),
            'java_source_sha256': subject.file_sha(root / subject.HOST_SOURCES['native_lease_java']),
            'cpp_header_sha256': subject.file_sha(root / subject.HOST_SOURCES['cpp_header']),
            'host_jvm': {'state': 'PASS', 'jni_loaded_path': 'OBSERVED_JVM_LIBRARY_LOG',
                'test_source_sha256': subject.file_sha(root / subject.HOST_SOURCES['borrow_jvm_test']),
                'transformed_test_source_sha256': subject.sha(transformed)}})
        scan = encoded({'state': 'PASS_SCOPED_PATH_AND_PRIVATE_IDENTIFIER_SCAN',
            'scope': 'WINDOWS_SDK_HOST_JNI_AND_PROOF_INPUTS_ONLY',
            'files': {'sdk_zip': subject.identity(sdk), 'host_jni_zip': subject.identity(jni),
                'sdk_evidence': subject.identity(evidence), 'binary_comparison': subject.identity(comparison),
                'jni_test_source': subject.identity(transformed), 'jni_test_cmake': subject.identity(test_cmake)}})
        args = SimpleNamespace(windows_sdk_zip=Path('sdk'), windows_jni_zip=Path('jni'),
            windows_evidence=Path('evidence'), windows_comparison=Path('comparison'), windows_path_scan=Path('scan'))
        proof_dir = root / 'artifacts/followup-delivery-20261101/windows'
        fixture = {args.windows_sdk_zip: sdk, args.windows_jni_zip: jni, args.windows_evidence: evidence,
            args.windows_comparison: comparison, args.windows_path_scan: scan,
            proof_dir / 'NativeMemoryLeaseJvmTest.java.txt': transformed,
            proof_dir / 'installed-consumer-CMakeLists.txt': test_cmake}
        original = subject.file_data
        def read(path):
            return fixture[path] if path in fixture else original(path)
        proof = encoded({'class_sha256': subject.sha(class_bytes)})
        with mock.patch.object(subject, 'file_data', side_effect=read):
            data = {'evidence/android/class_and_header.json': proof}
            self.assertEqual(subject.sha(b'core-dll'), subject.check_windows(data, args))
            self.assertEqual(jni, data['windows/MeguvizMemory-host-jni.zip'])
            fixture[args.windows_jni_zip] = archive({'wrong': b'library'})
            with self.assertRaises(ValueError):
                subject.check_windows({'evidence/android/class_and_header.json': proof}, args)

    def test_bridge_binary_requires_exact_tested_core(self):
        header = subject.file_data(subject.ROOT / subject.BRIDGE_SOURCES[3])
        binary = b'synthetic-bridge'
        bundle = archive({'lib/mgmem_compute_read_bridge.lib': binary,
                          'include/mgmem/compute_read_bridge.h': header})
        comparison = encoded({'source_preserved': True, 'untouched_files_byte_identical': True,
                              'bridge_archive': {'after_sha256': subject.sha(binary)}})
        scan = encoded({'state': 'PASS_SCOPED_PATH_PATTERN_SCAN', 'archive_sha256': subject.sha(bundle),
                        'finding_counts': {'private': 0, 'path': 0}})
        test_source = b'synthetic attested test source'
        test_cmake = b'synthetic test CMake'
        sanitization = encoded({'state': 'PASS_SCOPED_SANITIZED_INSTALLED_BRIDGE',
            'sdk_zip': {**subject.identity(bundle), 'entries': 2},
            'binary_comparison_receipt_sha256': subject.sha(comparison),
            'path_scan_receipt_sha256': subject.sha(scan)})
        evidence = encoded({'state': 'PASS_SCOPED_INSTALLED_BRIDGE_WITH_NEW_CORE',
            'installed_consumer': {'core_sha256': 'a' * 64, 'bridge_sha256': subject.sha(binary),
                'test_source_sha256': subject.sha(test_source), 'loaded_core_identity': 'OS_MODULE_PATH_AND_FILE_HASH_MATCH',
                'cases': {'normal': {'exit_code': 0}, 'violation': {'exit_code': 0},
                          'wrong_core_control': {'exit_code': 3}},
                'violation_case': 'ISOLATED_PROCESS_QUARANTINE_NOT_NORMAL_RESOURCE_ZERO'}})
        args = SimpleNamespace(bridge_sdk_zip=Path('archive'), bridge_evidence=Path('evidence'),
                               bridge_test_source=Path('test-source'), bridge_test_cmake=Path('test-cmake'),
                               bridge_sanitization_evidence=Path('sanitization'),
                               bridge_comparison=Path('comparison'), bridge_path_scan=Path('scan'))
        fixture = {args.bridge_sdk_zip: bundle, args.bridge_evidence: evidence,
                   args.bridge_test_source: test_source, args.bridge_test_cmake: test_cmake,
                   args.bridge_sanitization_evidence: sanitization,
                   args.bridge_comparison: comparison, args.bridge_path_scan: scan}
        original = subject.file_data
        def read(path):
            return fixture[path] if path in fixture else original(path)
        with mock.patch.object(subject, 'file_data', side_effect=read):
            windows_evidence = encoded({'bridge_new_core': {'receipt_sha256': subject.sha(evidence),
                'loaded_core_identity': 'OS_MODULE_PATH_AND_FILE_HASH_MATCH', 'core_sha256': 'a' * 64}})
            windows_scan = encoded({'files': {'bridge_evidence': subject.identity(evidence),
                                               'bridge_test_source': subject.identity(test_source),
                                               'bridge_test_cmake': subject.identity(test_cmake)}})
            data = {'evidence/windows/shareable-evidence.json': windows_evidence,
                    'evidence/windows/path-scan.json': windows_scan}
            self.assertTrue(subject.check_bridge(data, args, 'a' * 64))
            self.assertEqual(bundle, data['windows-optional/MeguvizMemory-ComputeReadBridge-sdk.zip'])
            with self.assertRaises(ValueError):
                subject.check_bridge({'evidence/windows/shareable-evidence.json': windows_evidence,
                                      'evidence/windows/path-scan.json': windows_scan}, args, 'b' * 64)
            missing_os_identity = json.loads(evidence)
            del missing_os_identity['installed_consumer']['loaded_core_identity']
            fixture[args.bridge_evidence] = encoded(missing_os_identity)
            changed_windows_evidence = encoded({'bridge_new_core': {
                'receipt_sha256': subject.sha(fixture[args.bridge_evidence]),
                'loaded_core_identity': 'OS_MODULE_PATH_AND_FILE_HASH_MATCH', 'core_sha256': 'a' * 64}})
            changed_windows_scan = encoded({'files': {'bridge_evidence': subject.identity(fixture[args.bridge_evidence]),
                                                       'bridge_test_source': subject.identity(test_source),
                                                       'bridge_test_cmake': subject.identity(test_cmake)}})
            with self.assertRaises(ValueError):
                subject.check_bridge({'evidence/windows/shareable-evidence.json': changed_windows_evidence,
                                      'evidence/windows/path-scan.json': changed_windows_scan}, args, 'a' * 64)


if __name__ == '__main__':
    unittest.main()
