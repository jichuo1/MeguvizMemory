#!/usr/bin/env python3
"""Exercise real reader, malformed-input and event-limit paths with private outputs."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import capture
import prepare


def main(args):
    out = args.out_dir.resolve()
    build = (prepare.ROOT / 'build').resolve()
    prepare.require(out.is_relative_to(build) and out != build and not out.exists(), 'invalid_output_directory')
    source = args.input.resolve(strict=True)
    prepare.require(source.is_relative_to(prepare.ROOT) and source.is_file(), 'invalid_input')
    data = capture.read_input(source)
    prepare.require(len(data) >= 8 and data[:4] == b'dex\n', 'expected_dex_input')
    out.mkdir(parents=True, exist_ok=False)
    def options(path, directory, maximum):
        return argparse.Namespace(input=path, baseline=args.baseline, instrumented=args.instrumented,
            manifest=args.manifest, out_dir=out / directory, max_events=maximum, timeout_seconds=60)
    positive = capture.capture(options(source, 'valid', 1000000))
    prepare.require(positive['reader_status'] == 'OK' and positive['events'] > 1 and
                    positive['input_bytes'] == len(data) and
                    positive['input_sha256'] == hashlib.sha256(data).hexdigest(), 'valid_reader_did_not_succeed')
    malformed = out / 'malformed.dex'
    with malformed.open('xb') as stream: stream.write(b'\0' + data[1:])
    negative = capture.capture(options(malformed, 'invalid', 1000000))
    prepare.require(negative['reader_status'].startswith('ERROR_'), 'invalid_reader_did_not_reject')
    try: capture.capture(options(source, 'overflow', 1))
    except ValueError as error:
        prepare.require(str(error) == 'instrumented_process_incomplete', 'unexpected_overflow_failure')
    else: raise ValueError('event_limit_not_enforced')
    prepare.require(not (out / 'overflow' / 'receipt.json').exists(), 'overflow_has_pass_receipt')
    result = {'state': 'PASS_SCOPED_HOST_READER_SMOKE', 'positive_events': positive['events'],
              'positive_site_count': len(positive['site_hits']), 'invalid_events': negative['events'],
              'invalid_reader_status': negative['reader_status'], 'limit': 'INCOMPLETE_NO_PASS_RECEIPT',
              'input_sha256': positive['input_sha256'], 'input_bytes': positive['input_bytes'],
              'scope': 'METADATA_READER_ONLY_NOT_DEX_QUERY_OR_PERFORMANCE'}
    with (out / 'smoke.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, indent=2); stream.write('\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'baseline', 'instrumented', 'manifest', 'out-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    try:
        value = main(parser.parse_args())
        print(json.dumps({k: value[k] for k in ('state', 'positive_events', 'positive_site_count', 'invalid_reader_status', 'limit')}))
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({'state': 'INCOMPLETE', 'reason': type(error).__name__}))
        raise SystemExit(2)
