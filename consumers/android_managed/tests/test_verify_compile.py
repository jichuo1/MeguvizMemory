#!/usr/bin/env python3
"""Archive-structure tests only; synthetic .so bytes are never executed."""

import importlib.util
import io
from pathlib import Path
import sys
import unittest
import warnings
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'verify_compile.py'
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('managed_sample_verify_compile', SCRIPT)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


def class_jar(missing=None, duplicate=None):
    data = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(data, 'w') as archive:
        warnings.simplefilter('ignore', UserWarning)
        for name in verify.REQUIRED_CLASSES:
            if name != missing:
                archive.writestr(verify.PREFIX + name, b'synthetic-class')
        if duplicate:
            archive.writestr(verify.PREFIX + duplicate, b'duplicate-class')
    return data.getvalue()


def aar(entries):
    data = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(data, 'w') as archive:
        warnings.simplefilter('ignore', UserWarning)
        archive.writestr('classes.jar', class_jar())
        for entry, body in entries:
            archive.writestr(entry, body)
    data.seek(0)
    return data


class ArchiveContracts(unittest.TestCase):
    def setUp(self):
        self.native = [('jni/arm64-v8a/libmgmem.so', b'core'),
                       ('jni/arm64-v8a/libmgmem_pressure_jni.so', b'jni')]

    def test_valid_structure(self):
        classes, abis = verify.extract_classpath(aar(self.native))
        verify.verify_classes(classes)
        self.assertEqual(abis, ['arm64-v8a'])

    def test_extra_abi_without_core_and_jni_rejected(self):
        with self.assertRaises(ValueError):
            verify.extract_classpath(aar(self.native + [('jni/x86_64/libother.so', b'other')]))

    def test_duplicate_or_empty_native_rejected(self):
        for entries in (self.native + [self.native[0]], [self.native[0], (self.native[1][0], b'')]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                verify.extract_classpath(aar(entries))

    def test_missing_or_duplicate_classes_rejected(self):
        for blob in (class_jar(missing='ManagedMemoryDomain$Pressure.class'),
                     class_jar(duplicate='ManagedPressureBackend.class')):
            with self.assertRaises(ValueError):
                verify.verify_classes(blob)


if __name__ == '__main__':
    unittest.main()
