"""Offline adversarial checks for the private arm64 profile, no device needed."""
import io
import json
import os
from pathlib import Path
import secrets
import struct
from types import SimpleNamespace
import unittest
from unittest import mock
import warnings
import zipfile

import freeze_arm64_input_profile as profile


def fake_elf(needed, *, executable=True, machine=183, interpreter=b'/system/bin/linker64\0', extra_tag=None):
    data = bytearray(1024)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HH', data, 16, 3, machine)
    struct.pack_into('<I', data, 20, 1)
    struct.pack_into('<Q', data, 24, 128 if executable else 0)
    struct.pack_into('<Q', data, 32, 64)
    struct.pack_into('<H', data, 52, 64)
    struct.pack_into('<HH', data, 54, 56, 3 if executable else 2)
    struct.pack_into('<IIQQQQQQ', data, 64, 1, 5, 0, 0, 0, len(data), len(data), 4096)
    tags = []
    names = b'\0'
    for name in needed:
        tags.append((1, len(names)))
        names += name.encode('ascii') + b'\0'
    tags += [(5, 768), (10, len(names))]
    if extra_tag is not None:
        tags.append((extra_tag, 0))
    tags.append((0, 0))
    struct.pack_into('<IIQQQQQQ', data, 120, 2, 4, 512, 512, 0, len(tags) * 16, len(tags) * 16, 8)
    if executable:
        struct.pack_into('<IIQQQQQQ', data, 176, 3, 4, 400, 400, 0, len(interpreter), len(interpreter), 1)
        data[400:400 + len(interpreter)] = interpreter
    for index, (tag, value) in enumerate(tags):
        struct.pack_into('<qQ', data, 512 + index * 16, tag, value)
    data[768:768 + len(names)] = names
    return bytes(data)


class ElfTests(unittest.TestCase):
    def test_expected_core_and_executable(self):
        self.assertEqual(profile.elf_info(fake_elf(sorted(profile.LIBS), executable=False), False)['machine'], 'AARCH64')
        self.assertEqual(profile.elf_info(fake_elf(sorted(profile.NATIVE_LIBS)), True)['needed'], sorted(profile.NATIVE_LIBS))

    def test_wrong_arch_missing_extra_duplicate_and_wrong_interpreter(self):
        cases = [fake_elf(sorted(profile.NATIVE_LIBS), machine=62),
                 fake_elf(sorted(profile.LIBS)),
                 fake_elf(sorted(profile.NATIVE_LIBS) + ['libevil.so']),
                 fake_elf(sorted(profile.NATIVE_LIBS) + ['libc.so']),
                 fake_elf(sorted(profile.NATIVE_LIBS), interpreter=b'/bad/linker\0'),
                 fake_elf(sorted(profile.NATIVE_LIBS), extra_tag=29), b'\x7fELF']
        for value in cases:
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                profile.elf_info(value, True)

    def test_malformed_dynamic_string_table(self):
        value = bytearray(fake_elf(sorted(profile.NATIVE_LIBS)))
        struct.pack_into('<Q', value, 512 + 4 * 16 + 8, 0xffffffffffffffff)
        with self.assertRaises(ValueError):
            profile.elf_info(bytes(value), True)

    def test_jni_shared_header_and_exact_dependencies(self):
        good = fake_elf(sorted(profile.NATIVE_LIBS), executable=False)
        self.assertEqual(profile.elf_info(good, False, jni=True)['kind'], 'SHARED_JNI')
        malformed = []
        for offset in (6, 20, 52):
            value = bytearray(good)
            value[offset] = 0
            malformed.append(bytes(value))
        malformed += [fake_elf(sorted(profile.NATIVE_LIBS), executable=False, machine=62),
                      fake_elf(sorted(profile.LIBS), executable=False),
                      fake_elf(sorted(profile.NATIVE_LIBS) + ['libevil.so'], executable=False),
                      fake_elf(sorted(profile.NATIVE_LIBS))]
        for value in malformed:
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                profile.elf_info(value, False, jni=True)


class ArchiveTests(unittest.TestCase):
    def test_duplicate_and_traversal_rejected(self):
        for names in [('a', 'a'), ('../outside',)]:
            buf = io.BytesIO()
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                with zipfile.ZipFile(buf, 'w') as z:
                    for name in names:
                        z.writestr(name, b'x')
            with self.subTest(names=names), self.assertRaises(ValueError):
                profile.zip_members(buf.getvalue())

    def test_duplicate_json_rejected(self):
        with self.assertRaises(ValueError):
            profile.unique_json(b'{"schema_version": 1, "schema_version": 2}')


class DriftTests(unittest.TestCase):
    def test_profile_hash_and_recomputed_snapshot(self):
        value = {'schema_version': 1, 'state': 'FROZEN_LOCAL_INPUTS_NOT_DEVICE',
                 'profile_id': 'a' * 32, 'snapshot': {'config': {}, 'marker': 'original'}}
        data = json.dumps(value).encode()
        def read(path, *_args):
            return (Path(path), (profile.sha(data) + '\n').encode() if Path(path).name == 'profile.sha256' else data)
        with mock.patch.object(profile, 'regular', side_effect=read), \
             mock.patch.object(profile, 'build_root', return_value=Path('.')), \
             mock.patch.object(profile.safe, 'read_regular', return_value=data):
            with self.assertRaisesRegex(ValueError, 'profile_hash_mismatch'):
                profile.validate(Path('unused'), '0' * 64)
            with mock.patch.object(profile, 'inspect', return_value={'config': {}, 'marker': 'changed'}):
                with self.assertRaisesRegex(ValueError, 'profile_input_drift'):
                    profile.validate(Path('unused'), profile.sha(data))
            with mock.patch.object(profile, 'inspect', return_value=value['snapshot']):
                self.assertEqual(profile.validate(Path('unused'), profile.sha(data)), value)

    def test_reparse_and_hardlink_rejected(self):
        with mock.patch.object(profile.safe, 'read_regular', side_effect=ValueError('hardlink')), \
             mock.patch.object(profile, 'confined', return_value=Path('unused')):
            with self.assertRaisesRegex(ValueError, 'hardlink'):
                profile.regular(Path('unused'), Path('.'))
        with mock.patch.object(profile.safe, 'confined', return_value=Path('unused')), \
             mock.patch.object(Path, 'lstat', return_value=object()), \
             mock.patch.object(profile.safe, 'is_link', return_value=True):
            with self.assertRaisesRegex(ValueError, 'path_link_or_reparse'):
                profile.confined(Path('unused'), Path('.'))

    def test_actual_hardlink_rejected(self):
        root = profile.build_root()
        directory = root / ('profile-negative-' + secrets.token_hex(8))
        directory.mkdir()
        first, second = directory / 'first', directory / 'second'
        first.write_bytes(b'fake input')
        try:
            os.link(first, second)
        except OSError:
            self.skipTest('hard links are unavailable in this filesystem')
        with self.assertRaises(ValueError):
            profile.regular(first, root)
        with self.assertRaisesRegex(ValueError, 'tool_invalid'):
            profile.external_file(first)

    def test_external_link_ancestor_rejected(self):
        with mock.patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0)), \
             mock.patch.object(profile.stat, 'S_ISDIR', return_value=True), \
             mock.patch.object(profile.safe, 'is_link', return_value=True):
            with self.assertRaisesRegex(ValueError, 'tool_ancestor_link_or_invalid'):
                profile.external_ancestors(Path(profile.ROOT / 'fake' / 'tool'))


if __name__ == '__main__':
    unittest.main()
