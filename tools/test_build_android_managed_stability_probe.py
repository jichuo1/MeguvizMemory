#!/usr/bin/env python3
"""Offline negative tests for the private stability APK builder."""
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile
import warnings
import build_android_managed_stability_probe as probe


TREE = '''N: android=http://schemas.android.com/apk/res/android (line=1)
  E: manifest (line=1)
    A: package="io.github.jichuo1.meguviz.memory.stabilityprobe"
      E: uses-sdk (line=2)
        A: http://schemas.android.com/apk/res/android:minSdkVersion(0x0101020c)=27
        A: http://schemas.android.com/apk/res/android:targetSdkVersion(0x01010270)=33
      E: application (line=3)
        A: http://schemas.android.com/apk/res/android:label(0x01010001)="Probe"
        A: http://schemas.android.com/apk/res/android:debuggable(0x0101000f)=true
        A: http://schemas.android.com/apk/res/android:allowBackup(0x01010280)=false
        E: activity (line=4)
          A: http://schemas.android.com/apk/res/android:name(0x01010003)="io.github.jichuo1.meguviz.memory.android.PressureR8ForegroundActivity"
          A: http://schemas.android.com/apk/res/android:exported(0x01010010)=true
          A: http://schemas.android.com/apk/res/android:permission(0x01010006)="android.permission.DUMP"
      E: instrumentation (line=5)
        A: http://schemas.android.com/apk/res/android:name(0x01010003)="io.github.jichuo1.meguviz.memory.android.ManagedStabilityInstrumentation"
        A: http://schemas.android.com/apk/res/android:targetPackage(0x01010021)="io.github.jichuo1.meguviz.memory.stabilityprobe"
        A: http://schemas.android.com/apk/res/android:functionalTest(0x01010023)=true
'''
BADGING = "package: name='io.github.jichuo1.meguviz.memory.stabilityprobe'\nminSdkVersion:'27'\ntargetSdkVersion:'33'\nnative-code: 'arm64-v8a'\n"


