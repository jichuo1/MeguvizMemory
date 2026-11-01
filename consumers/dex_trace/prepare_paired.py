#!/usr/bin/env python3
"""Prepare a diagnostic-only paired_v2 source copy; never edit the consumer."""
import argparse
import hashlib
import json
from pathlib import Path

import prepare
import prepare_consumer

ROOT = prepare.ROOT
PAIRED_SHA = '1b156fc937b05a04e2c772c876ede193a05d0d1340a93d65301f7a202c37b577'
PAIRED_BYTES = 26574
COMPONENTS = ('consumer_trace.hpp', 'consumer_trace.cpp', 'trace_runtime.hpp', 'paired_consumer.cmake', 'paired/CMakeLists.txt')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def transform(data):
    prepare.require(len(data) == PAIRED_BYTES and digest(data) == PAIRED_SHA, 'paired_source_identity_mismatch')
    text = data.decode('utf-8').replace('\r\n', '\n')
    replacements = [
        ('#include <mgmem/mgmem.h>\n', '#include <mgmem/mgmem.h>\n#include "consumer_trace.hpp"\n'),
        ('        {\n            InputOwner input(mode, m.bytes);',
         '        #if defined(MGMEM_DEX_CONSUMER_TRACE)\n'
         '        mgmem_dex_trace::Recorder trace;\n'
         '        #endif\n'
         '        {\n            InputOwner input(mode, m.bytes);'),
        ('            const auto opened = dmg_snapshot_open_borrowed(engine.get(), static_cast<const uint8_t *>(input.address), m.bytes,\n'
         '                &lease, &options, &raw_snapshot, &error);',
         '            #if defined(MGMEM_DEX_CONSUMER_TRACE)\n'
         '            const auto opened = [&] {\n'
         '                mgmem_dex_trace::Scope capture(trace, input.address, m.bytes);\n'
         '                return dmg_snapshot_open_borrowed(engine.get(), static_cast<const uint8_t *>(input.address), m.bytes,\n'
         '                    &lease, &options, &raw_snapshot, &error);\n'
         '            }();\n'
         '            #else\n'
         '            const auto opened = dmg_snapshot_open_borrowed(engine.get(), static_cast<const uint8_t *>(input.address), m.bytes,\n'
         '                &lease, &options, &raw_snapshot, &error);\n'
         '            #endif'),
        ('        m.success = true; print(m); return 0;',
         '        #if defined(MGMEM_DEX_CONSUMER_TRACE)\n'
         '        require(trace.complete(), "trace_incomplete");\n'
         '        #endif\n'
         '        std::cout << "MGMEM_CORE_MODULE_HEX " << mgmem_dex_trace::core_module_path_hex() << \'\\n\';\n'
         '        m.success = true; print(m);\n'
         '        #if defined(MGMEM_DEX_CONSUMER_TRACE)\n'
         '        if (!trace.emit(std::cout)) return 1;\n'
         '        #endif\n'
         '        return std::cout ? 0 : 1;'),
    ]
    for before, after in replacements:
        prepare.require(text.count(before) == 1, 'paired_patch_anchor_mismatch')
        text = text.replace(before, after, 1)
    return text.encode('utf-8')


def create(source, out):
    source = prepare_consumer.confined(source, ROOT, must_exist=True)
    out = prepare_consumer.confined(out, prepare_consumer.build_root())
    prepare.require(source.is_file() and not out.exists(), 'invalid_paths')
    data = prepare_consumer.read_regular(source, prepare_consumer.MAX_FILE)
    patched = transform(data)
    components = {}
    for name in COMPONENTS:
        path = Path(__file__).resolve().parent / name
        components[name] = digest(prepare_consumer.read_regular(path, prepare_consumer.MAX_FILE))
    out.mkdir(parents=True, exist_ok=False)
    with (out / 'paired_diagnostic.cpp').open('xb') as stream:
        stream.write(patched)
    prepare.require(prepare_consumer.read_regular(source, prepare_consumer.MAX_FILE) == data, 'paired_source_changed')
    manifest = {'state': 'PREPARED_NOT_BUILT', 'scope': 'PAIRED_CONSUMER_METADATA_READER_ONLY',
                'source_sha256': PAIRED_SHA, 'source_bytes': PAIRED_BYTES,
                'patched_sha256': digest(patched), 'components_sha256': components,
                'limits': ['The diagnostic source includes instrumentation and is not a performance binary.',
                           'The module path identifies a file, not mapped bytes or signed provenance.']}
    with (out / 'manifest.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(manifest, stream, indent=2); stream.write('\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paired-source', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = create(args.paired_source, args.out_dir)
        print(json.dumps({'state': result['state'], 'scope': result['scope']}))
    except (OSError, ValueError, UnicodeError) as error:
        print(json.dumps({'state': 'INCOMPLETE', 'reason': type(error).__name__}))
        raise SystemExit(2)
