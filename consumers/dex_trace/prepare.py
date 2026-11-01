#!/usr/bin/env python3
"""Create a private, hash-locked metadata-reader instrumentation copy."""
import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
FILES = {
    'reader.cpp': ('40b7e4edeefa74b512ff69032b1af45763dddd7a443266f85ebd54cb10cf177e', 61142, 31),
    'reader.hpp': ('d1a943ab76ceb6f142d5aded737faf7b9d2642989a8d729e9400a761cb8cfaba', 5204, 0),
    'checked_leb128.hpp': ('d1130c3c888fa6850d2fcf2ba4576f9503011c2baf81151b81d09da954cdab20', 1725, 1),
}
READ = re.compile(r'\bbytes\[([^\[\]]+)\]')


def require(value, reason):
    if not value:
        raise ValueError(reason)


def instrument(files):
    sites = []
    transformed = {}
    for name, data in files.items():
        text = data.decode('utf-8').replace('\r\n', '\n')
        matches = list(READ.finditer(text))
        require(len(matches) == FILES[name][2], 'reader_site_count_mismatch')
        ids = iter(range(1, 32) if name == 'reader.cpp' else (1001,))
        def replace(match):
            site = next(ids)
            sites.append({'id': site, 'file': name, 'line': text.count('\n', 0, match.start()) + 1})
            return 'mgmem_dex_trace::load(bytes, ' + match.group(1) + ', ' + str(site) + ')'
        patched = READ.sub(replace, text)
        require(not READ.search(patched), 'reader_site_untransformed')
        if matches:
            patched = '#include "trace_runtime.hpp"\n' + patched
        transformed[name] = patched.encode('utf-8')
    require(len(sites) == 32 and len({item['id'] for item in sites}) == 32, 'site_manifest_mismatch')
    return transformed, sites


def prepare(source, out):
    source = Path(source).resolve(strict=True)
    out = Path(out).resolve()
    build = (ROOT / 'build').resolve()
    require(source.is_dir() and out.is_relative_to(build) and out != build and not out.exists(), 'invalid_paths')
    files = {}
    for name, (expected, length, _) in FILES.items():
        path = source / 'src' / 'format' / name
        require(path.is_file() and not path.is_symlink(), 'source_missing_or_link')
        data = path.read_bytes()
        require(len(data) == length and hashlib.sha256(data).hexdigest() == expected, 'source_identity_mismatch')
        files[name] = data
    transformed, sites = instrument(files)
    out.mkdir(parents=True, exist_ok=False)
    for mode in ('baseline', 'instrumented'):
        target = out / mode
        target.mkdir()
        for name in FILES:
            with (target / name).open('xb') as stream:
                stream.write(files[name] if mode == 'baseline' else transformed[name])
    for name, (expected, length, _) in FILES.items():
        current = (source / 'src' / 'format' / name).read_bytes()
        require(len(current) == length and hashlib.sha256(current).hexdigest() == expected, 'source_changed_during_prepare')
    manifest = {'state': 'PREPARED_NOT_BUILT', 'scope': 'DEX_METADATA_READER_ONLY',
                'sources': {name: {'sha256': sha, 'bytes': size} for name, (sha, size, _) in FILES.items()},
                'sites': sites, 'instrumented_sha256': {name: hashlib.sha256(data).hexdigest()
                                                        for name, data in transformed.items()}}
    with (out / 'manifest.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(manifest, stream, indent=2); stream.write('\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dex-source', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.dex_source, args.out_dir)
        print(json.dumps({'state': result['state'], 'site_count': len(result['sites'])}))
    except (OSError, ValueError, UnicodeError) as error:
        print(json.dumps({'state': 'INCOMPLETE', 'reason': type(error).__name__}))
        raise SystemExit(2)
