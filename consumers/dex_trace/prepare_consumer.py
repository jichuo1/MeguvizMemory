#!/usr/bin/env python3
"""Prepare locked, isolated Dex source trees for a reader-only consumer trace.

Only files covered by the supplied DexMeguviz source lock are copied. The
source tree and lock remain read-only; outputs are private build inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

import prepare as reader_prepare

ROOT = reader_prepare.ROOT
MAX_LOCK = 8 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
SCOPE_DIRS = {'src', 'include', 'third_party', 'cmake', 'cli'}
SECRET_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.keystore', '.jks'}
TRACE_HEADER = 'src/format/trace_checked_leb128.hpp'
READER = 'src/format/reader.cpp'
HEADER = 'src/format/reader.hpp'
LEB = 'src/format/checked_leb128.hpp'
SHA = re.compile(r'^[0-9a-f]{64}$')


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_object(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('nonfinite_json_number')
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def is_link(st):
    # Windows junctions are reparse points even when is_symlink() is false.
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)


def checked_path(root, value):
    require(isinstance(value, str) and value and '\\' not in value and ':' not in value and
            '\x00' not in value, 'unsafe_source_path')
    parts = value.split('/')
    require(all(part not in ('', '.', '..') for part in parts) and
            not PurePosixPath(value).is_absolute(), 'unsafe_source_path')
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'COM{i}' for i in range(1, 10)} | {f'LPT{i}' for i in range(1, 10)}
    require(all(not part.endswith(('.', ' ')) and part.split('.', 1)[0].upper() not in reserved and
                all(ord(char) >= 32 for char in part) for part in parts), 'unsafe_source_path')
    return root.joinpath(*parts)


def confined(path, boundary, must_exist=False):
    """Resolve lexical dot segments without silently following link ancestors."""
    boundary = Path(boundary).resolve(strict=True)
    path = Path(os.path.abspath(path))
    require(path.is_relative_to(boundary) and path != boundary, 'invalid_paths')
    current = boundary
    for part in path.relative_to(boundary).parts:
        current = current / part
        if os.path.lexists(current):
            st = current.lstat()
            require(not is_link(st), 'path_link_or_junction')
            if current != path: require(stat.S_ISDIR(st.st_mode), 'path_parent_not_directory')
        elif must_exist:
            raise ValueError('path_missing')
    return path


def build_root():
    path = ROOT / 'build'
    require(os.path.lexists(path), 'build_root_missing')
    st = path.lstat()
    require(not is_link(st) and stat.S_ISDIR(st.st_mode), 'build_root_link_or_invalid')
    resolved = path.resolve(strict=True)
    require(resolved.parent == ROOT, 'build_root_outside_workspace')
    return resolved


def read_regular(path, limit):
    before = path.lstat()
    require(not is_link(before) and stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
            0 <= before.st_size <= limit, 'source_file_invalid')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1 and
                (opened.st_dev, opened.st_ino, opened.st_size) ==
                (before.st_dev, before.st_ino, before.st_size), 'source_file_changed')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    require(not is_link(current) and len(data) == before.st_size and len(data) <= limit and
            signature(after) == signature(before) == signature(current), 'source_file_changed')
    return data


def lock_records(raw):
    require(len(raw) <= MAX_LOCK, 'source_lock_oversize')
    lock = json_object(raw.decode('utf-8-sig'))
    require(isinstance(lock, dict) and lock.get('schema_version') == 1 and
            lock.get('scope') == 'SCOPED_SOURCE_TREE_LOCK' and
            lock.get('external_parser_source') == 'LOCKED', 'source_lock_scope_invalid')
    records = lock.get('files')
    require(isinstance(records, list) and len(records) == lock.get('file_count') and records,
            'source_lock_records_invalid')
    seen = set()
    for entry in records:
        require(isinstance(entry, dict) and set(entry) == {'root', 'path', 'bytes', 'sha256'} and
                isinstance(entry['root'], str) and entry['root'] in ('MeguvizMemory', 'DexMeguviz') and
                type(entry['bytes']) is int and 0 <= entry['bytes'] <= MAX_FILE and
                isinstance(entry['sha256'], str) and SHA.fullmatch(entry['sha256']), 'source_lock_record_invalid')
        checked_path(Path('.'), entry['path'])
        key = (entry['root'].casefold(), entry['path'].casefold())
        require(key not in seen, 'source_lock_duplicate_path')
        seen.add(key)
    require(records == sorted(records, key=lambda e: (e['root'], e['path'])), 'source_lock_order_invalid')
    canonical = json.dumps(records, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')
    require(lock.get('source_set_sha256') == sha(canonical), 'source_lock_set_mismatch')
    selected = {e['path']: {'bytes': e['bytes'], 'sha256': e['sha256']}
                for e in records if e['root'] == 'DexMeguviz'}
    require(selected and {'CMakeLists.txt', READER, HEADER, LEB} <= set(selected) and
            sum(e['bytes'] for e in selected.values()) <= MAX_TOTAL, 'dex_source_lock_incomplete')
    require(all('/' not in name or name.split('/', 1)[0] in SCOPE_DIRS for name in selected),
            'dex_source_scope_invalid')
    require(all(Path(name).name.lower() != '.env' and not Path(name).name.lower().startswith('.env.') and
                Path(name).suffix.lower() not in SECRET_SUFFIXES for name in selected), 'dex_secret_path_rejected')
    require(all('/' in name or Path(name).suffix.lower() in {'.md', '.json', '.cmake', '.txt'} or
                name in {'CMakeLists.txt', 'LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES', '.gitignore', '.gitattributes'}
                for name in selected), 'dex_root_file_out_of_scope')
    for name, (digest, size, _) in reader_prepare.FILES.items():
        require(selected['src/format/' + name] == {'bytes': size, 'sha256': digest}, 'reader_lock_mismatch')
    require(TRACE_HEADER not in selected, 'trace_header_already_exists')
    return lock, selected


def inventory(root, allow_fixture_tree=False):
    """Enumerate regular files, rejecting symlinks/junctions rather than following them."""
    require(root.is_dir() and not is_link(root.lstat()), 'source_root_invalid')
    found = set()
    def onerror(error):
        raise error
    for current, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        current = Path(current)
        for name in dirs:
            st = (current / name).lstat()
            require(not is_link(st) and stat.S_ISDIR(st.st_mode), 'source_link_or_special')
        for name in files:
            path = current / name
            st = path.lstat()
            require(not is_link(st) and stat.S_ISREG(st.st_mode) and st.st_nlink == 1,
                    'source_link_or_special')
            relative = path.relative_to(root).as_posix()
            if allow_fixture_tree and relative.startswith('tests/'):
                continue
            found.add(relative)
    require(len(found) == len({name.casefold() for name in found}), 'source_case_collision')
    return found


def patches(source_bytes):
    inputs = {'reader.cpp': source_bytes[READER], 'reader.hpp': source_bytes[HEADER],
              'checked_leb128.hpp': source_bytes[LEB]}
    transformed, sites = reader_prepare.instrument(inputs)
    reader = transformed['reader.cpp'].decode('utf-8')
    checked = transformed['checked_leb128.hpp'].decode('utf-8')
    require(reader.count('#include "checked_leb128.hpp"') == 1 and
            reader.count('detail::read_uleb32(') == 1 and reader.count('detail::read_sleb32(') == 2 and
            checked.count('namespace dexmeguviz::detail {') == 1, 'patch_anchor_count_mismatch')
    reader = reader.replace('#include "checked_leb128.hpp"', '#include "trace_checked_leb128.hpp"')
    reader = reader.replace('detail::read_uleb32(', 'mgmem_trace_detail::read_uleb32(')
    reader = reader.replace('detail::read_sleb32(', 'mgmem_trace_detail::read_sleb32(')
    checked = checked.replace('namespace dexmeguviz::detail {', 'namespace dexmeguviz::mgmem_trace_detail {')
    require(len(sites) == 32 and {s['id'] for s in sites} == set(range(1, 32)) | {1001} and
            reader.count('mgmem_trace_detail::read_uleb32(') == 1 and
            reader.count('mgmem_trace_detail::read_sleb32(') == 2 and
            'namespace dexmeguviz::detail {' not in checked, 'patch_contract_invalid')
    return {READER: reader.encode('utf-8'), TRACE_HEADER: checked.encode('utf-8')}, sites


def record(data):
    return {'bytes': len(data), 'sha256': sha(data)}


def manifest_for(raw_lock, lock, selected, changed, sites):
    return {'schema_version': 1, 'state': 'PREPARED_NOT_BUILT',
            'scope': 'LOCKED_DEX_CONSUMER_READER_ONLY',
            'source_lock_sha256': sha(raw_lock), 'source_set_sha256': lock['source_set_sha256'],
            'source_files': selected, 'patched_files': {name: record(data) for name, data in changed.items()},
            'sites': sites,
            'limits': ['Only the locked build source subset is copied; unlisted test fixtures are excluded.',
                       'A source lock does not authenticate runtime images or prove performance.',
                       'Only metadata-reader source accesses are instrumented.']}


def verify_trees(out, selected):
    require(inventory(out / 'baseline') == set(selected) and
            inventory(out / 'traced') == set(selected) | {TRACE_HEADER}, 'prepared_file_set_mismatch')
    originals = {}
    for name, expected in selected.items():
        data = read_regular(checked_path(out / 'baseline', name), MAX_FILE)
        require(record(data) == expected, 'prepared_baseline_drift')
        originals[name] = data
    changed, sites = patches(originals)
    for name, original in originals.items():
        data = read_regular(checked_path(out / 'traced', name), MAX_FILE)
        require(data == changed.get(name, original), 'prepared_trace_drift')
    require(read_regular(checked_path(out / 'traced', TRACE_HEADER), MAX_FILE) == changed[TRACE_HEADER],
            'prepared_trace_drift')
    return changed, sites


def verify_prepared(out, lock_path):
    out = confined(out, build_root(), must_exist=True)
    lock_path = confined(lock_path, ROOT, must_exist=True)
    require({item.name for item in out.iterdir()} == {'baseline', 'traced', 'manifest.json'},
            'prepared_root_set_mismatch')
    raw = read_regular(lock_path, MAX_LOCK)
    lock, selected = lock_records(raw)
    changed, sites = verify_trees(out, selected)
    manifest = json_object(read_regular(out / 'manifest.json', MAX_LOCK).decode('utf-8'))
    require(manifest == manifest_for(raw, lock, selected, changed, sites), 'prepared_manifest_drift')
    require(read_regular(lock_path, MAX_LOCK) == raw, 'source_lock_changed_during_verify')
    return manifest


def prepare(source, lock_path, out):
    build = build_root()
    source = confined(source, build, must_exist=True)
    lock_path = confined(lock_path, ROOT, must_exist=True)
    out = confined(out, build)
    require(source.is_dir() and not os.path.lexists(out) and
            not out.is_relative_to(source) and not source.is_relative_to(out), 'invalid_paths')
    raw = read_regular(lock_path, MAX_LOCK)
    lock, selected = lock_records(raw)
    require(inventory(source, allow_fixture_tree=True) == set(selected), 'source_file_set_mismatch')
    source_bytes = {}
    for name, expected in selected.items():
        data = read_regular(checked_path(source, name), MAX_FILE)
        require(record(data) == expected, 'source_identity_mismatch')
        source_bytes[name] = data
    changed, sites = patches(source_bytes)
    out.mkdir(parents=True, exist_ok=False)
    for mode in ('baseline', 'traced'):
        target = out / mode
        target.mkdir()
        files = dict(source_bytes)
        if mode == 'traced': files.update(changed)
        for name, data in files.items():
            path = checked_path(target, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream: stream.write(data)
    # Re-read all source bytes and the lock after copying; a failure leaves an
    # incomplete private directory and never writes a success manifest.
    require(inventory(source, allow_fixture_tree=True) == set(selected) and
            read_regular(lock_path, MAX_LOCK) == raw, 'source_changed_during_prepare')
    for name, expected in selected.items():
        require(record(read_regular(checked_path(source, name), MAX_FILE)) == expected,
                'source_changed_during_prepare')
    require({item.name for item in out.iterdir()} == {'baseline', 'traced'}, 'prepared_root_set_mismatch')
    verified_changes, verified_sites = verify_trees(out, selected)
    require(verified_changes == changed and verified_sites == sites and read_regular(lock_path, MAX_LOCK) == raw,
            'prepared_content_changed')
    manifest = manifest_for(raw, lock, selected, changed, sites)
    # Exclusive creation cannot replace another file. Consumers must still
    # require a successful process exit and call verify_prepared afterwards.
    with (out / 'manifest.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dex-source', type=Path, required=True)
    parser.add_argument('--source-lock', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.dex_source, args.source_lock, args.out_dir)
        print(json.dumps({'state': result['state'], 'source_files': len(result['source_files']),
                          'trace_sites': len(result['sites'])}))
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
        print(json.dumps({'state': 'INCOMPLETE', 'reason': type(error).__name__}))
        raise SystemExit(2)
