#!/usr/bin/env python3
"""Offline contract tests for the isolated native-pressure diagnostic builder."""
import unittest

import build_android_native_pressure_probe as probe


TREE = '''N: android=http://schemas.android.com/apk/res/android (line=1)
  E: manifest (line=1)
    A: package="io.github.jichuo1.meguviz.memory.nativepressureprobe"
      E: uses-sdk (line=2)
        A: http://schemas.android.com/apk/res/android:minSdkVersion(0x0101020c)=27
        A: http://schemas.android.com/apk/res/android:targetSdkVersion(0x01010270)=33
      E: application (line=3)
        A: http://schemas.android.com/apk/res/android:label(0x01010001)="Probe"
        A: http://schemas.android.com/apk/res/android:debuggable(0x0101000f)=true
        A: http://schemas.android.com/apk/res/android:allowBackup(0x01010280)=false
      E: instrumentation (line=5)
        A: http://schemas.android.com/apk/res/android:name(0x01010003)="io.github.jichuo1.meguviz.memory.android.NativePressureCloseInstrumentation"
        A: http://schemas.android.com/apk/res/android:targetPackage(0x01010021)="io.github.jichuo1.meguviz.memory.nativepressureprobe"
        A: http://schemas.android.com/apk/res/android:functionalTest(0x01010023)=true
'''
OWNER = 'io.github.jichuo1.meguviz.memory.android.NativePressureBackend'
DESC = 'Lio/github/jichuo1/meguviz/memory/android/NativePressureBackend;'
MAPPING = OWNER + ' -> ' + OWNER + ':\n    1:33:void onNativeClosed(int):0:0 -> onNativeClosed\n'
DEX = ("Class #0            -\n  Class descriptor  : '" + DESC + "'\n"
       "    #19 : (in " + DESC + ")\n      name : 'onNativeClosed'\n"
       "      type : '(I)V'\n      access : 0x0002 (PRIVATE)\n")


class NativePressureProbeContracts(unittest.TestCase):
    def test_manifest_valid(self):
        probe.verify_manifest_tree(TREE)

    def test_rejects_other_package_and_target(self):
        for bad in (TREE.replace('memory.nativepressureprobe', 'memory.managedprobe'),
                    TREE.replace('NativePressureCloseInstrumentation', 'ManagedInstrumentation')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                probe.verify_manifest_tree(bad)

    def test_rejects_extra_components_and_changed_sdk(self):
        for bad in (TREE + '      E: service (line=6)\n',
                    TREE.replace('targetSdkVersion(0x01010270)=33', 'targetSdkVersion(0x01010270)=34'),
                    TREE.replace('android:debuggable(0x0101000f)=true', 'android:debuggable(0x0101000f)=false'),
                    TREE.replace('android:allowBackup(0x01010280)=false', 'android:allowBackup(0x01010280)=true')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                probe.verify_manifest_tree(bad)

    def test_rejects_duplicate_attributes_and_wrong_hierarchy(self):
        line = '        A: http://schemas.android.com/apk/res/android:debuggable(0x0101000f)=true\n'
        for bad in (TREE.replace(line, line + line), TREE.replace('      E: instrumentation', '        E: instrumentation')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                probe.verify_manifest_tree(bad)

    def test_callback_valid_and_windows_newlines(self):
        probe.verify_callback(MAPPING, DEX)
        probe.verify_callback(MAPPING.replace('\n', '\r\n'), DEX)

    def test_callback_rejects_obfuscation_and_wrong_mapping_owner(self):
        for bad in (MAPPING.replace('-> onNativeClosed', '-> a'),
                    MAPPING.replace(OWNER + ' -> ' + OWNER, OWNER + ' -> obfuscated.Name'),
                    MAPPING + MAPPING):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                probe.verify_callback(bad, DEX)

    def test_callback_rejects_missing_wrong_signature_or_visibility(self):
        for bad in (DEX.replace('onNativeClosed', 'changed'), DEX.replace('(I)V', '(J)V'),
                    DEX.replace('(PRIVATE)', '(PUBLIC)'), DEX + DEX):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                probe.verify_callback(MAPPING, bad)

    def test_callback_from_other_class_does_not_satisfy(self):
        bad_mapping = OWNER + ' -> ' + OWNER + ':\nOther -> Other:\n    void onNativeClosed(int) -> onNativeClosed\n'
        with self.assertRaises(ValueError):
            probe.verify_callback(bad_mapping, DEX)

    def test_shrink_witness_handles_windows_newline_and_rejects_absence(self):
        probe.verify_shrink_usage((probe.WITNESS + '\r\n').encode('utf-8'))
        with self.assertRaises(ValueError):
            probe.verify_shrink_usage(b'other.Class\r\n')

    def test_native_declaration_parser_rejects_unhandled_declarations(self):
        good = '  private static native long nativeCreate(boolean);\n    descriptor: (Z)J\n'
        self.assertEqual(probe.native_descriptors(good), {'nativeCreate': '(Z)J'})
        for bad in (good + '  public native void changed();\n    descriptor: ()V\n', good + good):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                probe.native_descriptors(bad)


if __name__ == '__main__':
    unittest.main()
