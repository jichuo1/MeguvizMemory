#!/usr/bin/env python3
"""Offline CMake package contracts with synthetic ELF headers; no NDK/device."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'cmake/MeguvizMemoryConfig.cmake.in'
PACKAGE_INIT = '''
macro(check_required_components _name)
  foreach(_component ${${_name}_FIND_COMPONENTS})
    if(NOT ${_name}_${_component}_FOUND AND ${_name}_FIND_REQUIRED_${_component})
      set(${_name}_FOUND FALSE)
    endif()
  endforeach()
endmacro()
'''
CORE_TARGETS = '''
if(TARGET MeguvizMemory::mgmem AND TARGET MeguvizMemory::pressure)
  return()
endif()
get_filename_component(_fixture_lib "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)
add_library(MeguvizMemory::mgmem SHARED IMPORTED)
set_target_properties(MeguvizMemory::mgmem PROPERTIES IMPORTED_CONFIGURATIONS RELEASE
  IMPORTED_LOCATION_RELEASE "${_fixture_lib}/libmgmem.so")
add_library(MeguvizMemory::pressure STATIC IMPORTED)
'''
JNI_TARGETS = '''
if(TARGET MeguvizMemory::android_jni)
  return()
endif()
get_filename_component(_fixture_lib "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)
add_library(MeguvizMemory::android_jni SHARED IMPORTED)
set_target_properties(MeguvizMemory::android_jni PROPERTIES IMPORTED_CONFIGURATIONS RELEASE
  IMPORTED_LOCATION_RELEASE "${_fixture_lib}/libmgmem_pressure_jni.so"
  INTERFACE_LINK_LIBRARIES "MeguvizMemory::mgmem")
'''


def elf(machine=183, klass=2):
    value = bytearray(20)
    value[:6] = b'\x7fELF' + bytes((klass, 1))
    value[16:18] = (3).to_bytes(2, 'little')
    value[18:20] = machine.to_bytes(2, 'little')
    return bytes(value)


def create_sdk(root, with_jni=True, core_machine=183, jni_machine=183):
    lib = root / 'lib'
    config = lib / 'cmake/MeguvizMemory'
    config.mkdir(parents=True)
    (lib / 'libmgmem.so').write_bytes(elf(core_machine))
    (config / 'MeguvizMemoryConfig.cmake').write_text(
        TEMPLATE.read_text(encoding='utf-8').replace('@PACKAGE_INIT@', PACKAGE_INIT), encoding='utf-8')
    (config / 'MeguvizMemoryTargets.cmake').write_text(CORE_TARGETS, encoding='utf-8')
    if with_jni:
        (lib / 'libmgmem_pressure_jni.so').write_bytes(elf(jni_machine))
        (config / 'MeguvizMemoryAndroidJniABI.cmake').write_text(
            'set(MeguvizMemory_ANDROID_JNI_ABI "arm64-v8a")\n', encoding='utf-8')
        (config / 'MeguvizMemoryAndroidJniTargets.cmake').write_text(JNI_TARGETS, encoding='utf-8')
    return config


class AndroidJniConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cmake = os.environ.get('MGMEM_CMAKE') or shutil.which('cmake')
        if not cls.cmake:
            raise RuntimeError('A local CMake executable is required')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.module = self.root / 'modules'
        self.module.mkdir()
        (self.module / 'FindThreads.cmake').write_text(
            'set(Threads_FOUND TRUE)\nif(NOT TARGET Threads::Threads)\n'
            '  add_library(Threads::Threads INTERFACE IMPORTED)\nendif()\n', encoding='utf-8')
        self.source = self.root / 'project'
        self.source.mkdir()

    def configure(self, body, expect_success, reason=None, android=True):
        prelude = ('cmake_minimum_required(VERSION 3.25)\nproject(JniContract NONE)\n'
                   'list(PREPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/../modules")\n')
        if android:
            prelude += 'set(ANDROID TRUE)\nset(CMAKE_SYSTEM_NAME Android)\nset(CMAKE_ANDROID_ARCH_ABI arm64-v8a)\nset(ANDROID_ABI arm64-v8a)\n'
        else:
            prelude += 'set(ANDROID TRUE)\nset(CMAKE_SYSTEM_NAME Linux)\nset(CMAKE_ANDROID_ARCH_ABI arm64-v8a)\n'
        (self.source / 'CMakeLists.txt').write_text(prelude + body, encoding='utf-8')
        result = subprocess.run([str(self.cmake), '-S', str(self.source), '-B', str(self.root / 'build')],
                                capture_output=True, text=True, timeout=30, check=False)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode == 0, expect_success, output)
        if reason is not None:
            self.assertIn(reason, output)

    def test_core_only_package_is_compatible_and_required_component_fails(self):
        directory = create_sdk(self.root / 'a', with_jni=False).as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{directory}")\nfind_package(MeguvizMemory CONFIG REQUIRED)\n'
                       'if(TARGET MeguvizMemory::android_jni)\nmessage(FATAL_ERROR "unexpected_target")\nendif()\n', True)

    def test_missing_component_is_explicitly_rejected(self):
        directory = create_sdk(self.root / 'a', with_jni=False).as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{directory}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_same_prefix_can_be_requested_twice(self):
        directory = create_sdk(self.root / 'a').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{directory}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       'get_target_property(links MeguvizMemory::android_jni INTERFACE_LINK_LIBRARIES)\n'
                       'if(NOT "MeguvizMemory::mgmem" IN_LIST links)\nmessage(FATAL_ERROR "core_missing")\nendif()\n', True)

    def test_wrong_core_elf_is_rejected(self):
        directory = create_sdk(self.root / 'a', core_machine=62).as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{directory}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_wrong_jni_elf_is_rejected(self):
        directory = create_sdk(self.root / 'a', jni_machine=62).as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{directory}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_non_shared_elf_is_rejected(self):
        directory = create_sdk(self.root / 'a')
        binary = self.root / 'a/lib/libmgmem_pressure_jni.so'
        value = bytearray(binary.read_bytes())
        value[16:18] = (2).to_bytes(2, 'little')
        binary.write_bytes(value)
        self.configure(f'set(MeguvizMemory_DIR "{directory.as_posix()}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_mismatched_abi_metadata_is_rejected(self):
        directory = create_sdk(self.root / 'a')
        (directory / 'MeguvizMemoryAndroidJniABI.cmake').write_text(
            'set(MeguvizMemory_ANDROID_JNI_ABI "x86_64")\n', encoding='utf-8')
        self.configure(f'set(MeguvizMemory_DIR "{directory.as_posix()}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_same_abi_other_prefix_does_not_reuse_core(self):
        first = create_sdk(self.root / 'first').as_posix()
        second = create_sdk(self.root / 'second').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       f'set(MeguvizMemory_DIR "{second}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_existing_jni_target_from_another_prefix_is_rejected(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        other = (self.root / 'second/lib/libmgmem_pressure_jni.so').as_posix()
        self.configure('add_library(MeguvizMemory::android_jni SHARED IMPORTED)\n'
                       'set_target_properties(MeguvizMemory::android_jni PROPERTIES IMPORTED_CONFIGURATIONS RELEASE\n'
                       f'  IMPORTED_LOCATION_RELEASE "{other}" INTERFACE_LINK_LIBRARIES "MeguvizMemory::mgmem")\n'
                       f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_existing_core_target_with_extra_transitive_library_is_rejected(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        core = (self.root / 'first/lib/libmgmem.so').as_posix()
        other = (self.root / 'second/lib/libmgmem.so').as_posix()
        self.configure('add_library(MeguvizMemory::mgmem SHARED IMPORTED)\n'
                       'set_target_properties(MeguvizMemory::mgmem PROPERTIES IMPORTED_CONFIGURATIONS RELEASE\n'
                       f'  IMPORTED_LOCATION_RELEASE "{core}" INTERFACE_LINK_LIBRARIES "{other}")\n'
                       'add_library(MeguvizMemory::pressure STATIC IMPORTED)\n'
                       f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_existing_core_legacy_generic_link_is_rejected_on_first_find(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        core = (self.root / 'first/lib/libmgmem.so').as_posix()
        other = (self.root / 'second/lib/libmgmem.so').as_posix()
        self.configure('add_library(MeguvizMemory::mgmem SHARED IMPORTED)\n'
                       'set_target_properties(MeguvizMemory::mgmem PROPERTIES IMPORTED_CONFIGURATIONS RELEASE\n'
                       f'  IMPORTED_LOCATION_RELEASE "{core}" IMPORTED_LINK_INTERFACE_LIBRARIES "{other}")\n'
                       'add_library(MeguvizMemory::pressure STATIC IMPORTED)\n'
                       f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_existing_core_legacy_release_link_is_rejected(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        other = (self.root / 'second/lib/libmgmem.so').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       f'set_target_properties(MeguvizMemory::mgmem PROPERTIES IMPORTED_LINK_INTERFACE_LIBRARIES_RELEASE "{other}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_mapped_unlisted_core_legacy_link_is_rejected(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        core = (self.root / 'first/lib/libmgmem.so').as_posix()
        other = (self.root / 'second/lib/libmgmem.so').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       'set_target_properties(MeguvizMemory::mgmem PROPERTIES MAP_IMPORTED_CONFIG_DEBUG OTHER\n'
                       f'  IMPORTED_LOCATION_OTHER "{core}" IMPORTED_LINK_INTERFACE_LIBRARIES_OTHER "{other}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_other_prefix_core_in_second_configuration_is_rejected(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        other = (self.root / 'second/lib/libmgmem.so').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       'set_property(TARGET MeguvizMemory::mgmem APPEND PROPERTY IMPORTED_CONFIGURATIONS DEBUG)\n'
                       f'set_target_properties(MeguvizMemory::mgmem PROPERTIES IMPORTED_LOCATION_DEBUG "{other}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_other_prefix_jni_in_second_configuration_is_rejected(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        other = (self.root / 'second/lib/libmgmem_pressure_jni.so').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       'set_property(TARGET MeguvizMemory::android_jni APPEND PROPERTY IMPORTED_CONFIGURATIONS DEBUG)\n'
                       f'set_target_properties(MeguvizMemory::android_jni PROPERTIES IMPORTED_LOCATION_DEBUG "{other}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_other_prefix_default_core_location_is_rejected(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        other = (self.root / 'second/lib/libmgmem.so').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       f'set_target_properties(MeguvizMemory::mgmem PROPERTIES IMPORTED_LOCATION "{other}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_unlisted_direct_debug_core_location_is_rejected(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        other = (self.root / 'second/lib/libmgmem.so').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       f'set_target_properties(MeguvizMemory::mgmem PROPERTIES IMPORTED_LOCATION_DEBUG "{other}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_unlisted_direct_debug_jni_location_is_rejected(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        other = (self.root / 'second/lib/libmgmem_pressure_jni.so').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       f'set_target_properties(MeguvizMemory::android_jni PROPERTIES IMPORTED_LOCATION_DEBUG "{other}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_core_map_to_unlisted_other_prefix_is_rejected(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        other = (self.root / 'second/lib/libmgmem.so').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       'set_target_properties(MeguvizMemory::mgmem PROPERTIES MAP_IMPORTED_CONFIG_DEBUG OTHER\n'
                       f'  IMPORTED_LOCATION_OTHER "{other}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_jni_map_to_unlisted_other_prefix_is_rejected(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        other = (self.root / 'second/lib/libmgmem_pressure_jni.so').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       'set_target_properties(MeguvizMemory::android_jni PROPERTIES MAP_IMPORTED_CONFIG_DEBUG OTHER\n'
                       f'  IMPORTED_LOCATION_OTHER "{other}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_valid_config_mapping_to_release_is_allowed(self):
        first = create_sdk(self.root / 'first').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       'set_target_properties(MeguvizMemory::android_jni PROPERTIES MAP_IMPORTED_CONFIG_DEBUG RELEASE)\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', True)

    def test_missing_first_mapped_config_falls_back_to_release(self):
        first = create_sdk(self.root / 'first').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       'set_target_properties(MeguvizMemory::mgmem PROPERTIES MAP_IMPORTED_CONFIG_DEBUG "DEBUG;RELEASE")\n'
                       'set_target_properties(MeguvizMemory::android_jni PROPERTIES MAP_IMPORTED_CONFIG_DEBUG "DEBUG;RELEASE")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', True)

    def test_first_available_mapped_foreign_location_is_rejected(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        other = (self.root / 'second/lib/libmgmem.so').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       'set_target_properties(MeguvizMemory::mgmem PROPERTIES MAP_IMPORTED_CONFIG_DEBUG "OTHER;RELEASE"\n'
                       f'  IMPORTED_LOCATION_OTHER "{other}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_first_mapped_location_set_but_missing_is_not_skipped(self):
        first = create_sdk(self.root / 'first').as_posix()
        missing = (self.root / 'missing/libmgmem.so').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       'set_target_properties(MeguvizMemory::mgmem PROPERTIES MAP_IMPORTED_CONFIG_DEBUG "DEBUG;RELEASE"\n'
                       f'  IMPORTED_LOCATION_DEBUG "{missing}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_first_mapped_location_set_empty_is_not_skipped(self):
        first = create_sdk(self.root / 'first').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       'set_target_properties(MeguvizMemory::mgmem PROPERTIES MAP_IMPORTED_CONFIG_DEBUG "DEBUG;RELEASE"\n'
                       '  IMPORTED_LOCATION_DEBUG "")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_unselected_later_mapped_location_does_not_block(self):
        first = create_sdk(self.root / 'first').as_posix()
        create_sdk(self.root / 'second')
        other = (self.root / 'second/lib/libmgmem.so').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{first}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n'
                       'set_target_properties(MeguvizMemory::mgmem PROPERTIES MAP_IMPORTED_CONFIG_DEBUG "RELEASE;OTHER"\n'
                       f'  IMPORTED_LOCATION_OTHER "{other}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', True)

    def test_first_import_jni_from_other_prefix_fails_default_find(self):
        first = create_sdk(self.root / 'first')
        create_sdk(self.root / 'second')
        other = (self.root / 'second/lib/libmgmem_pressure_jni.so').as_posix()
        (first / 'MeguvizMemoryAndroidJniTargets.cmake').write_text(
            JNI_TARGETS.replace('${_fixture_lib}/libmgmem_pressure_jni.so', other), encoding='utf-8')
        self.configure(f'set(MeguvizMemory_DIR "{first.as_posix()}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED)\n', False,
                       'Android JNI target is incompatible with this package')

    def test_first_import_jni_extra_interface_link_is_rejected(self):
        first = create_sdk(self.root / 'first')
        (first / 'MeguvizMemoryAndroidJniTargets.cmake').write_text(
            JNI_TARGETS.replace('INTERFACE_LINK_LIBRARIES "MeguvizMemory::mgmem"',
                                'INTERFACE_LINK_LIBRARIES "MeguvizMemory::mgmem;rogue"'), encoding='utf-8')
        self.configure(f'set(MeguvizMemory_DIR "{first.as_posix()}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_first_import_jni_missing_core_interface_is_rejected(self):
        first = create_sdk(self.root / 'first')
        (first / 'MeguvizMemoryAndroidJniTargets.cmake').write_text(
            JNI_TARGETS.replace('INTERFACE_LINK_LIBRARIES "MeguvizMemory::mgmem"',
                                'INTERFACE_LINK_LIBRARIES ""'), encoding='utf-8')
        self.configure(f'set(MeguvizMemory_DIR "{first.as_posix()}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_first_import_jni_multi_config_foreign_path_is_rejected(self):
        first = create_sdk(self.root / 'first')
        create_sdk(self.root / 'second')
        other = (self.root / 'second/lib/libmgmem_pressure_jni.so').as_posix()
        modified = JNI_TARGETS.replace('IMPORTED_CONFIGURATIONS RELEASE', 'IMPORTED_CONFIGURATIONS "RELEASE;DEBUG"')
        modified = modified.replace('INTERFACE_LINK_LIBRARIES "MeguvizMemory::mgmem"',
                                    f'IMPORTED_LOCATION_DEBUG "{other}" INTERFACE_LINK_LIBRARIES "MeguvizMemory::mgmem"')
        (first / 'MeguvizMemoryAndroidJniTargets.cmake').write_text(modified, encoding='utf-8')
        self.configure(f'set(MeguvizMemory_DIR "{first.as_posix()}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI')

    def test_host_configuration_does_not_expose_android_elf_target(self):
        directory = create_sdk(self.root / 'a').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{directory}")\nfind_package(MeguvizMemory CONFIG REQUIRED)\n'
                       'if(TARGET MeguvizMemory::android_jni)\nmessage(FATAL_ERROR "android_target_on_host")\nendif()\n',
                       True, android=False)

    def test_host_cannot_require_android_jni(self):
        directory = create_sdk(self.root / 'a').as_posix()
        self.configure(f'set(MeguvizMemory_DIR "{directory}")\n'
                       'find_package(MeguvizMemory CONFIG REQUIRED COMPONENTS android_jni)\n', False,
                       'Android JNI is not installed for the requested Android ABI', android=False)


if __name__ == '__main__':
    unittest.main()
