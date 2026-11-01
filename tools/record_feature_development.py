#!/usr/bin/env python3
"""Record scoped feature-development evidence without publishing raw logs.

Reads existing CTest/build inputs and executes only the already-built local
installed bridge consumer. This tool does not build, use ADB, measure product
performance or claim that a current file hash proves its build provenance.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build/feature-20261101'
OUTPUT = ROOT / 'artifacts/feature-development-20261101/host-receipt-v2.json'
HOST = [
    'ownership_budget_close', 'c_abi_consumer', 'cpp_raii_consumer', 'external_import',
    'external_import_poison_retain', 'external_import_poison_release', 'external_import_poison_alias',
    'checked_external_normal', 'checked_external_failure-owner', 'checked_external_failure-lease',
    'checked_external_exception', 'checked_external_rollback', 'checked_external_race-checked',
    'checked_external_race-legacy', 'checked_external_c_consumer', 'pressure_budget_close',
    *('jni_lock_' + x for x in ('callback-reentry', 'single-publisher', 'attach-retry',
        'java-exception-retry', 'error-quarantine', 'foreign-release', 'foreign-release-bind',
        'foreign-release-snapshot', 'auto-attach-recovery', 'auto-java-recovery',
        'auto-concurrent-poll', 'auto-error-quarantine', 'binding-exception-matrix',
        'snapshot-error-external', 'snapshot-error-owned', 'snapshot-state-external',
        'snapshot-state-owned', 'snapshot-header-external', 'snapshot-header-owned')),
    'original_abi10_consumer', 'runtime_origin',
]
BORROW = ['borrow_jni_host', 'borrow_jni_real_jvm']
BRIDGE = ['compute_read_bridge', 'compute_read_bridge_unmap_violation',
          'compute_read_bridge_fake_pre_admission', 'compute_read_bridge_fake_close_race']


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def fingerprint(path):
    data = path.read_bytes()
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def expected_executable(name):
    if name.startswith('jni_lock_'): return 'mgmem_jni_lock_tests.exe'
    if name.startswith('checked_external_'):
        return 'mgmem_checked_import_c.exe' if name == 'checked_external_c_consumer' else 'mgmem_checked_import_tests.exe'
    if name.startswith('external_import'): return 'mgmem_import_tests.exe'
    return {
        'ownership_budget_close': 'mgmem_core_tests.exe', 'c_abi_consumer': 'mgmem_c_consumer.exe',
        'cpp_raii_consumer': 'mgmem_cpp_consumer.exe', 'pressure_budget_close': 'mgmem_pressure_tests.exe',
        'original_abi10_consumer': 'mgmem_abi10_consumer.exe', 'runtime_origin': 'mgmem_runtime_tests.exe',
        'borrow_jni_host': 'mgmem_borrow_jni_host_tests.exe', 'borrow_jni_real_jvm': 'java.exe',
        'compute_read_bridge': 'mgmem_compute_read_bridge_tests.exe',
        'compute_read_bridge_unmap_violation': 'mgmem_compute_read_bridge_tests.exe',
        'compute_read_bridge_fake_pre_admission': 'mgmem_compute_fake_pre_admission.exe',
        'compute_read_bridge_fake_close_race': 'mgmem_compute_fake_close_race.exe',
    }[name]


def check_log(name, expected):
    path = BUILD / name / 'Testing/Temporary/LastTest.log'
    raw = path.read_bytes()
    text = raw.decode('utf-8-sig').replace('\r\n', '\n')
    require(len(re.findall(r'^Start testing: .+$', text, re.M)) == 1 and
            len(re.findall(r'^End testing: .+$', text, re.M)) == 1 and
            re.match(r'^Start testing: .+\n', text) is not None and
            re.search(r'^End testing: .+\n?\Z', text, re.M) is not None,
            name + '_incomplete_log')
    headers = list(re.finditer(r'^(\d+)/(\d+) Testing: ([^\n]+)$', text, re.M))
    require(len(headers) == len(expected), name + '_case_count')
    results = []
    binary_hashes = {}
    for index, (header, case) in enumerate(zip(headers, expected), 1):
        require(header.group(1) == str(index) and header.group(2) == str(len(expected)) and
                header.group(3) == case, name + '_case_order_or_name')
        end = headers[index].start() if index < len(headers) else text.rindex('End testing:')
        section = text[header.end():end]
        require(len(re.findall(r'^' + str(index) + '/' + str(len(expected)) + r' Test: ' + re.escape(case) + r'$', section, re.M)) == 1 and
                len(re.findall(r'^Test Passed\.$', section, re.M)) == 1 and
                not re.search(r'^Test (?:Failed|Not Run)\.', section, re.M) and
                len(re.findall(r'^<end of output>$', section, re.M)) == 1 and
                len(re.findall(r'^"' + re.escape(case) + r'" start time:', section, re.M)) == 1 and
                len(re.findall(r'^"' + re.escape(case) + r'" end time:', section, re.M)) == 1 and
                len(re.findall(r'^"' + re.escape(case) + r'" time elapsed:', section, re.M)) == 1,
                name + '_test_incomplete_or_failed')
        commands = re.findall(r'^Command: "([^"\n]+)"', section, re.M)
        require(len(commands) == 1 and len(re.findall(r'^Directory: .+$', section, re.M)) == 1,
                name + '_test_command_missing')
        exe = Path(commands[0]).resolve()
        require(exe.name.lower() == expected_executable(case).lower() and exe.is_file(), name + '_test_binary_mismatch')
        if case != 'borrow_jni_real_jvm':
            require(exe.parent == (BUILD / name).resolve(), name + '_test_binary_outside_suite')
        else:
            require(exe.name.lower() == 'java.exe', name + '_java_binary_mismatch')
        binary_hashes[exe.name.lower()] = fingerprint(exe)
        results.append(case)
    require(len(re.findall(r'^Test Passed\.$', text, re.M)) == len(expected) and
            not re.search(r'^Test (?:Failed|Not Run)\.', text, re.M), name + '_unexpected_result')
    return {'status': 'PASS', 'total': len(expected), 'passed': len(expected),
            'test_names': results, 'log_sha256': hashlib.sha256(raw).hexdigest(),
            'current_test_binary_fingerprints': binary_hashes}


def cmake_cache_value(path, key):
    text = path.read_text(encoding='utf-8')
    rows = re.findall(r'^' + re.escape(key) + r':[^=]+=(.*)$', text, re.M)
    require(len(rows) == 1, 'cmake_option_missing_or_ambiguous')
    return rows[0].strip()


def check_aar(abi):
    directory = BUILD / ('aar-' + abi)
    aar = directory / 'MeguvizMemory-Android-Pressure-0.2.0.aar'
    receipt = json.loads((BUILD / ('aar-' + abi + '-receipt.json')).read_text(encoding='utf-8'))
    require(receipt.get('status') == 'PASS' and receipt.get('abi') == abi and
            receipt.get('device_executed') is False and receipt.get('performance_qualified') is False,
            'aar_build_receipt_scope')
    artifacts = [x for x in receipt['artifacts'] if x['path'].endswith('.aar')]
    require(len(artifacts) == 1 and artifacts[0]['sha256'] == fingerprint(aar)['sha256'] and
            artifacts[0]['bytes'] == aar.stat().st_size, 'aar_receipt_bytes_mismatch')
    jni = (BUILD / ('jni-' + abi) / 'libmgmem_pressure_jni.so').read_bytes()
    core = (BUILD / ('sdk-' + abi) / 'lib/libmgmem.so').read_bytes()
    machine = {'arm64-v8a': 183, 'armeabi-v7a': 40, 'x86_64': 62}[abi]
    require(all(x[:4] == b'\x7fELF' and x[5] == 1 and int.from_bytes(x[18:20], 'little') == machine
                for x in (jni, core)), 'aar_elf_abi_mismatch')
    with zipfile.ZipFile(aar) as z:
        names = z.namelist()
        require(len(names) == len(set(names)) and
                sorted(x for x in names if x.endswith('.so')) == sorted([
                    'jni/' + abi + '/libmgmem_pressure_jni.so', 'jni/' + abi + '/libmgmem.so']),
                'aar_native_closure_mismatch')
        require(z.read('jni/' + abi + '/libmgmem_pressure_jni.so') == jni and
                z.read('jni/' + abi + '/libmgmem.so') == core and
                z.read('proguard.txt') == (ROOT / 'adapters/android/consumer-rules.pro').read_bytes() and
                z.read('META-INF/NOTICE-LLVM.txt') == (ROOT / 'third_party_notices/llvm-NOTICE.txt').read_bytes(),
                'aar_member_mismatch')
        with zipfile.ZipFile(io.BytesIO(z.read('classes.jar'))) as classes:
            require('io/github/jichuo1/meguviz/memory/android/NativeMemoryLease.class' in classes.namelist(),
                    'aar_borrow_class_missing')
    return {'aar': fingerprint(aar), 'jni': fingerprint(BUILD / ('jni-' + abi) / 'libmgmem_pressure_jni.so'),
            'core': fingerprint(BUILD / ('sdk-' + abi) / 'lib/libmgmem.so'),
            'packaging_receipt_sha256': fingerprint(BUILD / ('aar-' + abi + '-receipt.json'))['sha256'],
            'checked': ['abi_headers', 'native_members', 'borrow_class', 'keep_rules', 'llvm_notice'],
            'artifact_privacy': 'LOCAL_UNSANITIZED_NOT_SHAREABLE',
            'scope': 'BUILD_AND_PACKAGING_ONLY_NOT_DEVICE_EXECUTION'}


def run_installed_consumer():
    binary = BUILD / 'bridge-consumer/installed_bridge_consumer.exe'
    require(binary.is_file(), 'installed_consumer_missing')
    environment = os.environ.copy()
    environment['PATH'] = str(BUILD / 'install-host/bin') + os.pathsep + environment.get('PATH', '')
    result = {}
    for name, args, marker in (
        ('normal', [], b'PASS scoped full READ-map adapter / real CPU reference SDK'),
        ('unmap_violation', ['--violate-unmap'], b'PASS scoped checked-release quarantine; fixture intentionally retained until process exit'),
    ):
        run = subprocess.run([str(binary), *args], cwd=BUILD / 'bridge-consumer', env=environment,
                             capture_output=True, timeout=30, check=False)
        require(run.returncode == 0 and marker in run.stdout, 'installed_consumer_' + name + '_failed')
        result[name] = {'status': 'PASS', 'exit_code': run.returncode,
                        'stdout_sha256': hashlib.sha256(run.stdout).hexdigest(),
                        'stderr_sha256': hashlib.sha256(run.stderr).hexdigest()}
    return {'status': 'PASS_SCOPED_LOCAL_INSTALLED_CONSUMER', 'binary': fingerprint(binary), 'cases': result,
            'quarantine_case': 'ISOLATED_PROCESS_NOT_NORMAL_RESOURCE_ZERO'}


def main():
    require(not OUTPUT.exists(), 'receipt_already_exists')
    require(cmake_cache_value(BUILD / 'host-asan/CMakeCache.txt', 'MGMEM_SANITIZE') == 'ON', 'asan_not_enabled')
    require(cmake_cache_value(BUILD / 'host/CMakeCache.txt', 'MGMEM_SANITIZE') == 'OFF', 'release_not_plain')
    suites = {name: check_log(name, cases) for name, cases in (
        ('host', HOST), ('host-asan', HOST), ('borrow-host', BORROW), ('bridge-host', BRIDGE))}
    sources = {
        'core_c_header': 'include/mgmem/mgmem.h', 'core_implementation': 'src/mgmem.cpp',
        'checked_import_tests': 'tests/checked_import_tests.cpp', 'checked_import_c_consumer': 'tests/checked_import_c.c',
        'borrow_header': 'include/mgmem/android_borrow_jni.h', 'borrow_jni': 'adapters/android/native/borrow_jni.cpp',
        'borrow_java': 'adapters/android/java/io/github/jichuo1/meguviz/memory/android/NativeMemoryLease.java',
        'borrow_jni_host_tests': 'tests/borrow_jni_host_tests.cpp', 'borrow_java_test': 'tests/NativeMemoryLeaseJvmTest.java',
        'borrow_java_fixture': 'tests/borrow_jni_java_fixture.cpp',
        'bridge_header': 'adapters/compute/include/mgmem/compute_read_bridge.h',
        'bridge_source': 'adapters/compute/src/compute_read_bridge.cpp',
        'bridge_tests': 'adapters/compute/tests/read_bridge_tests.cpp',
        'bridge_fake_pre_admission': 'adapters/compute/tests/fake_pre_admission.cpp',
        'bridge_fake_close_race': 'adapters/compute/tests/fake_close_race.cpp',
        'core_cmake': 'CMakeLists.txt', 'android_cmake': 'adapters/android/CMakeLists.txt',
        'bridge_cmake': 'adapters/compute/CMakeLists.txt', 'android_keep_rules': 'adapters/android/consumer-rules.pro',
        'android_packager': 'tools/build_pressure_android.py', 'receipt_generator': 'tools/record_feature_development.py',
    }
    current_sources = {key: fingerprint(ROOT / value) for key, value in sources.items()}
    binaries = {
        'host_core': fingerprint(BUILD / 'host/mgmem.dll'),
        'host_asan_core': fingerprint(BUILD / 'host-asan/mgmem.dll'),
        'host_installed_core': fingerprint(BUILD / 'install-host/bin/mgmem.dll'),
        'borrow_host_jni': fingerprint(BUILD / 'borrow-host/mgmem_pressure_jni.dll'),
        'installed_bridge': fingerprint(BUILD / 'bridge-install/lib/mgmem_compute_read_bridge.lib'),
        'compute_cpu_sdk': fingerprint(ROOT / 'build/compute-current-sdk/lib/meguviz_compute_core.lib'),
        'compute_sdk_header': fingerprint(ROOT / 'build/compute-current-sdk/include/meguviz/compute.h'),
    }
    require(binaries['host_core'] == binaries['host_installed_core'], 'host_installed_core_mismatch')
    aars = {abi: check_aar(abi) for abi in ('arm64-v8a', 'armeabi-v7a', 'x86_64')}
    installed_consumer = run_installed_consumer()
    receipt = {
        'schema_version': 1, 'state': 'PASS_SCOPED_LOCAL_FEATURE_EVIDENCE',
        'scope': 'HOST_TESTS_INSTALLED_HOST_CONSUMER_AND_ANDROID_BUILD_ONLY',
        'suites': suites, 'host_asan_configuration': {'MGMEM_SANITIZE': 'ON'},
        'source_fingerprints_observed_at_recording': current_sources,
        'binary_fingerprints_observed_at_recording': binaries,
        'android_aar_build_only': aars, 'installed_consumer': installed_consumer,
        'device_execution': 'NOT_EXECUTED', 'performance_qualification': 'NOT_MEASURED',
        'formal_release': 'NOT_QUALIFIED', 'BIL': 'NOT_INTEGRATED',
        'provenance_limit': 'Current fingerprints and log digests identify observed files only; a hash alone does not establish build or execution provenance.',
        'other_limits': ['Fake-origin tests are not real Compute runtime tests.',
                         'Quarantined violation fixtures do not prove normal resource reclamation.',
                         'The three AARs are separate single-ABI local unsanitized artifacts containing private environment identifiers; they must not be publicly distributed.',
                         'These artifact hashes record local identity and do not indicate shareability or Android runtime qualification.'],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open('x', encoding='utf-8', newline='\n') as out:
        json.dump(receipt, out, ensure_ascii=False, indent=2)
        out.write('\n')
    print(json.dumps({'state': receipt['state'], 'suite_counts': {k: v['passed'] for k, v in suites.items()},
                      'installed_consumer': installed_consumer['status'], 'android_aar_count': len(aars)}))


if __name__ == '__main__':
    main()
