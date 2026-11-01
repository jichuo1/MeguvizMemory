#!/usr/bin/env python3
"""Compile the sample against a selected Android AAR; no device or ART execution."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'consumers/android_managed/java/io/github/jichuo1/meguviz/memory/sample/ManagedPressureSample.java'
REQUIRED_CLASSES = (
    'ManagedMemoryDomain.class', 'ManagedMemoryDomain$Pressure.class',
    'ManagedMemoryDomain$Allocation.class', 'ManagedMemoryDomain$Lease.class',
    'ManagedPressureBackend.class', 'MemoryPressureAdapter.class', 'NativeMemoryLease.class',
)
PREFIX = 'io/github/jichuo1/meguviz/memory/android/'
MAX_CLASSES_BYTES = 64 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def extract_classpath(aar):
    with zipfile.ZipFile(aar) as archive:
        names = archive.namelist()
        if names.count('classes.jar') != 1:
            raise ValueError('AAR must have exactly one classes.jar')
        info = archive.getinfo('classes.jar')
        if info.file_size > MAX_CLASSES_BYTES:
            raise ValueError('classes.jar exceeds the sample verifier limit')
        core = {name.split('/')[1] for name in names
                if name.startswith('jni/') and name.endswith('/libmgmem.so') and len(name.split('/')) == 3}
        jni = {name.split('/')[1] for name in names
               if name.startswith('jni/') and name.endswith('/libmgmem_pressure_jni.so') and len(name.split('/')) == 3}
        advertised = {name.split('/')[1] for name in names
                      if name.startswith('jni/') and name.endswith('.so') and len(name.split('/')) == 3}
        if not core or '' in core or core != jni or core != advertised:
            raise ValueError('AAR must contain both Memory core and JNI for every advertised ABI')
        native_names = ['jni/' + abi + '/' + library for abi in core
                        for library in ('libmgmem.so', 'libmgmem_pressure_jni.so')]
        if any(names.count(name) != 1 or archive.getinfo(name).file_size == 0 for name in native_names):
            raise ValueError('AAR native libraries must be unique and nonempty')
        return archive.read(info), sorted(core)


def verify_classes(classes):
    if len(classes) > MAX_CLASSES_BYTES:
        raise ValueError('classes.jar exceeds the sample verifier limit')
    with zipfile.ZipFile(io.BytesIO(classes)) as archive:
        names = archive.namelist()
        missing = [name for name in REQUIRED_CLASSES if names.count(PREFIX + name) != 1]
        if missing:
            raise ValueError('classes.jar lacks required managed pressure API classes: ' + ', '.join(missing))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--aar', type=Path, help='Selected AAR containing current managed pressure API')
    source.add_argument('--classes-jar', type=Path, help='Development-only source-built classes.jar')
    parser.add_argument('--android-jar', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True, help='New isolated output directory')
    parser.add_argument('--javac', type=Path)
    args = parser.parse_args()
    javac = str(args.javac) if args.javac else shutil.which('javac')
    if not javac:
        parser.error('javac is required')
    if not args.android_jar.is_file():
        parser.error('an existing Android platform android.jar is required')
    selected = args.aar if args.aar else args.classes_jar
    if not selected.is_file():
        parser.error('selected AAR/classes.jar does not exist')
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    receipt = {'scope': 'ANDROID_JAVA_COMPILE_ONLY_NO_RUNTIME', 'status': 'FAIL',
               'input_kind': 'AAR' if args.aar else 'DEVELOPMENT_CLASSES_JAR',
               'input_sha256': digest(selected.read_bytes()),
               'android_jar_sha256': digest(args.android_jar.read_bytes()),
               'sample_source_sha256': digest(SOURCE.read_bytes()),
               'verifier_sha256': digest(Path(__file__).read_bytes()),
               'android_runtime': 'NOT_RUN', 'device': 'NOT_RUN', 'performance': 'NOT_MEASURED'}
    try:
        if args.aar:
            classes, abis = extract_classpath(args.aar)
            receipt['aar_abis_with_core_and_jni'] = abis
        else:
            classes = args.classes_jar.read_bytes()
        verify_classes(classes)
        receipt['classes_jar_sha256'] = digest(classes)
        jar = out / 'classes.jar'
        jar.write_bytes(classes)
        compiled = out / 'compiled'
        compiled.mkdir()
        command = [javac, '--release', '8', '-classpath', str(args.android_jar.resolve()) + os.pathsep + str(jar),
                   '-d', str(compiled), str(SOURCE)]
        result = subprocess.run(command, capture_output=True, timeout=60, check=False)
        (out / 'javac.stderr').write_bytes(result.stderr)
        receipt['javac_exit_code'] = result.returncode
        if result.returncode:
            receipt['reason'] = 'JAVAC_FAILED_SEE_LOCAL_STDERR'
        else:
            output = compiled / 'io/github/jichuo1/meguviz/memory/sample/ManagedPressureSample.class'
            if not output.is_file():
                raise ValueError('expected compiled sample class missing')
            receipt['sample_class_sha256'] = digest(output.read_bytes())
            receipt['status'] = 'PASS'
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
        # Keep diagnostic text local: it may contain paths from compiler errors.
        (out / 'diagnostic.txt').write_text(str(error), encoding='utf-8')
        receipt['reason'] = 'INPUT_OR_COMPILE_ERROR_SEE_LOCAL_DIAGNOSTIC'
    (out / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': receipt['status'], 'scope': receipt['scope'], 'input_kind': receipt['input_kind']}))
    return 0 if receipt['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
