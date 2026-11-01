#!/usr/bin/env python3
"""Build only an isolated NativeMemoryLease instrumentation APK; never runs ADB.

Consumes a prebuilt pressure AAR plus matching installed shared core SDK. The
fixture links those shared libraries; it must not embed another Memory core.
Output, diagnostic signing key and raw tool logs remain in a new build/ folder.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('sdk', 'java-home', 'ndk', 'ndk-prebuilt', 'cmake', 'ninja', 'memory-sdk', 'aar', 'out-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--abi', choices=('arm64-v8a', 'x86_64'), default='arm64-v8a')
    parser.add_argument('--build-tools', default='37.0.0')
    parser.add_argument('--platform', default='android-37.0')
    parser.add_argument('--r8', action='store_true', help='Shrink the real AAR using its original consumer rules')
    args = parser.parse_args()
    out = args.out_dir.resolve()
    if not out.is_relative_to((ROOT / 'build').resolve()) or out.exists():
        parser.error('Use a new output directory inside ignored build/')
    if not re.fullmatch(r'\d+(?:\.\d+)*', args.build_tools) or not re.fullmatch(r'android-\d+(?:\.\d+)?', args.platform):
        parser.error('Unexpected SDK or build-tools version')
    suffix = '.exe' if os.name == 'nt' else ''
    tools = args.sdk / 'build-tools' / args.build_tools
    android = args.sdk / 'platforms' / args.platform / 'android.jar'
    java, javac, jar, keytool = (args.java_home / 'bin' / (name + suffix) for name in ('java', 'javac', 'jar', 'keytool'))
    aapt, zipalign = (tools / (name + suffix) for name in ('aapt2', 'zipalign'))
    d8, signer = tools / 'lib/d8.jar', tools / 'lib/apksigner.jar'
    dexdump = tools / ('dexdump' + suffix)
    readelf = args.ndk_prebuilt / 'bin' / ('llvm-readelf' + suffix)
    toolchain = args.ndk / 'build/cmake/android.toolchain.cmake'
    core = args.memory_sdk / 'lib/libmgmem.so'
    required = [java, javac, jar, keytool, aapt, zipalign, d8, signer, android, readelf,
                toolchain, args.cmake, args.ninja, core, args.aar,
                args.memory_sdk / 'include/mgmem/android_borrow_jni.h']
    if args.r8:
        required.append(dexdump)
    if not all(path.is_file() for path in required):
        parser.error('Existing JDK, SDK, NDK, AAR and installed shared Memory SDK required')

    with zipfile.ZipFile(args.aar) as package:
        names = package.namelist()
        if len(names) != len(set(names)):
            raise ValueError('AAR has duplicate entries')
        expected = ['jni/' + args.abi + '/libmgmem.so', 'jni/' + args.abi + '/libmgmem_pressure_jni.so']
        if sorted(n for n in names if n.startswith('jni/' + args.abi + '/') and n.endswith('.so')) != sorted(expected):
            raise ValueError('Expected exactly core and JNI shared libraries for the selected ABI')
        core_bytes, jni_bytes, classes_bytes = (package.read(n) for n in (expected[0], expected[1], 'classes.jar'))
        rules_bytes = package.read('proguard.txt') if args.r8 else None
    if core_bytes != core.read_bytes():
        raise ValueError('AAR core is not identical to the installed core used to build the fixture')

    out.mkdir(parents=True)
    inputs = out / 'inputs'; inputs.mkdir()
    (inputs / 'classes.jar').write_bytes(classes_bytes)
    (inputs / 'libmgmem_pressure_jni.so').write_bytes(jni_bytes)
    (inputs / 'libmgmem.so').write_bytes(core_bytes)
    if args.r8:
        (inputs / 'proguard.txt').write_bytes(rules_bytes)
    with zipfile.ZipFile(inputs / 'classes.jar') as classes:
        names = classes.namelist()
        if len(names) != len(set(names)) or names.count('io/github/jichuo1/meguviz/memory/android/NativeMemoryLease.class') != 1:
            raise ValueError('AAR must contain exactly one NativeMemoryLease class')

    operations = []
    def run(label, command):
        result = subprocess.run([str(part) for part in command], capture_output=True, text=True, check=False)
        (out / (label + '.stdout')).write_text(result.stdout, encoding='utf-8')
        (out / (label + '.stderr')).write_text(result.stderr, encoding='utf-8')
        operations.append({'operation': label, 'exit_code': result.returncode,
                           'stdout_sha256': sha(result.stdout.encode()), 'stderr_sha256': sha(result.stderr.encode())})
        if result.returncode:
            raise RuntimeError('Build stage failed: ' + label + '; inspect private build logs')
        return result.stdout

    fixture_source = ROOT / 'tests/android_borrow'
    build = out / 'native'
    run('fixture-configure', [args.cmake, '-S', fixture_source, '-B', build, '-G', 'Ninja',
        '-DCMAKE_MAKE_PROGRAM=' + str(args.ninja.resolve()), '-DCMAKE_BUILD_TYPE=Release',
        '-DCMAKE_TOOLCHAIN_FILE=' + str(toolchain.resolve()), '-DANDROID_ABI=' + args.abi,
        '-DANDROID_PLATFORM=android-27', '-DANDROID_STL=c++_static',
        '-DMGMEM_ANDROID_SDK_DIR=' + str(args.memory_sdk.resolve()),
        '-DMGMEM_ANDROID_JNI_LIBRARY=' + str((inputs / 'libmgmem_pressure_jni.so').resolve())])
    run('fixture-build', [args.cmake, '--build', build, '--parallel', '2'])
    fixture = build / 'libmgmem_borrow_fixture.so'
    if not fixture.is_file():
        raise RuntimeError('Fixture shared library missing')
    expected_machine = {'arm64-v8a': 'AArch64', 'x86_64': 'Advanced Micro Devices X86-64'}[args.abi]
    for label, path in [('core', inputs / 'libmgmem.so'), ('jni', inputs / 'libmgmem_pressure_jni.so'), ('fixture', fixture)]:
        header = run('readelf-header-' + label, [readelf, '-h', path])
        if not re.search(r'Machine:\s*' + re.escape(expected_machine), header):
            raise RuntimeError('ELF machine mismatch: ' + label)
    dynamic = run('readelf-fixture-dynamic', [readelf, '-d', fixture])
    needed = re.findall(r'Shared library:\s*\[([^\]]+)\]', dynamic)
    allowed_needed = {'libmgmem.so', 'libmgmem_pressure_jni.so', 'libm.so', 'libdl.so', 'libc.so'}
    if not {'libmgmem.so', 'libmgmem_pressure_jni.so'}.issubset(needed) or set(needed) - allowed_needed:
        raise RuntimeError('Fixture does not link exactly the intended shared Memory/JNI runtime closure')
    prefix = 'Java_io_github_jichuo1_meguviz_memory_android_'
    expected_exports = {
        'fixture': [prefix + 'NativeBorrowInstrumentation_' + name for name in
                    ('nativeCreate', 'nativeBeginClose', 'nativeSnapshot', 'nativeCollectBusy', 'nativeFinish', 'nativeCleanupBestEffort')],
        'jni': ['mgmem_android_borrow_register_v1', 'mgmem_android_borrow_cancel_v1'] +
               [prefix + 'NativeMemoryLease_' + name for name in ('nativeClaim', 'nativeCopy', 'nativeClose')],
    }
    for label, path in [('fixture', fixture), ('jni', inputs / 'libmgmem_pressure_jni.so')]:
        symbols = run('readelf-exports-' + label, [readelf, '--dyn-syms', '--wide', path])
        for name in expected_exports[label]:
            if not re.search(r'(?m)^\s*\d+:\s+\S+\s+\d+\s+FUNC\s+GLOBAL\s+DEFAULT\s+\d+\s+' + re.escape(name) + r'\s*$', symbols):
                raise RuntimeError('Missing defined exported function: ' + name)

    instrument = fixture_source / 'NativeBorrowInstrumentation.java'
    witness = fixture_source / 'BorrowShrinkWitness.java'
    manifest = fixture_source / 'AndroidManifest.xml'
    classes = out / 'classes'; classes.mkdir()
    classpath = str(android) + os.pathsep + str(inputs / 'classes.jar')
    run('javac-test', [javac, '--release', '8', '-classpath', classpath, '-d', classes, instrument] + ([witness] if args.r8 else []))
    test_jar = out / 'test.jar'
    run('jar-test', [jar, '--create', '--file', test_jar, '-C', classes, '.'])
    dex = out / 'dex'; dex.mkdir()
    r8_evidence = None
    if args.r8:
        witness_name = 'io.github.jichuo1.meguviz.memory.android.BorrowShrinkWitness'
        instrument_name = 'io.github.jichuo1.meguviz.memory.android.NativeBorrowInstrumentation'
        lease_name = 'io.github.jichuo1.meguviz.memory.android.NativeMemoryLease'
        with zipfile.ZipFile(test_jar) as archive:
            if archive.namelist().count(witness_name.replace('.', '/') + '.class') != 1:
                raise RuntimeError('Unreferenced shrinking witness missing from R8 input')
        config = out / 'instrumentation-r8.pro'
        usage = out / 'r8-usage.txt'
        config.write_text('-keep class ' + instrument_name + ' { *; }\n'
                          '-printusage ' + usage.resolve().as_posix() + '\n', encoding='utf-8', newline='\n')
        version = run('r8-version', [java, '-cp', d8, 'com.android.tools.r8.R8', '--version']).strip()
        mapping = out / 'r8-mapping.txt'
        run('r8', [java, '-cp', d8, 'com.android.tools.r8.R8', '--release', '--min-api', '27', '--lib', android,
                   '--pg-conf', inputs / 'proguard.txt', '--pg-conf', config, '--pg-map-output', mapping,
                   '--output', dex, inputs / 'classes.jar', test_jar])
        if sorted(p.name for p in dex.glob('*.dex')) != ['classes.dex']:
            raise RuntimeError('Expected exactly one R8 DEX')
        usage_text = usage.read_text(encoding='utf-8')
        if not re.search(r'(?m)^' + re.escape(witness_name) + r'(?::.*)?$', usage_text):
            raise RuntimeError('R8 shrinking witness was not removed')
        mapping_text = mapping.read_text(encoding='utf-8')
        if not re.search(r'(?m)^' + re.escape(lease_name) + r' -> ' + re.escape(lease_name) + r':$', mapping_text):
            raise RuntimeError('NativeMemoryLease class name was not kept')
        dumped = run('dexdump-r8', [dexdump, '-f', dex / 'classes.dex'])
        blocks = re.split(r'(?m)^Class #\d+\s+-\s*$', dumped)
        def class_block(name):
            descriptor = 'L' + name.replace('.', '/') + ';'
            found = [block for block in blocks if "Class descriptor  : '" + descriptor + "'" in block]
            if len(found) != 1:
                raise RuntimeError('Expected exactly one retained DEX class: ' + name)
            return found[0]
        if "Class descriptor  : 'L" + witness_name.replace('.', '/') + ";'" in dumped:
            raise RuntimeError('Shrinking witness remains in DEX')
        for owner, members in ((lease_name, {'nativeClaim':'(J)J','nativeCopy':'(JJ[BIIZ)I','nativeClose':'(J)I'}),
                               (instrument_name, {'nativeCreate':'(Z)J','nativeBeginClose':'()Z','nativeSnapshot':'()[J',
                                                  'nativeCollectBusy':'()Z','nativeFinish':'()Z','nativeCleanupBestEffort':'()V'})):
            block = class_block(owner)
            for name, signature in members.items():
                method = (r"(?ms)^\s*#\d+\s+:\s+\(in\s+L" + re.escape(owner.replace('.', '/')) +
                          r";\)\s*\n\s*name\s*:\s*'" + re.escape(name) +
                          r"'\s*\n\s*type\s*:\s*'" + re.escape(signature) +
                          r"'\s*\n\s*access\s*:\s*[^\n]*\bNATIVE\b")
                if not re.search(method, block):
                    raise RuntimeError('Required DEX native name/signature missing: ' + owner + '.' + name)
        # Independent negative control: identical inputs and instrumentation
        # root, deliberately omit AAR rules. No production artifact is changed.
        negative = out / 'r8-no-aar-rules'; negative.mkdir()
        negative_conf = negative / 'instrumentation-only.pro'
        negative_conf.write_text('-keep class ' + instrument_name + ' { *; }\n', encoding='utf-8', newline='\n')
        negative_mapping = negative / 'mapping.txt'
        negative_dex = negative / 'dex'; negative_dex.mkdir()
        run('r8-negative-without-aar-rules', [java, '-cp', d8, 'com.android.tools.r8.R8', '--release',
            '--min-api', '27', '--lib', android, '--pg-conf', negative_conf, '--pg-map-output', negative_mapping,
            '--output', negative_dex, inputs / 'classes.jar', test_jar])
        negative_text = negative_mapping.read_text(encoding='utf-8')
        negative_class = re.search(r'(?m)^' + re.escape(lease_name) + r' -> ([^:]+):$', negative_text)
        negative_name = negative_class.group(1) if negative_class else None
        r8_evidence = {'r8_version': version, 'aar_proguard_sha256': sha(rules_bytes),
            'diagnostic_rule_scope': 'INSTRUMENTATION_CLASS_ONLY_NO_NATIVE_MEMORY_LEASE_KEEP',
            'unused_witness_removed': witness_name, 'lease_class_name_and_native_signatures_retained_in_dex': True,
            'instrumentation_class_name_and_native_signatures_retained_in_dex': True,
            'mapping_sha256': sha(mapping.read_bytes()), 'usage_sha256': sha(usage.read_bytes()),
            'dex_sha256': sha((dex / 'classes.dex').read_bytes()),
            'without_aar_rules_control': {'lease_mapped_name': negative_name,
                'lease_name_preserved': negative_name == lease_name,
                'mapping_sha256': sha(negative_mapping.read_bytes())},
            'device_execution': 'NOT_RUN'}
    else:
        run('d8', [java, '-cp', d8, 'com.android.tools.r8.D8', '--min-api', '27', '--lib', android,
                   '--output', dex, inputs / 'classes.jar', test_jar])
    unsigned = out / 'borrow-unsigned.apk'
    run('aapt2-link', [aapt, 'link', '-I', android, '--manifest', manifest, '--min-sdk-version', '27',
                      '--target-sdk-version', '33', '-o', unsigned])
    with zipfile.ZipFile(unsigned, 'a', compression=zipfile.ZIP_STORED) as package:
        package.write(dex / 'classes.dex', 'classes.dex')
        for name, path in [('libmgmem.so', inputs / 'libmgmem.so'),
                           ('libmgmem_pressure_jni.so', inputs / 'libmgmem_pressure_jni.so'),
                           ('libmgmem_borrow_fixture.so', fixture)]:
            package.write(path, 'lib/' + args.abi + '/' + name)
    aligned = out / 'borrow-aligned.apk'
    run('zipalign', [zipalign, '-P', '16', '-f', '4', unsigned, aligned])
    key = out / 'diagnostic.keystore'
    run('diagnostic-key', [keytool, '-genkeypair', '-keystore', key, '-storepass', 'android', '-keypass', 'android',
        '-alias', 'borrowprobe', '-keyalg', 'RSA', '-keysize', '2048', '-validity', '3650',
        '-dname', 'CN=MeguvizMemory Borrow Probe,O=MeguvizMemory', '-noprompt'])
    apk = out / 'MeguvizMemory-Borrow-Probe.apk'
    run('sign', [java, '-jar', signer, 'sign', '--ks', key, '--ks-pass', 'pass:android', '--key-pass', 'pass:android',
                 '--ks-key-alias', 'borrowprobe', '--out', apk, aligned])
    signature = run('verify-signature', [java, '-jar', signer, 'verify', '--print-certs', apk])
    certs = re.findall(r'certificate SHA-256 digest:\s*([0-9a-fA-F]{64})', signature)
    if len(certs) != 1:
        raise RuntimeError('Expected exactly one diagnostic signing certificate')
    with zipfile.ZipFile(apk) as package:
        if len(package.namelist()) != len(set(package.namelist())):
            raise RuntimeError('APK has duplicate entries')
        for name, expected_bytes in [('libmgmem.so', core_bytes), ('libmgmem_pressure_jni.so', jni_bytes),
                                     ('libmgmem_borrow_fixture.so', fixture.read_bytes())]:
            if package.read('lib/' + args.abi + '/' + name) != expected_bytes:
                raise RuntimeError('APK native member mismatch: ' + name)

    sources = [manifest, instrument, fixture_source / 'borrow_fixture.cpp', fixture_source / 'CMakeLists.txt',
               Path(__file__).resolve(), args.memory_sdk / 'include/mgmem/android_borrow_jni.h']
    if args.r8:
        sources.append(witness)
    receipt = {'schema_version': 1, 'state': 'PASS_R8_BUILD_ONLY_NOT_DEVICE_EXECUTION' if args.r8 else 'PASS_BUILD_ONLY_NOT_DEVICE_EXECUTION',
               'scope': 'ISOLATED_DIAGNOSTIC_APK_NOT_PRODUCTION_AAR', 'device_executed': False,
               'abi': args.abi, 'minimum_api': 27, 'package_name': 'io.github.jichuo1.meguviz.memory.borrowprobe',
               'instrumentation': 'io.github.jichuo1.meguviz.memory.android.NativeBorrowInstrumentation',
               'apk_sha256': sha(apk.read_bytes()), 'apk_bytes': apk.stat().st_size,
               'certificate_sha256': certs[0].lower(), 'signing': 'PRIVATE_DIAGNOSTIC_ONLY',
               'inputs': {'aar_sha256': sha(args.aar.read_bytes()), 'classes_jar_sha256': sha(classes_bytes),
                          'shared_core_sha256': sha(core_bytes), 'shared_jni_sha256': sha(jni_bytes)},
               'fixture_sha256': sha(fixture.read_bytes()), 'fixture_needed': sorted(needed), 'verified_exports': expected_exports,
               'source_sha256': {p.name: sha(p.read_bytes()) for p in sources}, 'operations': operations,
               'r8_evidence': r8_evidence,
               'limitations': ['Input AAR source provenance must be verified separately.',
                   'Build is not Android/ART/device, production-consumer, performance or release qualification.']}
    if not args.r8:
        receipt['limitations'].append('D8 mode does not test R8 shrinking or consumer rules.')
    (out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({'state': receipt['state'], 'apk': str(apk.relative_to(ROOT)), 'sha256': receipt['apk_sha256']}))


if __name__ == '__main__':
    main()
