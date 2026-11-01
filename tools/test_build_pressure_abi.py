#!/usr/bin/env python3
"""Check that Android packaging rejects mismatched native ELF architectures."""
import io
import struct

from build_pressure_android import verify_elf_abi


def header(elf_class, machine):
    data = bytearray(20)
    data[:4] = b'\x7fELF'
    data[4], data[5] = elf_class, 1
    struct.pack_into('<H', data, 18, machine)
    return data


def main():
    class Input:
        def __init__(self, data):
            self.data = data

        def open(self, mode):
            assert mode == 'rb'
            return io.BytesIO(self.data)

    for abi, elf_class, machine in [('arm64-v8a', 2, 183), ('x86_64', 2, 62), ('armeabi-v7a', 1, 40)]:
        source = Input(header(elf_class, machine))
        verify_elf_abi(source, abi)
        wrong = 'x86_64' if abi != 'x86_64' else 'arm64-v8a'
        try:
            verify_elf_abi(source, wrong)
        except ValueError:
            pass
        else:
            raise AssertionError('wrong ABI accepted')
    for invalid in (b'', b'\x7fELF', b'\x00' * 20):
        try:
            verify_elf_abi(Input(invalid), 'arm64-v8a')
        except ValueError:
            pass
        else:
            raise AssertionError('malformed header accepted')
    print('Android ABI package input checks passed')


if __name__ == '__main__':
    main()
