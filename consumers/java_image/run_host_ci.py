#!/usr/bin/env python3
"""Build and exercise the installed-core Java image consumer on Windows/Linux.

Creates a fresh directory only beneath this checkout's ignored build/. It does
not fetch dependencies, operate a device, or modify the supplied Memory SDK.
Raw build/test logs remain in the new directory; the summary contains hashes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
CONSUMER = ROOT / 'consumers/java_image'
ADAPTER_SOURCE = ROOT / 'adapters/android/java/io/github/jichuo1/meguviz/memory/android/NativeMemoryLease.java'


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--memory-sdk', type=Path, required=True, help='Existing installed shared Memory SDK')
    parser.add_argument('--out-dir', type=Path, required=True, help='New output directory inside workspace build/')
    parser.add_argument('--java-home', type=Path, default=os.environ.get('JAVA_HOME'), help='Existing JDK 9+; defaults to JAVA_HOME')
    parser.add_argument('--cmake', help='Existing CMake executable; defaults to PATH')
    parser.add_argument('--generator', help='Optional CMake generator, e.g. Ninja')
    args = parser.parse_args()
    if os.name != 'nt' and not sys.platform.startswith('linux'):
        parser.error('This host fixture supports Windows and Linux')
    root = ROOT.resolve()
    out = args.out_dir.resolve()
    sdk = args.memory_sdk.resolve()
    if not out.is_relative_to((root / 'build').resolve()) or out == (root / 'build').resolve() or out.exists():
        parser.error('Use a fresh child directory within build/')
    cmake_name = args.cmake or shutil.which('cmake')
    if not cmake_name:
        parser.error('An existing CMake executable is required')
    cmake = Path(cmake_name).resolve()
    java_home = Path(args.java_home).resolve() if args.java_home else None
    if java_home is None:
        javac_on_path = shutil.which('javac')
        java_home = Path(javac_on_path).resolve().parent.parent if javac_on_path else None
    if java_home is None:
        parser.error('An existing JDK is required')
    suffix = '.exe' if os.name == 'nt' else ''
    java, javac, jar = (java_home / 'bin' / (name + suffix) for name in ('java', 'javac', 'jar'))
    if not all(path.is_file() for path in (cmake, java, javac, jar)):
        parser.error('CMake and JDK java/javac/jar must exist')
    headers = ('mgmem.h', 'pressure.h', 'android_borrow_jni.h', 'android_pressure_jni.h')
    for name in headers:
        installed = sdk / 'include/mgmem' / name
        source = root / 'include/mgmem' / name
        if not installed.is_file() or installed.read_bytes() != source.read_bytes():
            parser.error('Installed SDK headers do not match the current source contract')
    core_candidates = ([sdk / 'bin/mgmem.dll'] if os.name == 'nt' else
                       [sdk / 'lib/libmgmem.so', sdk / 'lib64/libmgmem.so'])
    cores = [path for path in core_candidates if path.is_file()]
    if len(cores) != 1:
        parser.error('Expected exactly one installed shared Memory core')
    core = cores[0].resolve()
    source_paths = [ADAPTER_SOURCE, root / 'src/mgmem.cpp', root / 'include/mgmem/mgmem.h',
        root / 'include/mgmem/pressure.h', root / 'include/mgmem/android_borrow_jni.h',
        root / 'include/mgmem/android_pressure_jni.h', root / 'adapters/android/native/borrow_jni.cpp',
        root / 'adapters/android/native/pressure_jni.cpp', CONSUMER / 'runtime/CMakeLists.txt',
        CONSUMER / 'CMakeLists.txt', CONSUMER / 'native/image_jni.cpp',
        CONSUMER / 'java/io/github/jichuo1/meguviz/memory/image/JavaImage.java',
        CONSUMER / 'verify_host.py', CONSUMER / 'run_host_ci.py',
        root / 'consumers/image/CMakeLists.txt', root / 'consumers/image/main.cpp']
    before = {path.relative_to(root).as_posix(): digest(path) for path in source_paths}
    core_before = digest(core)
    env = os.environ.copy()
    env['JAVA_HOME'] = str(java_home)
    env['PATH'] = str(java_home / 'bin') + os.pathsep + env.get('PATH', '')
    out.mkdir(parents=True, exist_ok=False)
    logs = out / 'private-logs'; logs.mkdir()
    operations = []

    def run(label: str, command: list[object]) -> None:
        try:
            result = subprocess.run([str(item) for item in command], cwd=root, env=env,
                                    capture_output=True, timeout=300, check=False)
        except subprocess.TimeoutExpired as error:
            (logs / (label + '.stdout')).write_bytes(error.stdout or b'')
            (logs / (label + '.stderr')).write_bytes(error.stderr or b'')
            raise RuntimeError(label + '_timeout') from error
        (logs / (label + '.stdout')).write_bytes(result.stdout)
        (logs / (label + '.stderr')).write_bytes(result.stderr)
        operations.append({'name': label, 'exit_code': result.returncode,
            'stdout_sha256': hashlib.sha256(result.stdout).hexdigest(),
            'stderr_sha256': hashlib.sha256(result.stderr).hexdigest()})
        if result.returncode:
            raise RuntimeError(label + '_failed_see_private_logs')

    def configure(label: str, source: Path, build: Path, *options: str) -> None:
        command = [cmake, '-S', source, '-B', build]
        if args.generator:
            command += ['-G', args.generator]
        command += ['-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_PREFIX_PATH=' + str(sdk), *options]
        run(label, command)

    jni_build, jni_prefix = out / 'jni-build', out / 'jni-install'
    configure('configure-jni', CONSUMER / 'runtime', jni_build, '-DCMAKE_INSTALL_PREFIX=' + str(jni_prefix))
    run('build-jni', [cmake, '--build', jni_build, '--config', 'Release', '--parallel', '2'])
    run('install-jni', [cmake, '--install', jni_build, '--config', 'Release'])
    jni = jni_prefix / ('bin/mgmem_pressure_jni.dll' if os.name == 'nt' else 'lib/libmgmem_pressure_jni.so')
    if not jni.is_file():
        raise RuntimeError('installed_shared_jni_missing')

    adapter_classes = out / 'adapter-classes'; adapter_classes.mkdir()
    run('compile-current-adapter', [javac, '--release', '8', '-d', adapter_classes, ADAPTER_SOURCE])
    adapter_jar = out / 'adapter.jar'
    run('package-current-adapter', [jar, '--create', '--file', adapter_jar, '-C', adapter_classes, '.'])

    consumer_build = out / 'consumer-build'
    configure('configure-consumer', CONSUMER, consumer_build,
              '-DMGMEM_JNI_PREFIX=' + str(jni_prefix), '-DMGMEM_ANDROID_CLASSES_JAR=' + str(adapter_jar))
    run('build-consumer', [cmake, '--build', consumer_build, '--config', 'Release', '--parallel', '2'])
    location = consumer_build / 'java-image-library-Release.txt'
    if not location.is_file():
        raise RuntimeError('consumer_target_location_missing')
    consumer_jni = Path(location.read_text(encoding='utf-8').strip()).resolve()
    if not consumer_jni.is_relative_to(consumer_build.resolve()) or not consumer_jni.is_file():
        raise RuntimeError('consumer_target_location_invalid')

    reference_build = out / 'reference-build'
    configure('configure-reference', root / 'consumers/image', reference_build)
    run('build-reference', [cmake, '--build', reference_build, '--config', 'Release', '--target', 'mgmem_image', '--parallel', '2'])
    exe_name = 'mgmem_image.exe' if os.name == 'nt' else 'mgmem_image'
    reference_candidates = [reference_build / exe_name, reference_build / 'Release' / exe_name]
    references = [candidate for candidate in reference_candidates if candidate.is_file()]
    if len(references) != 1:
        raise RuntimeError('reference_target_location_ambiguous')

    verification = out / 'verification'
    run('verify-installed-consumer', [sys.executable, CONSUMER / 'verify_host.py',
        '--java', java, '--javac', javac, '--reference', references[0], '--core', core,
        '--jni', jni, '--consumer-jni', consumer_jni, '--classes', consumer_build / 'classes',
        '--adapter-jar', adapter_jar, '--out-dir', verification])
    report = json.loads((verification / 'receipt.json').read_text(encoding='utf-8'))
    if (report.get('state') != 'PASS_HOST_FUNCTIONAL_DEVELOPMENT' or len(report.get('cases', [])) != 12 or
            len(report.get('negative_cases', [])) != 8 or report.get('identities', {}).get('core_sha256') != core_before or
            report.get('identities', {}).get('jni_sha256') != digest(jni) or
            report.get('identities', {}).get('java_jar_sha256') != digest(adapter_jar)):
        raise RuntimeError('consumer_verification_scope_or_identity_mismatch')
    if digest(core) != core_before or any(digest(path) != before[path.relative_to(root).as_posix()] for path in source_paths):
        raise RuntimeError('source_or_installed_core_changed_during_run')
    result = {'schema_version': 1, 'state': 'PASS_INSTALLED_SHARED_HOST_JAVA_IMAGE',
        'scope': 'HOST_FUNCTIONAL_NO_ANDROID_NO_PERFORMANCE', 'host': 'windows' if os.name == 'nt' else 'linux',
        'functional_cases': len(report['cases']), 'negative_cases': len(report['negative_cases']),
        'installed_core_sha256': core_before, 'installed_jni_sha256': digest(jni),
        'adapter_jar_sha256': digest(adapter_jar), 'adapter_class_sha256': report['identities']['java_class_sha256'],
        'consumer_jni_sha256': digest(consumer_jni), 'reference_sha256': digest(references[0]),
        'verification_receipt_sha256': digest(verification / 'receipt.json'),
        'source_sha256': before, 'operations': operations,
        'limitations': ['The host JNI is separately built from the adapter sources; it is not the Android AAR JNI binary.',
            'Module paths and current file hashes were compared inside the JVM; mapped executable bytes were not hashed.',
            'Source and file hashes alone are not reproducible-build provenance or a performance claim.',
            'No device, ART, BIL, energy or formal release qualification is implied.']}
    with (out / 'receipt.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'state': result['state'], 'host': result['host'],
                      'functional_cases': result['functional_cases'], 'negative_cases': result['negative_cases']}))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        # Paths and command output remain in the ignored local build directory.
        print(json.dumps({'state': 'FAILED_OR_BLOCKED', 'reason': str(error) if isinstance(error, RuntimeError) else type(error).__name__}))
        sys.exit(1)
