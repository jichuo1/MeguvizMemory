#!/usr/bin/env python3
"""Create a V2-only paired consumer copy; never edit the ordinary consumer."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
DEX_TRACE = ROOT / 'consumers' / 'dex_trace'
sys.path.insert(0, str(DEX_TRACE))
import prepare
import prepare_consumer

PAIRED_SHA = '1b156fc937b05a04e2c772c876ede193a05d0d1340a93d65301f7a202c37b577'
PAIRED_BYTES = 26574
COMPONENTS = (
    'consumers/dex_trace/stream_v2/capture/stream_trace.hpp',
    'consumers/dex_trace/stream_v2/capture/stream_trace.cpp',
    'consumers/dex_trace/stream_v2/capture/paired/CMakeLists.txt',
    'consumers/dex_trace/stream_v2/capture/prepare_stream_paired.py',
    'consumers/dex_trace/trace_runtime.hpp',
    'consumers/image/trace_sha256.hpp',
)
LIMITS = ['The generated binary is diagnostic-only and not a performance binary.',
          'A terminal frame requires an independently checked process exit and complete final report.']


def digest(data):
    return hashlib.sha256(data).hexdigest()


def component_identities():
    result = {}
    for name in COMPONENTS:
        path = prepare_consumer.confined(ROOT / name, ROOT, must_exist=True)
        result[name] = digest(prepare_consumer.read_regular(path, prepare_consumer.MAX_FILE))
    return result


def transform(data):
    prepare.require(len(data) == PAIRED_BYTES and digest(data) == PAIRED_SHA, 'paired_source_identity_mismatch')
    text = data.decode('utf-8').replace('\r\n', '\n')
    replacements = [
        ('#include <mgmem/mgmem.h>\n', '#include <mgmem/mgmem.h>\n#include "stream_trace.hpp"\n'),
        ('int main(int argc, char **argv) {\n',
         'int main(int argc, char **argv) {\n'
         '#if defined(MGMEM_DEX_CONSUMER_TRACE_STREAM_V2)\n'
         '    if (!mgmem_dex_trace_v2::configure_stdout_binary()) return 1;\n'
         '#endif\n'),
        ('        {\n            InputOwner input(mode, m.bytes);',
         '        #if defined(MGMEM_DEX_CONSUMER_TRACE_STREAM_V2)\n'
         '        mgmem_dex_trace_v2::Recorder trace(std::cout);\n'
         '        #endif\n'
         '        {\n            InputOwner input(mode, m.bytes);'),
        ('            const auto opened = dmg_snapshot_open_borrowed(engine.get(), static_cast<const uint8_t *>(input.address), m.bytes,\n'
         '                &lease, &options, &raw_snapshot, &error);',
         '            #if defined(MGMEM_DEX_CONSUMER_TRACE_STREAM_V2)\n'
         '            const auto opened = [&] {\n'
         '                try {\n'
         '                    mgmem_dex_trace_v2::Scope capture(trace, input.address, m.bytes);\n'
         '                    return dmg_snapshot_open_borrowed(engine.get(), static_cast<const uint8_t *>(input.address), m.bytes,\n'
         '                        &lease, &options, &raw_snapshot, &error);\n'
         '                } catch (...) {\n'
         '                    if (!trace.complete()) trace.report_failure(std::cerr);\n'
         '                    throw;\n'
         '                }\n'
         '            }();\n'
         '            #else\n'
         '            const auto opened = dmg_snapshot_open_borrowed(engine.get(), static_cast<const uint8_t *>(input.address), m.bytes,\n'
         '                &lease, &options, &raw_snapshot, &error);\n'
         '            #endif'),
        ('        m.success = true; print(m); return 0;',
         '        #if defined(MGMEM_DEX_CONSUMER_TRACE_STREAM_V2)\n'
         '        if (!trace.finish()) { trace.report_failure(std::cerr); require(false, "trace_incomplete"); }\n'
         '        #endif\n'
         '        std::cout << "MGMEM_CORE_MODULE_HEX " << mgmem_dex_trace_v2::core_module_path_hex() << \'\\n\';\n'
         '        m.success = true; print(m); std::cout.flush();\n'
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
    original = prepare_consumer.read_regular(source, prepare_consumer.MAX_FILE)
    patched = transform(original)
    components = component_identities()
    out.mkdir(parents=True, exist_ok=False)
    with (out / 'paired_stream.cpp').open('xb') as stream:
        stream.write(patched)
    prepare.require(prepare_consumer.read_regular(source, prepare_consumer.MAX_FILE) == original, 'paired_source_changed')
    manifest = {'schema_version': 1, 'state': 'PREPARED_NOT_BUILT',
                'scope': 'PAIRED_CONSUMER_METADATA_READER_STREAM_V2',
                'source_sha256': PAIRED_SHA, 'source_bytes': PAIRED_BYTES,
                'patched_sha256': digest(patched), 'components_sha256': components,
                'limits': LIMITS}
    with (out / 'manifest.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(manifest, stream, indent=2); stream.write('\n')
    prepare.require(verify_prepared(out, source) == manifest, 'paired_stage_drift')
    return manifest


def verify_prepared(stage, source):
    """Recreate the transform; a forged manifest cannot hide altered code."""
    source = prepare_consumer.confined(source, ROOT, must_exist=True)
    stage = prepare_consumer.confined(stage, prepare_consumer.build_root(), must_exist=True)
    prepare.require(source.is_file() and stage.is_dir(), 'invalid_paths')
    prepare.require({item.name for item in stage.iterdir()} == {'paired_stream.cpp', 'manifest.json'},
                    'paired_stage_set_mismatch')
    original = prepare_consumer.read_regular(source, prepare_consumer.MAX_FILE)
    patched = transform(original)
    actual = prepare_consumer.read_regular(stage / 'paired_stream.cpp', prepare_consumer.MAX_FILE)
    raw = prepare_consumer.read_regular(stage / 'manifest.json', 1024 * 1024)
    manifest = prepare_consumer.json_object(raw.decode('utf-8'))
    components = component_identities()
    prepare.require(isinstance(manifest, dict) and set(manifest) == {
        'schema_version', 'state', 'scope', 'source_sha256', 'source_bytes', 'patched_sha256',
        'components_sha256', 'limits'} and
        type(manifest['schema_version']) is int and manifest['schema_version'] == 1 and manifest['state'] == 'PREPARED_NOT_BUILT' and
        manifest['scope'] == 'PAIRED_CONSUMER_METADATA_READER_STREAM_V2' and
        manifest['source_sha256'] == PAIRED_SHA and manifest['source_bytes'] == PAIRED_BYTES and
        manifest['patched_sha256'] == digest(patched) and manifest['components_sha256'] == components and
        manifest['limits'] == LIMITS and actual == patched, 'paired_stage_drift')
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
