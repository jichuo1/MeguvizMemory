#!/usr/bin/env python3
"""Build an isolated R8 native-pressure diagnostic APK; never invokes ADB.

The APK and its fresh diagnostic key stay in build/. Its input libraries are
read from an identified Android engineering candidate, not rebuilt here.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from pathlib import Path
import zipfile

import build_android_candidate as common
import android_managed_round as managed_round
import build_android_managed_probe as base

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'io.github.jichuo1.meguviz.memory.nativepressureprobe'
INSTRUMENT = 'io.github.jichuo1.meguviz.memory.android.NativePressureCloseInstrumentation'
WITNESS = 'io.github.jichuo1.meguviz.memory.android.ManagedShrinkWitness'
JAVA_PACKAGE = 'io.github.jichuo1.meguviz.memory.android.'
NATIVE_CLASSES = ('NativeMemoryLease', 'NativePressureBackend', 'ManagedMemoryDomain')
MAX_MEMBER = 64 * 1024 * 1024
MAX_CANDIDATE = 256 * 1024 * 1024
MAX_EVIDENCE = 1024 * 1024


digest = base.digest
bounded_file = base.bounded_file
read_unique = base.read_unique
candidate_inputs = base.candidate_inputs
validate_dex = base.validate_dex


def native_descriptors(output):
    """Reject native declarations the known private-static parser cannot cover."""
    found = base.native_descriptors(output)
    declarations = re.findall(r'(?m)^\s*[^\n]*\bnative\b[^\n]*\);\s*$', output)
    common.require(len(declarations) == len(found), 'unrecognized_jni_declaration')
    return found


def verify_shrink_usage(data):
    """Require the diagnostic witness to have actually been removed by R8."""
    text = data.decode('utf-8').replace('\r\n', '\n') if isinstance(data, bytes) else data.replace('\r\n', '\n')
    common.require(re.search(r'(?m)^' + re.escape(WITNESS) + r'(?::.*)?$', text) is not None,
                   'shrink_witness_not_removed')


def verify_manifest_tree(tree):
    """Require only the dedicated package and its one instrumentation."""
    lines = tree.splitlines()
    nodes = [(i, match.group(1)) for i, line in enumerate(lines)
             if (match := re.match(r'^\s*E: ([A-Za-z0-9_-]+) \(', line))]
    common.require([kind for _, kind in nodes] == ['manifest', 'uses-sdk', 'application', 'instrumentation'],
                   'apk_manifest_elements_mismatch')
    indent = lambda index: len(lines[index]) - len(lines[index].lstrip())
    common.require(indent(nodes[0][0]) < indent(nodes[1][0]) == indent(nodes[2][0]) == indent(nodes[3][0]) and
                   re.findall(r'(?m)^\s*A: package="([^\"]+)"', tree) == [PACKAGE],
                   'apk_manifest_hierarchy_or_package_mismatch')
    def attrs(index):
        level = indent(index)
        result = {}
        for line in lines[index + 1:]:
            current = len(line) - len(line.lstrip())
            if line.strip() and current <= level:
                break
            if current != level + 2:
                continue
            match = re.match(r'^\s*A: http://schemas\.android\.com/apk/res/android:([A-Za-z0-9_]+)\(0x[0-9a-f]+\)=(.+)$', line)
            if match:
                common.require(match.group(1) not in result, 'apk_manifest_duplicate_attribute')
                result[match.group(1)] = match.group(2)
        return result
    manifest, sdk, application, instrument = (attrs(nodes[i][0]) for i in range(4))
    text = lambda actual, expected: re.match(r'^"' + re.escape(expected) + r'"(?:\s|$)', actual or '') is not None
    common.require(set(manifest) <= {'compileSdkVersion', 'compileSdkVersionCodename'} and
                   sdk == {'minSdkVersion': '27', 'targetSdkVersion': '33'} and
                   set(application) == {'label', 'debuggable', 'allowBackup'} and
                   application['debuggable'] == 'true' and application['allowBackup'] == 'false' and
                   set(instrument) == {'name', 'targetPackage', 'functionalTest'} and
                   text(instrument['name'], INSTRUMENT) and text(instrument['targetPackage'], PACKAGE) and
                   instrument['functionalTest'] == 'true', 'apk_manifest_contract_mismatch')


def verify_callback(mapping, dumped):
    """Check the exact JNI GetMethodID callback in both R8 mapping and DEX."""
    owner = JAVA_PACKAGE + 'NativePressureBackend'
    header = owner + ' -> ' + owner + ':'
    lines = mapping.replace('\r\n', '\n').splitlines()
    matches = [i for i, line in enumerate(lines) if line == header]
    common.require(len(matches) == 1, 'callback_owner_mapping_missing')
    members = []
    for line in lines[matches[0] + 1:]:
        if line and not line[0].isspace() and not line.startswith('#'):
            break
        members.append(line)
    common.require(sum(re.search(r'\bvoid onNativeClosed\(int\)(?::[0-9]+)* -> onNativeClosed$', line) is not None
                       for line in members) == 1, 'callback_mapping_missing')
    descriptor = 'L' + owner.replace('.', '/') + ';'
    blocks = re.split(r'(?m)^Class #\d+\s+-\s*$', dumped)
    found = [block for block in blocks if "Class descriptor  : '" + descriptor + "'" in block]
    common.require(len(found) == 1, 'callback_dex_owner_missing')
    method = (r"(?ms)^\s*#\d+\s+:\s+\(in\s+" + re.escape(descriptor) +
              r"\)\s*\n\s*name\s*:\s*'onNativeClosed'\s*\n\s*type\s*:\s*'\(I\)V'"
              r"\s*\n\s*access\s*:\s*[^\n]*\bPRIVATE\b")
    common.require(len(re.findall(method, found[0])) == 1, 'callback_dex_signature_missing')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidate-zip', 'candidate-evidence', 'android-sdk', 'java-home', 'ndk', 'out-dir'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--abi', choices=('arm64-v8a', 'x86_64'), required=True)
    parser.add_argument('--build-tools', default='37.0.0')
    parser.add_argument('--platform', default='android-37.0')
    args = parser.parse_args()
    common.require(re.fullmatch(r'\d+(?:\.\d+)*', args.build_tools) and
                   re.fullmatch(r'android-\d+(?:\.\d+)?', args.platform), 'sdk_version_invalid')
    build = common.workspace_directory('build')
    out = common.fresh_work_directory(args.out_dir, build)
    candidate = args.candidate_zip.resolve(strict=True)
    evidence = args.candidate_evidence.resolve(strict=True)
    common.require(candidate.is_file() and evidence.is_file() and candidate.is_relative_to(ROOT) and
                   evidence.is_relative_to(ROOT), 'candidate_inputs_outside_workspace')
    suffix = '.exe' if __import__('os').name == 'nt' else ''
    sdktools = args.android_sdk / 'build-tools' / args.build_tools
    jdk = args.java_home / 'bin'
    host = 'windows-x86_64' if suffix else 'linux-x86_64'
    llvm = args.ndk / 'toolchains/llvm/prebuilt' / host / 'bin'
    tools = {'java': jdk / ('java' + suffix), 'javac': jdk / ('javac' + suffix),
             'javap': jdk / ('javap' + suffix), 'jar': jdk / ('jar' + suffix),
             'keytool': jdk / ('keytool' + suffix), 'aapt2': sdktools / ('aapt2' + suffix),
             'zipalign': sdktools / ('zipalign' + suffix), 'dexdump': sdktools / ('dexdump' + suffix),
             'r8': sdktools / 'lib/d8.jar', 'apksigner': sdktools / 'lib/apksigner.jar',
             'android_jar': args.android_sdk / 'platforms' / args.platform / 'android.jar',
             'readelf': llvm / ('llvm-readelf' + suffix)}
    common.require(all(p.is_file() for p in tools.values()), 'required_tool_missing')
    sources = [ROOT / 'tests/android_native_pressure_device/AndroidManifest.xml',
               ROOT / 'tests/android_native_pressure_device/NativePressureCloseInstrumentation.java',
               ROOT / 'tests/android_managed/ManagedShrinkWitness.java', Path(__file__).resolve(),
               ROOT / 'tools/android_managed_round.py', ROOT / 'tools/build_android_managed_probe.py',
               ROOT / 'tools/build_android_candidate.py',
               ROOT / 'tools/package_feature_preview.py', ROOT / 'tools/source_lock.py']
    common.require(all(p.is_file() and not p.is_symlink() for p in sources), 'probe_source_missing')
    common.require(len({path.name for path in sources}) == len(sources), 'duplicate_probe_source_name')
    tool_ids = {name: common.file_identity(path) for name, path in tools.items()}
    source_ids = {path.name: common.file_identity(path)['sha256'] for path in sources}
    record, candidate_bytes, evidence_bytes, aar, sdk, classes, rules, core, jni = candidate_inputs(candidate, evidence, args.abi)
    out.mkdir(parents=True, exist_ok=False)
    operations = []
    status = out / 'STATUS.json'
    status.write_bytes(common.json_bytes({'state': 'RUNNING', 'device_executed': False}))
    try:
        inputs = out / 'inputs'; inputs.mkdir()
        for name, data in (('MeguvizMemory.aar', aar), ('MeguvizMemory-sdk.zip', sdk), ('classes.jar', classes),
                           ('proguard.txt', rules), ('libmgmem.so', core), ('libmgmem_pressure_jni.so', jni)):
            with (inputs / name).open('xb') as stream: stream.write(data)
        native_expected = {}
        for klass in NATIVE_CLASSES:
            owner = JAVA_PACKAGE + klass
            output = common.run_tool([tools['javap'], '-private', '-s', '-classpath', inputs / 'classes.jar', owner],
                                     out, operations, 'javap-' + klass)
            native_expected[owner] = native_descriptors(output)
        symbols = common.run_tool([tools['readelf'], '--dyn-syms', '--wide', inputs / 'libmgmem_pressure_jni.so'],
                                  out, operations, 'jni-symbols')
        expected_exports = {common.JAVA_PREFIX + klass + '_' + method
                            for klass in NATIVE_CLASSES for method in native_expected[JAVA_PACKAGE + klass]}
        common.require(len(expected_exports) == 37, 'jni_native_count_mismatch')
        common.validate_jni_symbols(symbols, expected_exports)
        compiled = out / 'classes'; compiled.mkdir()
        common.run_tool([tools['javac'], '--release', '8', '-classpath',
                         str(tools['android_jar']) + __import__('os').pathsep + str(inputs / 'classes.jar'),
                         '-d', compiled, sources[1], sources[2]], out, operations, 'javac-probe')
        probe_jar = out / 'probe.jar'
        common.run_tool([tools['jar'], '--create', '--file', probe_jar, '-C', compiled, '.'], out, operations, 'jar-probe')
        with zipfile.ZipFile(probe_jar) as archive:
            read_unique(archive, WITNESS.replace('.', '/') + '.class')
        dex = out / 'dex'; dex.mkdir()
        config = out / 'probe-r8.pro'; usage = out / 'r8-usage.txt'; mapping = out / 'r8-mapping.txt'
        config.write_text('-keep class ' + INSTRUMENT + ' { *; }\n-printusage ' + usage.resolve().as_posix() + '\n',
                          encoding='utf-8', newline='\n')
        common.run_tool([tools['java'], '-cp', tools['r8'], 'com.android.tools.r8.R8', '--release', '--min-api', '27',
                         '--lib', tools['android_jar'], '--pg-conf', inputs / 'proguard.txt', '--pg-conf', config,
                         '--pg-map-output', mapping, '--output', dex, inputs / 'classes.jar', probe_jar], out, operations, 'r8')
        dex_file = dex / 'classes.dex'
        common.require(sorted(p.name for p in dex.glob('*.dex')) == ['classes.dex'], 'dex_output_invalid')
        usage_bytes = bounded_file(usage, MAX_MEMBER)
        mapping_bytes = bounded_file(mapping, MAX_MEMBER)
        verify_shrink_usage(usage_bytes)
        dumped = common.run_tool([tools['dexdump'], '-f', dex_file], out, operations, 'dexdump')
        validate_dex(dumped, native_expected)
        verify_callback(mapping_bytes.decode('utf-8'), dumped)
        common.require("Class descriptor  : 'L" + INSTRUMENT.replace('.', '/') + ";'" in dumped,
                       'instrumentation_not_retained')
        unsigned = out / 'unsigned.apk'
        common.run_tool([tools['aapt2'], 'link', '-I', tools['android_jar'], '--manifest', sources[0],
                         '--min-sdk-version', '27', '--target-sdk-version', '33', '-o', unsigned], out, operations, 'aapt-link')
        with zipfile.ZipFile(unsigned, 'a', compression=zipfile.ZIP_STORED) as package:
            package.write(dex_file, 'classes.dex')
            package.writestr('lib/' + args.abi + '/libmgmem.so', core)
            package.writestr('lib/' + args.abi + '/libmgmem_pressure_jni.so', jni)
        aligned = out / 'aligned.apk'
        common.run_tool([tools['zipalign'], '-P', '16', '-f', '4', unsigned, aligned], out, operations, 'zipalign')
        key = out / 'diagnostic.keystore'
        common.run_tool([tools['keytool'], '-genkeypair', '-keystore', key, '-storepass', 'android', '-keypass', 'android',
                         '-alias', 'nativepressureprobe', '-keyalg', 'RSA', '-keysize', '2048', '-validity', '3650',
                         '-dname', 'CN=MeguvizMemory Native Pressure Probe,O=MeguvizMemory', '-noprompt'], out, operations, 'diagnostic-key')
        apk = out / 'Native-Pressure-Probe.apk'
        common.run_tool([tools['java'], '-jar', tools['apksigner'], 'sign', '--ks', key, '--ks-pass', 'pass:android',
                         '--key-pass', 'pass:android', '--ks-key-alias', 'nativepressureprobe', '--out', apk, aligned],
                        out, operations, 'sign')
        signature = common.run_tool([tools['java'], '-jar', tools['apksigner'], 'verify', '--print-certs', apk],
                                    out, operations, 'verify-signature')
        certs = re.findall(r'certificate SHA-256 digest:\s*([0-9a-fA-F]{64})', signature)
        common.require(len(certs) == 1, 'certificate_identity_invalid')
        common.run_tool([tools['zipalign'], '-c', '-P', '16', '-v', '4', apk], out, operations, 'verify-alignment')
        badging = common.run_tool([tools['aapt2'], 'dump', 'badging', apk], out, operations, 'verify-badging')
        common.require(re.findall(r"(?m)^package: name='([^']+)'", badging) == [PACKAGE] and
                       re.findall(r"(?m)^native-code: '([^']+)'\s*$", badging) == [args.abi] and
                       re.findall(r"(?m)^minSdkVersion:'([^']+)'\s*$", badging) == ['27'] and
                       re.findall(r"(?m)^targetSdkVersion:'([^']+)'\s*$", badging) == ['33'],
                       'apk_package_or_abi_mismatch')
        tree = common.run_tool([tools['aapt2'], 'dump', 'xmltree', apk, '--file', 'AndroidManifest.xml'],
                               out, operations, 'verify-manifest')
        verify_manifest_tree(tree)
        with zipfile.ZipFile(apk) as package:
            names = package.namelist()
            native_names = [name for name in names if name.startswith('lib/') and name.endswith('.so')]
            common.require(len(names) == len(set(names)) and set(native_names) == {
                'lib/' + args.abi + '/libmgmem.so', 'lib/' + args.abi + '/libmgmem_pressure_jni.so'}, 'apk_native_closure_invalid')
            regular = {'AndroidManifest.xml', 'resources.arsc', 'classes.dex'} | set(native_names)
            signatures = [name for name in names if name.startswith('META-INF/')]
            common.require(set(names) == regular | set(signatures) and len(signatures) == 3 and
                           'META-INF/MANIFEST.MF' in signatures and
                           len([name for name in signatures if re.fullmatch(r'META-INF/[A-Z0-9_-]+\.SF', name)]) == 1 and
                           len([name for name in signatures if re.fullmatch(r'META-INF/[A-Z0-9_-]+\.(?:RSA|DSA|EC)', name)]) == 1,
                           'apk_unexpected_package_members')
            common.require(read_unique(package, native_names[0]) == (core if native_names[0].endswith('/libmgmem.so') else jni) and
                           read_unique(package, native_names[1]) == (core if native_names[1].endswith('/libmgmem.so') else jni) and
                           read_unique(package, 'classes.dex') == dex_file.read_bytes(), 'apk_member_mismatch')
        common.require(bounded_file(candidate, MAX_CANDIDATE) == candidate_bytes and
                       bounded_file(evidence, MAX_EVIDENCE) == evidence_bytes and
                       all(common.file_identity(path) == tool_ids[name] for name, path in tools.items()) and
                       all(common.file_identity(path)['sha256'] == source_ids[path.name] for path in sources), 'build_inputs_changed')
        common.verify_tool_log_integrity(out, operations)
        receipt = {'schema_version': 1, 'state': 'PASS_R8_BUILD_ONLY_NOT_DEVICE_EXECUTION',
                   'scope': 'ISOLATED_NATIVE_PRESSURE_DIAGNOSTIC_NOT_PRODUCTION_APK', 'device_executed': False,
                   'package_name': PACKAGE, 'instrumentation': INSTRUMENT, 'abi': args.abi,
                   'apk_sha256': digest(apk.read_bytes()), 'apk_bytes': apk.stat().st_size,
                   'certificate_sha256': certs[0].lower(), 'candidate_sha256': digest(candidate_bytes),
                   'candidate_source_commit': record['source_commit'], 'signing': 'PRIVATE_DIAGNOSTIC_ONLY',
                   'inputs': {'aar_sha256': digest(aar), 'shared_core_sha256': digest(core),
                              'shared_jni_sha256': digest(jni), 'classes_jar_sha256': digest(classes)},
                   'source_sha256': source_ids, 'tools': tool_ids,
                   'r8_evidence': {'dex_sha256': digest(dex_file.read_bytes()),
                                   'mapping_sha256': digest(mapping_bytes), 'usage_sha256': digest(usage_bytes),
                                   'instrumentation_retained': True,
                                   'managed_native_signatures_retained': True, 'native_method_count': len(expected_exports),
                                   'shrink_witness_removed': True, 'callback_retained': True,
                                   'device_execution': 'NOT_RUN'},
                   'apk_manifest_readback': 'PASS_PACKAGE_INSTRUMENTATION_TARGET_AND_SDK',
                   'operations': operations,
                   'limits': ['Diagnostic R8 build does not prove ART, physical-device, real pressure or performance behavior.',
                              'Source and file hashes are not signed build provenance.',
                              'The diagnostic key and raw logs remain in the ignored build directory.']}
        with (out / 'receipt.json').open('xb') as stream: stream.write(common.json_bytes(receipt))
        status.write_bytes(common.json_bytes({'state': receipt['state'], 'device_executed': False}))
        print(json.dumps({'state': receipt['state'], 'abi': args.abi, 'apk_sha256': receipt['apk_sha256']}))
        return 0
    except BaseException as error:
        reason = str(error)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', reason): reason = type(error).__name__
        status.write_bytes(common.json_bytes({'state': 'FAILED', 'reason': reason, 'device_executed': False}))
        raise


if __name__ == '__main__':
    raise SystemExit(main())
