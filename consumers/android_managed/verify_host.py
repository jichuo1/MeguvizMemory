#!/usr/bin/env python3
"""Run the sample with a previously built host JNI and Android type stubs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'consumers/android_managed/java/io/github/jichuo1/meguviz/memory/sample/ManagedPressureSample.java'
TEST = ROOT / 'consumers/android_managed/tests/ManagedPressureSampleHostTest.java'
EXPECTED = 'PASS: host JNI, synthetic event, and stub callback lifecycle; no Android runtime'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture-classes', type=Path, required=True,
                        help='classes directory from tests/managed_host')
    parser.add_argument('--fixture-jni', type=Path, required=True,
                        help='directory containing the managed host JNI library')
    parser.add_argument('--out-dir', type=Path, required=True, help='New isolated output directory')
    parser.add_argument('--javac', type=Path)
    parser.add_argument('--java', type=Path)
    args = parser.parse_args()
    javac = str(args.javac) if args.javac else shutil.which('javac')
    java = str(args.java) if args.java else shutil.which('java')
    if not javac or not java:
        parser.error('javac and java are required')
    fixture = args.fixture_classes.resolve()
    managed = fixture / 'io/github/jichuo1/meguviz/memory/android/ManagedMemoryDomain.class'
    stub = fixture / 'android/content/Context.class'
    native_dir = args.fixture_jni.resolve()
    names = ('mgmem_pressure_jni.dll', 'libmgmem_pressure_jni.so', 'libmgmem_pressure_jni.dylib')
    libraries = [native_dir / name for name in names if (native_dir / name).is_file()]
    if not managed.is_file() or not stub.is_file() or len(libraries) != 1:
        parser.error('fixture classes, Android stubs and exactly one host JNI library are required')
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    receipt = {'scope': 'HOST_JVM_JNI_AND_SYNTHETIC_ANDROID_STUBS_ONLY', 'status': 'FAIL',
               'sample_source_sha256': digest(SOURCE), 'test_source_sha256': digest(TEST),
               'verifier_sha256': digest(Path(__file__)), 'fixture_managed_class_sha256': digest(managed),
               'fixture_context_stub_sha256': digest(stub), 'fixture_native_sha256': digest(libraries[0]),
               'android_runtime': 'NOT_RUN', 'device': 'NOT_RUN', 'performance': 'NOT_MEASURED'}
    try:
        compiled = out / 'classes'
        compiled.mkdir()
        compile_result = subprocess.run([javac, '--release', '8', '-cp', str(fixture), '-d', str(compiled),
                                         str(SOURCE), str(TEST)], capture_output=True, timeout=60, check=False)
        (out / 'javac.stderr').write_bytes(compile_result.stderr)
        receipt['javac_exit_code'] = compile_result.returncode
        if compile_result.returncode:
            receipt['reason'] = 'JAVAC_FAILED_SEE_LOCAL_STDERR'
        else:
            test = subprocess.run([java, '-Djava.library.path=' + str(native_dir), '-cp',
                                   str(compiled) + os.pathsep + str(fixture),
                                   'io.github.jichuo1.meguviz.memory.sample.ManagedPressureSampleHostTest'],
                                  capture_output=True, timeout=30, check=False)
            (out / 'test.stdout').write_bytes(test.stdout)
            (out / 'test.stderr').write_bytes(test.stderr)
            receipt['test_exit_code'] = test.returncode
            if test.returncode == 0 and test.stdout.decode('utf-8').strip() == EXPECTED:
                receipt['status'] = 'PASS'
            else:
                receipt['reason'] = 'HOST_TEST_FAILED_SEE_LOCAL_OUTPUT'
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        (out / 'diagnostic.txt').write_text(str(error), encoding='utf-8')
        receipt['reason'] = 'LOCAL_RUN_ERROR_SEE_LOCAL_DIAGNOSTIC'
    (out / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': receipt['status'], 'scope': receipt['scope']}))
    return 0 if receipt['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
