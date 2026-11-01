#!/usr/bin/env python3
"""Build a private R8 stability diagnostic APK; build-only, never invokes ADB."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
import zipfile

import build_android_candidate as common
import build_android_managed_probe as base

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'io.github.jichuo1.meguviz.memory.stabilityprobe'
JAVA = 'io.github.jichuo1.meguviz.memory.android.'
INSTRUMENT = JAVA + 'ManagedStabilityInstrumentation'
ACTIVITY = JAVA + 'PressureR8ForegroundActivity'
WITNESS = base.WITNESS
SOURCES = (
    'tests/android_managed_stability/AndroidManifest.xml',
    'tests/android_managed_stability/ManagedStabilityInstrumentation.java',
    'tests/android_managed_stability/StabilityEngine.java',
    'tests/android_managed_stability/StabilityTiming.java',
    'adapters/android/sample/PressureR8ForegroundActivity.java',
    'tests/android_managed/ManagedShrinkWitness.java',
    'tools/build_android_managed_stability_probe.py',
    'tools/build_android_managed_probe.py', 'tools/build_android_candidate.py',
    'tools/android_managed_round.py', 'tools/package_feature_preview.py', 'tools/source_lock.py',
)


def native_declarations(output):
    found = base.native_descriptors(output)
    all_native = re.findall(r'(?m)^\s*[^\n]*\bnative\b[^\n]*\);\s*$', output)
    common.require(len(all_native) == len(found), 'unrecognized_jni_declaration')
    return found


def verify_shrink_usage(usage):
    text = usage.replace('\r\n', '\n')
    common.require(re.search(r'(?m)^' + re.escape(WITNESS) + r'(?::.*)?$', text) is not None, 'shrink_witness_not_removed')


def verify_manifest(tree, badging, abi):
    common.require(re.findall(r"(?m)^package: name='([^']+)'", badging) == [PACKAGE] and
                   re.findall(r"(?m)^native-code: '([^']+)'\s*$", badging) == [abi] and
                   re.findall(r"(?m)^minSdkVersion:'([^']+)'\s*$", badging) == ['27'] and
                   re.findall(r"(?m)^targetSdkVersion:'([^']+)'\s*$", badging) == ['33'], 'apk_badging_mismatch')
    lines = tree.splitlines()
    nodes = [(i, m.group(1)) for i, line in enumerate(lines)
             if (m := re.match(r'^\s*E: ([A-Za-z0-9_-]+) \(', line))]
    common.require([kind for _, kind in nodes] == ['manifest', 'uses-sdk', 'application', 'activity', 'instrumentation'],
                   'apk_manifest_elements_mismatch')
    indent = lambda index: len(lines[index]) - len(lines[index].lstrip())
    common.require(indent(nodes[0][0]) < indent(nodes[1][0]) == indent(nodes[2][0]) == indent(nodes[4][0]) and
                   indent(nodes[3][0]) > indent(nodes[2][0]) and
                   re.findall(r'(?m)^\s*A: package="([^\"]+)"', tree) == [PACKAGE], 'apk_manifest_hierarchy_mismatch')

    def attrs(index):
        indent = len(lines[index]) - len(lines[index].lstrip()); result = {}
        for line in lines[index + 1:]:
            level = len(line) - len(line.lstrip())
            if line.strip() and level <= indent: break
            if level != indent + 2: continue
            match = re.match(r'^\s*A: http://schemas\.android\.com/apk/res/android:([A-Za-z0-9_]+)\(0x[0-9a-f]+\)=(.+)$', line)
            if match:
                common.require(match.group(1) not in result, 'apk_manifest_duplicate_attribute')
                result[match.group(1)] = match.group(2)
        return result

    manifest, sdk, app, activity, instr = (attrs(nodes[i][0]) for i in (0, 1, 2, 3, 4))
    text = lambda actual, expected: re.match(r'^"' + re.escape(expected) + r'"(?:\s|$)', actual or '') is not None
    common.require(set(manifest) <= {'compileSdkVersion', 'compileSdkVersionCodename'} and
                   sdk == {'minSdkVersion': '27', 'targetSdkVersion': '33'} and
                   set(app) == {'label', 'debuggable', 'allowBackup'} and
                   app['debuggable'] == 'true' and app['allowBackup'] == 'false' and
                   set(activity) == {'name', 'exported', 'permission'} and
                   text(activity['name'], ACTIVITY) and activity['exported'] == 'true' and
                   text(activity['permission'], 'android.permission.DUMP') and
                   set(instr) == {'name', 'targetPackage', 'functionalTest'} and
                   text(instr['name'], INSTRUMENT) and text(instr['targetPackage'], PACKAGE) and
                   instr['functionalTest'] == 'true', 'apk_manifest_contract_mismatch')


def tool_paths(args):
    suffix = '.exe' if os.name == 'nt' else ''
    sdktools = args.android_sdk / 'build-tools' / args.build_tools
    jdk = args.java_home / 'bin'
    llvm = args.ndk / 'toolchains/llvm/prebuilt' / ('windows-x86_64' if suffix else 'linux-x86_64') / 'bin'
    return {'java': jdk / ('java' + suffix), 'javac': jdk / ('javac' + suffix),
            'javap': jdk / ('javap' + suffix), 'jar': jdk / ('jar' + suffix),
            'keytool': jdk / ('keytool' + suffix), 'aapt2': sdktools / ('aapt2' + suffix),
            'zipalign': sdktools / ('zipalign' + suffix), 'dexdump': sdktools / ('dexdump' + suffix),
            'r8': sdktools / 'lib/d8.jar', 'apksigner': sdktools / 'lib/apksigner.jar',
            'android_jar': args.android_sdk / 'platforms' / args.platform / 'android.jar',
            'readelf': llvm / ('llvm-readelf' + suffix)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidate-zip', 'candidate-evidence', 'android-sdk', 'java-home', 'ndk', 'out-dir'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--abi', required=True, choices=('arm64-v8a', 'x86_64'))
    parser.add_argument('--build-tools', default='37.0.0'); parser.add_argument('--platform', default='android-37.0')
    args = parser.parse_args()
    common.require(re.fullmatch(r'\d+(?:\.\d+)*', args.build_tools) and
                   re.fullmatch(r'android-\d+(?:\.\d+)?', args.platform), 'sdk_version_invalid')
    out = common.fresh_work_directory(args.out_dir, common.workspace_directory('build'))
    candidate = args.candidate_zip.resolve(strict=True); evidence = args.candidate_evidence.resolve(strict=True)
    common.require(candidate.is_file() and evidence.is_file() and candidate.is_relative_to(ROOT) and
                   evidence.is_relative_to(ROOT), 'candidate_inputs_outside_workspace')
    tools = tool_paths(args)
    common.require(all(path.is_file() for path in tools.values()), 'required_tool_missing')
    sources = {name: common.safe_file(ROOT, name) for name in SOURCES}
    source_ids = {name: common.file_identity(path) for name, path in sources.items()}
    tool_ids = {name: common.file_identity(path) for name, path in tools.items()}
    record, candidate_bytes, evidence_bytes, aar, sdk, classes, rules, core, jni = base.candidate_inputs(candidate, evidence, args.abi)
    common.require(record['android']['profiles'][args.abi]['jni_native_declarations'] == 37, 'candidate_jni_count_mismatch')
    out.mkdir(parents=True, exist_ok=False)
    status = out / 'STATUS.json'; status.write_bytes(common.json_bytes({'state': 'RUNNING', 'device_executed': False}))
    operations = []
    try:
        inputs = out / 'inputs'; inputs.mkdir()
        for name, data in (('MeguvizMemory.aar', aar), ('MeguvizMemory-sdk.zip', sdk), ('classes.jar', classes),
                           ('proguard.txt', rules), ('libmgmem.so', core), ('libmgmem_pressure_jni.so', jni)):
            with (inputs / name).open('xb') as stream: stream.write(data)
        run = lambda command, label: common.run_tool(command, out, operations, label)
        expected = {}
        for klass in base.NATIVE_CLASSES:
            owner = base.JAVA_PACKAGE + klass
            expected[owner] = native_declarations(run([tools['javap'], '-private', '-s', '-classpath', inputs / 'classes.jar', owner], 'javap-' + klass))
        exports = {common.JAVA_PREFIX + klass + '_' + method for klass in base.NATIVE_CLASSES
                   for method in expected[base.JAVA_PACKAGE + klass]}
        common.require(len(exports) == 37, 'jni_count_invalid')
        common.validate_jni_symbols(run([tools['readelf'], '--dyn-syms', '--wide', inputs / 'libmgmem_pressure_jni.so'], 'jni-symbols'), exports)
        compiled = out / 'classes'; compiled.mkdir()
        run([tools['javac'], '--release', '8', '-classpath', str(tools['android_jar']) + os.pathsep + str(inputs / 'classes.jar'),
             '-d', compiled, *(sources[name] for name in SOURCES[1:6])], 'javac-probe')
        jar = out / 'probe.jar'; run([tools['jar'], '--create', '--file', jar, '-C', compiled, '.'], 'jar-probe')
        with zipfile.ZipFile(jar) as archive: base.read_unique(archive, WITNESS.replace('.', '/') + '.class')
        dex = out / 'dex'; dex.mkdir(); usage = out / 'r8-usage.txt'; mapping = out / 'r8-mapping.txt'; conf = out / 'probe-r8.pro'
        conf.write_text('-keep class ' + INSTRUMENT + ' { *; }\n-keep class ' + ACTIVITY + ' { *; }\n-printusage ' + usage.resolve().as_posix() + '\n', encoding='utf-8', newline='\n')
        run([tools['java'], '-cp', tools['r8'], 'com.android.tools.r8.R8', '--release', '--min-api', '27', '--lib', tools['android_jar'],
             '--pg-conf', inputs / 'proguard.txt', '--pg-conf', conf, '--pg-map-output', mapping, '--output', dex, inputs / 'classes.jar', jar], 'r8')
        dex_file = dex / 'classes.dex'; common.require(sorted(p.name for p in dex.glob('*.dex')) == ['classes.dex'], 'dex_output_invalid')
        verify_shrink_usage(base.bounded_file(usage, base.MAX_MEMBER).decode('utf-8'))
        dumped = run([tools['dexdump'], '-f', dex_file], 'dexdump'); base.validate_dex(dumped, expected)
        for owner in (INSTRUMENT, ACTIVITY):
            common.require(dumped.count("Class descriptor  : 'L" + owner.replace('.', '/') + ";'") == 1, 'probe_class_missing')
        unsigned = out / 'unsigned.apk'
        run([tools['aapt2'], 'link', '-I', tools['android_jar'], '--manifest', sources[SOURCES[0]], '--min-sdk-version', '27', '--target-sdk-version', '33', '-o', unsigned], 'aapt-link')
        with zipfile.ZipFile(unsigned, 'a', compression=zipfile.ZIP_STORED) as package:
            package.write(dex_file, 'classes.dex')
            package.writestr('lib/' + args.abi + '/libmgmem.so', core)
            package.writestr('lib/' + args.abi + '/libmgmem_pressure_jni.so', jni)
        aligned = out / 'aligned.apk'; run([tools['zipalign'], '-P', '16', '-f', '4', unsigned, aligned], 'zipalign')
        key = out / 'diagnostic.keystore'
        run([tools['keytool'], '-genkeypair', '-keystore', key, '-storepass', 'android', '-keypass', 'android', '-alias', 'stabilityprobe',
             '-keyalg', 'RSA', '-keysize', '2048', '-validity', '3650', '-dname', 'CN=MeguvizMemory Stability Probe,O=MeguvizMemory', '-noprompt'], 'diagnostic-key')
        apk = out / 'Managed-Stability-Probe.apk'
        run([tools['java'], '-jar', tools['apksigner'], 'sign', '--ks', key, '--ks-pass', 'pass:android', '--key-pass', 'pass:android',
             '--ks-key-alias', 'stabilityprobe', '--out', apk, aligned], 'sign')
        signature = run([tools['java'], '-jar', tools['apksigner'], 'verify', '--print-certs', apk], 'verify-signature')
        certs = re.findall(r'certificate SHA-256 digest:\s*([0-9a-fA-F]{64})', signature)
        common.require(len(certs) == 1, 'certificate_identity_invalid')
        run([tools['zipalign'], '-c', '-P', '16', '-v', '4', apk], 'verify-alignment')
        badging = run([tools['aapt2'], 'dump', 'badging', apk], 'verify-badging')
        tree = run([tools['aapt2'], 'dump', 'xmltree', apk, '--file', 'AndroidManifest.xml'], 'verify-manifest')
        verify_manifest(tree, badging, args.abi)
        with zipfile.ZipFile(apk) as package:
            names = package.namelist(); natives = [name for name in names if name.startswith('lib/') and name.endswith('.so')]
            common.require(len(names) == len(set(names)) and set(natives) == {
                'lib/' + args.abi + '/libmgmem.so', 'lib/' + args.abi + '/libmgmem_pressure_jni.so'}, 'apk_native_closure_invalid')
            common.require(base.read_unique(package, 'classes.dex') == dex_file.read_bytes() and
                base.read_unique(package, 'lib/' + args.abi + '/libmgmem.so') == core and
                base.read_unique(package, 'lib/' + args.abi + '/libmgmem_pressure_jni.so') == jni, 'apk_member_mismatch')
        common.require(base.bounded_file(candidate, base.MAX_CANDIDATE) == candidate_bytes and
                       base.bounded_file(evidence, base.MAX_EVIDENCE) == evidence_bytes and
                       all(common.file_identity(path) == tool_ids[name] for name, path in tools.items()) and
                       all(common.file_identity(path) == source_ids[name] for name, path in sources.items()), 'build_inputs_changed')
        common.verify_tool_log_integrity(out, operations)
        receipt = {'schema_version': 1, 'state': 'PASS_R8_BUILD_ONLY_NOT_DEVICE_EXECUTION',
                   'scope': 'ISOLATED_STABILITY_DIAGNOSTIC_NOT_PRODUCTION_APK', 'device_executed': False,
                   'stability_qualified': False, 'performance_qualified': False, 'abi': args.abi,
                   'package_name': PACKAGE, 'instrumentation': INSTRUMENT, 'activity': ACTIVITY,
                   'apk_sha256': base.digest(apk.read_bytes()), 'apk_bytes': apk.stat().st_size,
                   'certificate_sha256': certs[0].lower(), 'candidate_sha256': base.digest(candidate_bytes),
                   'candidate_source_commit': record['source_commit'], 'signing': 'PRIVATE_DIAGNOSTIC_ONLY',
                   'inputs': {'aar_sha256': base.digest(aar), 'sdk_sha256': base.digest(sdk), 'shared_core_sha256': base.digest(core),
                              'shared_jni_sha256': base.digest(jni), 'classes_jar_sha256': base.digest(classes)},
                   'source_identities': source_ids, 'tools': tool_ids,
                   'r8_evidence': {'dex_sha256': base.digest(dex_file.read_bytes()), 'native_method_count': len(exports),
                                   'managed_native_signatures_retained': True, 'shrink_witness_removed': True},
                   'apk_manifest_readback': 'PASS_PACKAGE_ACTIVITY_DUMP_INSTRUMENTATION_TARGET_AND_SDK', 'operations': operations,
                   'limits': ['No device, ART, real system pressure, stability or performance qualification.',
                              'Hashes are not signed source provenance.', 'Private key and raw logs remain in ignored build directory.']}
        with (out / 'receipt.json').open('xb') as stream: stream.write(common.json_bytes(receipt))
        status.write_bytes(common.json_bytes({'state': receipt['state'], 'device_executed': False}))
        print(common.json_bytes({'state': receipt['state'], 'abi': args.abi, 'apk_sha256': receipt['apk_sha256']}).decode('utf-8'), end='')
        return 0
    except BaseException:
        status.write_bytes(common.json_bytes({'state': 'FAILED', 'device_executed': False}))
        raise


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        sys.stderr.write('{"state":"FAILED","device_executed":false}\n')
        raise SystemExit(1)
