#!/usr/bin/env python3
"""Focused adversarial checks for the feature-preview privacy boundary."""
import io
import unittest
import zipfile

from package_feature_preview import Scanner


def archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as package:
        for name, payload in entries:
            package.writestr(name, payload)
    return output.getvalue()


class FeaturePreviewScannerChecks(unittest.TestCase):
    def test_safe_nested_archive(self):
        inner = archive([('code.class', b'ordinary class bytes')])
        Scanner(['private-marker']).archive(archive([('classes.jar', inner)]), 'bundle')

    def test_known_private_marker_all_supported_encodings(self):
        for encoding in ('utf-8', 'utf-16le', 'utf-16be'):
            with self.subTest(encoding=encoding), self.assertRaises(ValueError):
                Scanner(['private-marker']).archive(archive([('code.bin', 'PrIvAtE-MaRkEr'.encode(encoding))]), 'bundle')

    def test_machine_path_all_supported_encodings(self):
        for path in ('Q:/Users/example/file', '/home/example/file'):
            for encoding in ('utf-8', 'utf-16le', 'utf-16be'):
                with self.subTest(path=path, encoding=encoding), self.assertRaises(ValueError):
                    Scanner(['private-marker']).archive(archive([('data.bin', path.encode(encoding))]), 'bundle')

    def test_forbidden_nested_artifact(self):
        inner = archive([('sample.apk', b'not an actual apk')])
        with self.assertRaises(ValueError):
            Scanner(['private-marker']).archive(archive([('inner.zip', inner)]), 'bundle')

    def test_traversal_and_case_collision(self):
        for entries in ([('../escape', b'x')], [('item', b'x'), ('ITEM', b'y')]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                Scanner(['private-marker']).archive(archive(entries), 'bundle')

    def test_symlink(self):
        output = io.BytesIO()
        info = zipfile.ZipInfo('link')
        info.external_attr = 0o120777 << 16
        with zipfile.ZipFile(output, 'w') as package:
            package.writestr(info, 'target')
        with self.assertRaises(ValueError):
            Scanner(['private-marker']).archive(output.getvalue(), 'bundle')

    def test_archive_prefix_and_trailing_overlay(self):
        valid = archive([('item', b'x')])
        for data in (b'/home/example/file'+valid, valid+b'Q:/Users/example/file'):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                Scanner(['private-marker']).archive(data, 'bundle')


if __name__ == '__main__':
    unittest.main()