class StabilityBuilderTests(unittest.TestCase):
    def test_manifest_valid(self):
        probe.verify_manifest(TREE, BADGING, 'arm64-v8a')

    def test_manifest_rejects_wrong_or_missing_permission(self):
        for replacement in ('android.permission.INTERNET', 'android.permission.FAKE'):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                probe.verify_manifest(TREE.replace('android.permission.DUMP', replacement), BADGING, 'arm64-v8a')
        line = '          A: http://schemas.android.com/apk/res/android:permission(0x01010006)="android.permission.DUMP"\n'
        with self.assertRaises(ValueError): probe.verify_manifest(TREE.replace(line, ''), BADGING, 'arm64-v8a')

    def test_manifest_rejects_sdk_target_and_component_changes(self):
        for bad in (TREE.replace('minSdkVersion(0x0101020c)=27', 'minSdkVersion(0x0101020c)=1'),
                    TREE.replace('targetSdkVersion(0x01010270)=33', 'targetSdkVersion(0x01010270)=34'),
                    TREE.replace('android:targetPackage(0x01010021)="io.github.jichuo1.meguviz.memory.stabilityprobe"',
                                 'android:targetPackage(0x01010021)="other"'),
                    TREE.replace('android:exported(0x01010010)=true', 'android:exported(0x01010010)=false'),
                    TREE + '      E: receiver (line=6)\n'):
            with self.subTest(bad=bad), self.assertRaises(ValueError): probe.verify_manifest(bad, BADGING, 'arm64-v8a')

    def test_badging_rejects_wrong_abi_package_sdk_and_duplicates(self):
        for bad in (BADGING.replace('arm64-v8a', 'x86_64'), BADGING.replace('stabilityprobe', 'wrong'),
                    BADGING.replace("targetSdkVersion:'33'", "targetSdkVersion:'34'"),
                    BADGING + "minSdkVersion:'27'\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError): probe.verify_manifest(TREE, bad, 'arm64-v8a')

    def test_manifest_rejects_wrong_root_and_activity_hierarchy(self):
        with self.assertRaises(ValueError):
            probe.verify_manifest(TREE.replace('A: package="io.github.jichuo1.meguviz.memory.stabilityprobe"', 'A: package="wrong"'), BADGING, 'arm64-v8a')
        with self.assertRaises(ValueError):
            probe.verify_manifest(TREE.replace('        E: activity', '      E: activity'), BADGING, 'arm64-v8a')

    def test_existing_work_directory_is_rejected(self):
        existing = Path(__file__).resolve()
        with self.assertRaises(ValueError): probe.common.fresh_work_directory(existing, existing.parent)

    def test_source_snapshot_mismatch_rejected(self):
        aar, sdk = b'fake-aar', b'fake-sdk'
        source = {'source_lock_sha256': 'original', 'local_source_set_sha256': 'set', 'local_source_count': 1}
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as z:
            z.writestr('android/arm64-v8a/MeguvizMemory.aar', aar)
            z.writestr('android/arm64-v8a/MeguvizMemory-sdk.zip', sdk)
            z.writestr('SOURCE.json', json.dumps(source))
        candidate = output.getvalue()
        record = {'state': 'PASS_SCOPED_ENGINEERING_CANDIDATES', 'source_commit': '1' * 40,
                  'source_lock_sha256': 'different', 'source_set_sha256': 'set', 'source_record_count': 1,
                  'android': {'sha256': probe.base.digest(candidate), 'profiles': {'arm64-v8a': {
                      'aar_sha256': probe.base.digest(aar), 'sdk_sha256': probe.base.digest(sdk)}}}}
        fake = {Path('candidate'): candidate, Path('evidence'): json.dumps(record).encode()}
        with patch.object(probe.base, 'bounded_file', side_effect=lambda path, limit: fake[path]):
            with self.assertRaisesRegex(ValueError, 'candidate_source_identity_mismatch'):
                probe.base.candidate_inputs(Path('candidate'), Path('evidence'), 'arm64-v8a')

    def test_jni_parser_rejects_unrecognized_or_overloaded(self):
        good = '  private static native long nativeCreate(boolean);\n    descriptor: (Z)J\n'
        self.assertEqual(probe.native_declarations(good), {'nativeCreate': '(Z)J'})
        for bad in (good + '  public native void changed();\n    descriptor: ()V\n', good + good):
            with self.assertRaises(ValueError): probe.native_declarations(bad)

    def test_archive_duplicate_members_rejected(self):
        buf = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(buf, 'w') as z:
                z.writestr('x', b'a'); z.writestr('x', b'b')
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z, self.assertRaises(ValueError):
            probe.base.read_unique(z, 'x')

    def test_dex_requires_exact_signature_and_witness_absence(self):
        owner = 'sample.C'; desc = 'Lsample/C;'
        dump = "Class #0            -\n  Class descriptor  : 'Lsample/C;'\n  #0 : (in Lsample/C;)\n    name : 'nativeX'\n    type : '(I)J'\n    access : 0x010a (PRIVATE STATIC NATIVE)\n"
        probe.base.validate_dex(dump, {owner: {'nativeX': '(I)J'}})
        with self.assertRaises(ValueError): probe.base.validate_dex(dump, {owner: {'nativeX': '(J)J'}})
        with self.assertRaises(ValueError):
            probe.base.validate_dex(dump + "Class #1            -\n  Class descriptor  : 'Lio/github/jichuo1/meguviz/memory/android/ManagedShrinkWitness;'\n", {owner: {'nativeX': '(I)J'}})

    def test_shrink_usage_accepts_windows_newlines_and_requires_witness(self):
        probe.verify_shrink_usage(probe.WITNESS + '\r\n')
        with self.assertRaises(ValueError): probe.verify_shrink_usage('some.other.Class\n')


if __name__ == '__main__': unittest.main()
