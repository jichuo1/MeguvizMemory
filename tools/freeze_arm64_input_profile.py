#!/usr/bin/env python3
"""Freeze or revalidate private arm64 validation inputs; never invokes ADB.

The profile is a local integrity record, not signed provenance, a device lock,
or permission to execute tests. No input contents or identities are printed.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import sys
import zipfile

import build_android_candidate as android
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'consumers/dex_trace'))
import prepare_consumer as safe

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r'^[0-9a-f]{64}$')
MAX_FILE = 64 * 1024 * 1024
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_PROFILE = 4 * 1024 * 1024
LIBS = frozenset({'libc.so', 'libdl.so', 'libm.so'})
NATIVE_LIBS = LIBS | {'libmgmem.so'}
ROLES = {
    'library': 'libmgmem.so',
    'core_safety': 'mgmem_core_tests',
    'c_safety': 'mgmem_c_consumer',
    'cpp_safety': 'mgmem_cpp_consumer',
    'dex_formal': 'mgmem_dex_paired_v2',
    'dex_diagnostic': 'mgmem_dex_paired_v2_diagnostic',
    'image_paired': 'mgmem_image_paired',
    'image_formal': 'mgmem_image_perf',
}
EXTRAS = {'dex_a1a3': 'mgmem_dex_paired', 'dex_import': 'mgmem_dex_import_consumer',
          'image_pressure': 'mgmem_image_pressure_stability'}
HELPERS = ('tools/freeze_arm64_input_profile.py', 'tools/build_android_candidate.py',
           'tools/package_feature_preview.py', 'tools/source_lock.py',
           'consumers/dex_trace/prepare_consumer.py', 'consumers/dex_trace/prepare.py')


def require(condition, code):
    if not condition:
        raise ValueError(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def record(data):
    return {'bytes': len(data), 'sha256': sha(data)}


def unique_json(data):
    return safe.json_object(data.decode('utf-8-sig'))


def build_root():
    path = ROOT / 'build'
    require(os.path.lexists(path) and stat.S_ISDIR(path.lstat().st_mode) and not safe.is_link(path.lstat()), 'build_root_invalid')
    root = path.resolve(strict=True)
    require(root.parent == ROOT.resolve(strict=True), 'build_root_outside_workspace')
    return root


def confined(path, boundary, exists=True):
    # safe.confined checks every ancestor using lstat, including Windows reparse points.
    result = safe.confined(path, boundary, must_exist=exists)
    if exists:
        require(not safe.is_link(result.lstat()), 'path_link_or_reparse')
    return result


def regular(path, boundary, maximum=MAX_FILE):
    result = confined(path, boundary)
    return result, safe.read_regular(result, maximum)


def relative(path):
    return Path(path).relative_to(ROOT).as_posix()


def relative_file(name, boundary, maximum=MAX_FILE):
    path = safe.checked_path(ROOT, name)
    return regular(path, boundary, maximum)


def entry(path, data):
    return {'path': relative(path), **record(data)}


def elf_info(data, executable, *, jni=False):
    """Filter selected ELF64 fields and PT_DYNAMIC; this is not a full loader."""
    require(not (executable and jni), 'elf_kind_invalid')
    require(isinstance(data, bytes) and len(data) >= 64 and data[:7] == b'\x7fELF\x02\x01\x01' and
            struct.unpack_from('<I', data, 20)[0] == 1 and struct.unpack_from('<H', data, 52)[0] == 64,
            'elf_header_invalid')
    require(struct.unpack_from('<H', data, 16)[0] == 3 and struct.unpack_from('<H', data, 18)[0] == 183,
            'elf_not_aarch64_pie')
    entrypoint = struct.unpack_from('<Q', data, 24)[0]
    phoff = struct.unpack_from('<Q', data, 32)[0]
    phentsize, phnum = struct.unpack_from('<HH', data, 54)
    require(phentsize == 56 and 1 <= phnum < 256 and phoff <= len(data) and
            phnum * phentsize <= len(data) - phoff, 'elf_program_headers_invalid')
    loads, dynamic, interpreter = [], [], []
    for index in range(phnum):
        kind, flags, offset, vaddr, _, filesz, memsz, align = struct.unpack_from('<IIQQQQQQ', data, phoff + index * phentsize)
        require(offset <= len(data) and filesz <= len(data) - offset and filesz <= memsz, 'elf_segment_invalid')
        if kind == 1:
            require(align > 0 and (align & (align - 1)) == 0 and (vaddr - offset) % align == 0, 'elf_load_alignment_invalid')
            loads.append((vaddr, offset, filesz))
        elif kind == 2:
            dynamic.append((offset, filesz))
        elif kind == 3:
            interpreter.append(data[offset:offset + filesz])
    require(loads and len(dynamic) == 1 and
            (interpreter == [b'/system/bin/linker64\0'] and entrypoint > 0 if executable
             else not interpreter and entrypoint == 0), 'elf_program_kind_invalid')
    off, size = dynamic[0]
    require(size >= 16 and size % 16 == 0, 'elf_dynamic_invalid')
    entries = []
    terminated = False
    for pos in range(off, off + size, 16):
        tag, value = struct.unpack_from('<qQ', data, pos)
        if tag == 0:
            terminated = True
            break
        entries.append((tag, value))
    require(terminated, 'elf_dynamic_unterminated')
    require(not any(tag in (15, 29) for tag, _ in entries), 'elf_search_path_forbidden')
    tables = [value for tag, value in entries if tag == 5]
    sizes = [value for tag, value in entries if tag == 10]
    offsets = [value for tag, value in entries if tag == 1]
    require(len(tables) == len(sizes) == 1 and 0 < sizes[0] <= 8 * 1024 * 1024 and offsets, 'elf_string_table_invalid')
    location = [(off0 + tables[0] - va, length - (tables[0] - va)) for va, off0, length in loads
                if va <= tables[0] < va + length]
    require(len(location) == 1 and sizes[0] <= location[0][1], 'elf_string_table_invalid')
    start = location[0][0]
    table = data[start:start + sizes[0]]
    needed = []
    for offset in offsets:
        require(offset < len(table), 'elf_needed_invalid')
        end = table.find(b'\0', offset)
        require(end > offset, 'elf_needed_invalid')
        try:
            value = table[offset:end].decode('ascii')
        except UnicodeError as error:
            raise ValueError('elf_needed_invalid') from error
        require(re.fullmatch(r'[A-Za-z0-9_.+-]{1,128}', value) is not None, 'elf_needed_invalid')
        needed.append(value)
    require(len(needed) == len(set(needed)) and set(needed) == (NATIVE_LIBS if executable or jni else LIBS),
            'elf_needed_mismatch')
    return {'kind': 'PIE_EXECUTABLE' if executable else ('SHARED_JNI' if jni else 'SHARED_CORE'), 'machine': 'AARCH64',
            'interpreter': '/system/bin/linker64' if executable else None, 'needed': sorted(needed)}


def zip_members(data, maximum_total=128 * 1024 * 1024):
    output = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        require(0 < len(names) <= 512 and len(names) == len(set(names)) == len({n.casefold() for n in names}), 'archive_members_invalid')
        total = 0
        for item in infos:
            name = item.filename
            safe.checked_path(Path('.'), name)
            kind = (item.external_attr >> 16) & 0o170000
            require(not item.is_dir() and kind != stat.S_IFLNK and 0 <= item.file_size <= MAX_FILE, 'archive_member_invalid')
            total += item.file_size
            require(total <= maximum_total, 'archive_total_limit')
            value = archive.read(item)
            require(len(value) == item.file_size, 'archive_member_size_invalid')
            output[name] = value
    return output


def inventory_files(directory):
    require(directory.is_dir() and not safe.is_link(directory.lstat()), 'directory_invalid')
    output = {}
    for current, dirs, files in os.walk(directory, followlinks=False):
        parent = Path(current)
        for name in dirs:
            child = parent / name
            require(not safe.is_link(child.lstat()) and child.is_dir(), 'directory_link_or_reparse')
        for name in files:
            child = parent / name
            _, data = regular(child, build_root())
            key = child.relative_to(directory).as_posix()
            require(key.casefold() not in {k.casefold() for k in output}, 'directory_case_collision')
            output[key] = data
    return output


def _required_path(config, key, boundary, directory=False, maximum=MAX_FILE):
    require(isinstance(config, dict) and isinstance(config.get(key), str), 'input_path_missing')
    path = safe.checked_path(ROOT, config[key])
    if directory:
        path = confined(path, boundary)
        require(path.is_dir(), 'input_directory_missing')
        return path, None
    return regular(path, boundary, maximum)


def _build_component(name):
    if name in ('mgmem_core_tests', 'mgmem_c_consumer', 'mgmem_cpp_consumer'):
        return 'safety'
    return 'dex' if name.startswith('mgmem_dex_') else 'image'


def external_ancestors(path):
    require(path.is_absolute() and '..' not in path.parts, 'tool_path_invalid')
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and not safe.is_link(info), 'tool_ancestor_link_or_invalid')


def external_file(path):
    """Hash one specified tool without exposing its absolute path in output."""
    path = Path(path)
    external_ancestors(path)
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and not safe.is_link(before) and before.st_nlink == 1, 'tool_invalid')
    require(before.st_size <= MAX_ARCHIVE, 'tool_oversize')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
                (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns), 'tool_changed')
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        after_open = os.fstat(stream.fileno())
    after = path.lstat()
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink) ==
            (after_open.st_dev, after_open.st_ino, after_open.st_size, after_open.st_mtime_ns, after_open.st_nlink) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink) and not safe.is_link(after), 'tool_changed')
    external_ancestors(path)
    return {'bytes': before.st_size, 'sha256': digest.hexdigest()}


def toolchain_records(sdk):
    root = Path(sdk)
    require(root.is_absolute() and root.is_dir() and not safe.is_link(root.lstat()), 'tool_sdk_invalid')
    external_ancestors(root)
    ndk = root / 'ndk/29.0.14206865'
    llvm = ndk / 'toolchains/llvm/prebuilt/windows-x86_64/bin'
    paths = {'cmake': root / 'cmake/4.1.2/bin/cmake.exe',
             'ninja': root / 'cmake/4.1.2/bin/ninja.exe',
             'toolchain': ndk / 'build/cmake/android.toolchain.cmake',
             'clang': llvm / 'clang.exe', 'clangxx': llvm / 'clang++.exe',
             'clang_scan_deps': llvm / 'clang-scan-deps.exe',
             'linker': llvm / 'ld.lld.exe', 'ar': llvm / 'llvm-ar.exe',
             'ranlib': llvm / 'llvm-ranlib.exe', 'readelf': llvm / 'llvm-readelf.exe'}
    return {key: external_file(path) for key, path in paths.items()}


def inspect(config):
    """Recompute every identity and contract; only private results are returned."""
    require(isinstance(config, dict) and set(config) == {'bundle', 'sdk', 'build_root', 'source_root',
            'source_lock', 'dex_stage', 'dex_lock', 'candidate', 'candidate_evidence', 'postcheck',
            'android_sdk'}, 'input_config_invalid')
    build = build_root()
    root = ROOT.resolve(strict=True)
    bundle, _ = _required_path(config, 'bundle', build, True)
    sdk, _ = _required_path(config, 'sdk', build, True)
    work, _ = _required_path(config, 'build_root', build, True)
    source_root, _ = _required_path(config, 'source_root', build, True)
    source_lock, source_lock_bytes = _required_path(config, 'source_lock', build)
    dex_stage, _ = _required_path(config, 'dex_stage', build, True)
    dex_lock, dex_lock_bytes = _required_path(config, 'dex_lock', root)
    candidate, candidate_bytes = _required_path(config, 'candidate', root, maximum=MAX_ARCHIVE)
    evidence, evidence_bytes = _required_path(config, 'candidate_evidence', root, maximum=1024 * 1024)
    postcheck, post_bytes = _required_path(config, 'postcheck', build, maximum=1024 * 1024)
    require(bundle.parent == work and sdk.parent == work and dex_stage.parent == work and postcheck.parent == work,
            'build_inputs_not_same_work_directory')
    ev = unique_json(evidence_bytes)
    post = unique_json(post_bytes)
    require(ev.get('state') == 'PASS_SCOPED_ENGINEERING_CANDIDATES' and
            isinstance(ev.get('source_commit'), str) and re.fullmatch(r'[0-9a-f]{40}', ev['source_commit']) and
            ev.get('android', {}).get('sha256') == sha(candidate_bytes) and
            post.get('state') == 'PASS_STATIC_CROSS_BUILD_AND_INDEPENDENT_READBACK_ONLY' and
            post.get('source_commit') == ev['source_commit'] and post.get('abi') == 'arm64-v8a' and
            post.get('api_target') == 27 and post.get('device_executed') is False and
            post.get('performance_qualified') is False, 'candidate_or_postcheck_invalid')
    with zipfile.ZipFile(io.BytesIO(candidate_bytes)) as outer:
        names = outer.namelist()
        require(len(names) == len(set(names)), 'candidate_duplicate_member')
        def pick(name):
            require(names.count(name) == 1, 'candidate_member_missing')
            info = outer.getinfo(name)
            require(not info.is_dir() and 0 <= info.file_size <= MAX_FILE, 'candidate_member_invalid')
            value = outer.read(info)
            require(len(value) == info.file_size, 'candidate_member_invalid')
            return value
        sdk_archive = pick('android/arm64-v8a/MeguvizMemory-sdk.zip')
        aar_archive = pick('android/arm64-v8a/MeguvizMemory.aar')
        src_manifest = unique_json(pick('SOURCE.json'))
    profile = ev['android']['profiles']['arm64-v8a']
    require(profile.get('sdk_sha256') == sha(sdk_archive) and profile.get('aar_sha256') == sha(aar_archive) and
            src_manifest.get('source_lock_sha256') == sha(source_lock_bytes) == ev.get('source_lock_sha256') and
            src_manifest.get('local_source_set_sha256') == ev.get('source_set_sha256') and
            src_manifest.get('local_source_count') == ev.get('source_record_count'), 'candidate_member_identity_mismatch')
    sdk_expected = zip_members(sdk_archive)
    sdk_actual = inventory_files(sdk)
    require(sdk_actual == sdk_expected, 'sdk_not_candidate_bytes')
    aar = zip_members(aar_archive)
    require(sdk_expected.get('lib/libmgmem.so') == aar.get('jni/arm64-v8a/libmgmem.so') and
            sdk_expected.get('lib/libmgmem_pressure_jni.so') == aar.get('jni/arm64-v8a/libmgmem_pressure_jni.so'),
            'aar_sdk_native_mismatch')
    jni_elf = elf_info(sdk_expected['lib/libmgmem_pressure_jni.so'], False, jni=True)
    _, source_records = android.verify_lock(source_root, source_lock)
    require(source_records == src_manifest.get('local_source_records') and len(source_records) == post.get('source_records_verified'),
            'source_records_mismatch')
    for name, expected in source_records.items():
        path = safe.checked_path(source_root, name)
        _, data = regular(path, build)
        require(record(data) == expected, 'source_drift')
    dex_manifest = safe.verify_prepared(dex_stage, dex_lock)
    require(len(dex_manifest.get('source_files', {})) == post.get('dex_source_records_verified'), 'dex_source_count_mismatch')
    safety_names = {'mgmem_core_tests': 'core_tests.cpp', 'mgmem_c_consumer': 'c_consumer.c', 'mgmem_cpp_consumer': 'cpp_consumer.cpp'}
    for filename in safety_names.values():
        _, data = regular(work / 'safety-src' / filename, build)
        require(record(data) == source_records.get('tests/' + filename), 'safety_source_drift')
    expected_bundle = set(ROLES.values()) | set(EXTRAS.values())
    bundle_files = inventory_files(bundle)
    require(set(bundle_files) == expected_bundle, 'bundle_file_set_mismatch')
    require(bundle_files['libmgmem.so'] == sdk_expected['lib/libmgmem.so'] and
            record(bundle_files['libmgmem.so']) == post.get('candidate_core') == post.get('bundle_core_identity') and
            record(sdk_expected['lib/libmgmem_pressure_jni.so']) == post.get('candidate_jni'), 'candidate_core_mismatch')
    artifacts = []
    graphs = {}
    for key, filename in {**ROLES, **EXTRAS}.items():
        data = bundle_files[filename]
        spec = elf_info(data, filename != 'libmgmem.so')
        path = bundle / filename
        if filename != 'libmgmem.so':
            expected = post.get('outputs', {}).get(filename, {})
            require(expected.get('identity') == record(data) and expected.get('needed') == spec['needed'] and
                    expected.get('link_uses_candidate_sdk') is True, 'artifact_postcheck_mismatch')
            component = _build_component(filename)
            if component not in graphs:
                graph_path, graph = regular(work / (component + '-build') / 'build.ninja', build, MAX_FILE)
                cache_path, cache = regular(work / (component + '-build') / 'CMakeCache.txt', build, MAX_FILE)
                rules_path, rules = regular(work / (component + '-build') / 'CMakeFiles/rules.ninja', build, MAX_FILE)
                graphs[component] = {'graph': graph.decode('utf-8').replace('\r\n', '\n'), 'identity': entry(graph_path, graph),
                                     'cache': entry(cache_path, cache), 'cache_text': cache.decode('utf-8'),
                                     'rules': entry(rules_path, rules)}
            match = re.search(r'(?ms)^build ' + re.escape(filename) + r':[^\n]*\n(.*?)(?:\n\n|\Z)', graphs[component]['graph'])
            target = (sdk / 'lib/libmgmem.so').as_posix()
            require(match is not None and re.search(r'^  LINK_LIBRARIES = .*' + re.escape(target), match.group(1), re.M),
                    'link_graph_not_candidate')
        artifacts.append({'role': key, 'name': filename, 'path': relative(path), **record(data), 'elf': spec})
    graph = graphs['dex']['graph']
    def defines(target):
        match = re.search(r'(?m)^build CMakeFiles/' + re.escape(target) + r'\.dir/paired_v2\.cpp\.o:[^\n]*\n(?:[^\n]*\n){0,4}?  DEFINES = ([^\r\n]*)$', graph)
        require(match is not None, 'dex_compile_graph_missing')
        return match.group(1)
    require('MGMEM_DEX_V2_DIAGNOSTIC' not in defines('mgmem_dex_paired_v2') and
            'MGMEM_DEX_V2_DIAGNOSTIC=1' in defines('mgmem_dex_paired_v2_diagnostic'), 'dex_build_kind_mismatch')
    helper_records = []
    for name in HELPERS:
        path, data = relative_file(name, root)
        helper_records.append(entry(path, data))
    evidence_paths = [source_lock, dex_lock, candidate, evidence, postcheck, work / 'build_candidate.py',
                      work / 'postcheck.py', work / 'receipt.json', work / 'safety-src/CMakeLists.txt', dex_stage / 'manifest.json']
    evidence_records = []
    for path in evidence_paths:
        boundary = build if path.is_relative_to(build) else root
        selected, data = regular(path, boundary, MAX_ARCHIVE)
        evidence_records.append(entry(selected, data))
    tool_records = toolchain_records(config['android_sdk'])
    sdk_path = Path(config['android_sdk'])
    ndk = sdk_path / 'ndk/29.0.14206865'
    llvm = ndk / 'toolchains/llvm/prebuilt/windows-x86_64/bin'
    expected_cache = {'CMAKE_COMMAND': sdk_path / 'cmake/4.1.2/bin/cmake.exe',
                      'CMAKE_MAKE_PROGRAM': sdk_path / 'cmake/4.1.2/bin/ninja.exe',
                      'CMAKE_TOOLCHAIN_FILE': ndk / 'build/cmake/android.toolchain.cmake',
                      'CMAKE_AR': llvm / 'llvm-ar.exe', 'CMAKE_RANLIB': llvm / 'llvm-ranlib.exe',
                      'CMAKE_LINKER': llvm / 'ld.lld.exe'}
    for val in graphs.values():
        for key, expected_path in expected_cache.items():
            matches = re.findall(r'(?m)^' + key + r':[^=\r\n]+=(.+?)\r?$', val['cache_text'])
            require(len(matches) == 1 and os.path.normcase(os.path.normpath(matches[0])) ==
                    os.path.normcase(os.path.normpath(str(expected_path))), 'toolchain_cache_mismatch')
    return {'scope': 'PRIVATE_STATIC_ARM64_INPUTS_NO_DEVICE', 'source_commit': ev['source_commit'],
            'abi': 'arm64-v8a', 'android_api_target': 27, 'config': config,
            'roles': artifacts, 'source_file_count': len(source_records),
            'dex_source_file_count': len(dex_manifest['source_files']),
            'sdk_files': {name: record(data) for name, data in sorted(sdk_actual.items())},
            'build_graphs': {key: {'graph': val['identity'], 'cache': val['cache'], 'rules': val['rules']} for key, val in graphs.items()},
            'helper_files': helper_records, 'evidence_files': evidence_records, 'toolchain_entries': tool_records,
            'jni_elf_filter': jni_elf,
            'limits': ['Hashes and local build graphs are not signed provenance or mapped-byte attestation.',
                       'Tool entries are frozen at profile creation, not independently attested at build time; the complete toolchain/sysroot is not covered.',
                       'A frozen profile is not device admission, Android runtime or performance qualification.']}


def snapshot_config(args):
    config = {key: relative(confined(getattr(args, key), build_root() if key in {
                'bundle', 'sdk', 'build_root', 'source_root', 'source_lock', 'dex_stage', 'postcheck'}
                else ROOT.resolve(strict=True)))
            for key in ('bundle', 'sdk', 'build_root', 'source_root', 'source_lock', 'dex_stage', 'dex_lock',
                        'candidate', 'candidate_evidence', 'postcheck')}
    config['android_sdk'] = str(args.android_sdk)
    return config


def freeze(args):
    config = snapshot_config(args)
    first = inspect(config)
    require(inspect(config) == first, 'inputs_changed_during_freeze')
    out = confined(args.out_dir, build_root(), exists=False)
    require(not os.path.lexists(out), 'output_exists')
    out.mkdir(parents=False, exist_ok=False)
    profile = {'schema_version': 1, 'state': 'FROZEN_LOCAL_INPUTS_NOT_DEVICE',
               'profile_id': secrets.token_hex(16), 'snapshot': first}
    data = (json.dumps(profile, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    require(len(data) <= MAX_PROFILE, 'profile_oversize')
    with (out / 'profile.json').open('xb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    require(inspect(config) == first, 'inputs_changed_after_profile_write')
    with (out / 'profile.sha256').open('x', encoding='ascii', newline='\n') as stream:
        stream.write(sha(data) + '\n'); stream.flush(); os.fsync(stream.fileno())
    require(safe.read_regular(out / 'profile.json', MAX_PROFILE) == data, 'profile_readback_changed')
    return {'state': 'FROZEN_LOCAL_INPUTS_NOT_DEVICE', 'role_count': len(ROLES), 'extra_count': len(EXTRAS),
            'profile_visibility': 'PRIVATE_BUILD_ONLY', 'device_executed': False, 'performance_qualified': False}


def validate(profile_path, expected_sha256):
    require(isinstance(expected_sha256, str) and SHA.fullmatch(expected_sha256), 'expected_profile_hash_invalid')
    path, data = regular(profile_path, build_root(), MAX_PROFILE)
    require(sha(data) == expected_sha256, 'profile_hash_mismatch')
    _, marker = regular(path.parent / 'profile.sha256', build_root(), 128)
    require(marker == (expected_sha256 + '\n').encode('ascii'), 'profile_completion_marker_invalid')
    profile = unique_json(data)
    require(isinstance(profile, dict) and set(profile) == {'schema_version', 'state', 'profile_id', 'snapshot'} and
            type(profile['schema_version']) is int and profile['schema_version'] == 1 and
            profile['state'] == 'FROZEN_LOCAL_INPUTS_NOT_DEVICE' and
            re.fullmatch(r'[0-9a-f]{32}', profile['profile_id']) and isinstance(profile['snapshot'], dict),
            'profile_schema_invalid')
    require(inspect(profile['snapshot'].get('config')) == profile['snapshot'], 'profile_input_drift')
    require(safe.read_regular(path, MAX_PROFILE) == data, 'profile_changed_during_validation')
    return profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    freezer = sub.add_parser('freeze')
    for name in ('bundle', 'sdk', 'build-root', 'source-root', 'source-lock', 'dex-stage', 'dex-lock',
                 'candidate', 'candidate-evidence', 'postcheck', 'out-dir'):
        freezer.add_argument('--' + name, type=Path, required=True)
    freezer.add_argument('--android-sdk', type=Path, required=True)
    checker = sub.add_parser('validate')
    checker.add_argument('--profile', type=Path, required=True)
    checker.add_argument('--profile-sha256', required=True)
    args = parser.parse_args()
    try:
        if args.action == 'freeze':
            output = freeze(args)
        else:
            validate(args.profile, args.profile_sha256)
            output = {'state': 'PASS_LOCAL_PROFILE_IDENTITY_ONLY', 'device_executed': False, 'performance_qualified': False}
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, zipfile.BadZipFile, struct.error) as error:
        print(json.dumps({'state': 'INCOMPLETE', 'reason': str(error) if isinstance(error, ValueError) and
                          re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', str(error)) else type(error).__name__,
                          'device_executed': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
