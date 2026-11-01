#!/usr/bin/env python3
"""Private four-variant Dex consumer reader comparison; no performance claim."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import time
import zipfile

import capture
import prepare
import prepare_consumer
import prepare_paired

ROOT = prepare.ROOT
VARIANTS = ('D-C0', 'D-Z0', 'D-C1', 'D-C2')
CASES = ('normal', 'cross-thread', 'invalid-lease', 'borrow-budget', 'corrupt-input')
EXPECTED_STATUS = {'normal': 0, 'cross-thread': 0, 'invalid-lease': 1, 'borrow-budget': 4, 'corrupt-input': 2}
MAX_SDK_MEMBER = 64 * 1024 * 1024
MAX_SDK_TOTAL = 256 * 1024 * 1024
UNSIGNED_FIELDS = ('input_bytes', 'method_count', 'result_checksum', 'logical_requested_bytes',
                   'unique_owned_backing_peak_bytes', 'external_source_capacity_bytes', 'borrowed_view_peak_bytes',
                   'source_plus_input_unique_capacity_bytes', 'retained_before_collect_bytes', 'reserved_final_bytes',
                   'arena_used_bytes', 'input_backing_padding_bytes', 'provider_published_count',
                   'parser_peak_owned_bytes', 'input_budget_bytes', 'parser_budget_bytes', 'callback_retains',
                   'callback_releases_including_caller', 'closed_backing_bytes')
STABLE = ('schema_version', 'workload', 'variant', 'case', 'success', 'diagnostic', 'failure_reason',
          'source_sha256', 'input_bytes', 'method_count', 'result_checksum', 'logical_requested_bytes',
          'unique_owned_backing_peak_bytes', 'external_source_capacity_bytes', 'borrowed_view_peak_bytes',
          'source_plus_input_unique_capacity_bytes', 'retained_before_collect_bytes', 'reserved_final_bytes',
          'arena_used_bytes', 'input_backing_padding_bytes', 'provider_published_count', 'parser_peak_owned_bytes',
          'input_budget_bytes', 'parser_budget_bytes', 'callback_retains', 'callback_releases_including_caller',
          'expected_status', 'observed_status', 'lease_final_correct', 'snapshot_released_before_last_read',
          'source_final_released', 'final_release_on_other_thread', 'closed_backing_bytes', 'final_resource_state',
          'resource_scope', 'product_scope')
BINDING_NAME = 'paired-capture-binding.json'
BINDING_SCOPE = 'SNAPSHOT_OPEN_CALLER_THREAD_METADATA_READER_ONLY'
TRACE_FAILURE_PREFIX = 'MGMEM_DEX_TRACE_INCOMPLETE '
TRACE_FAILURE_REASONS = frozenset({'EVENT_LIMIT', 'SPAN_MISMATCH', 'OFFSET_OUT_OF_RANGE',
                                   'SITE_INVALID', 'SCOPE_INVALID', 'OUTPUT_FAILURE'})


class TraceIncomplete(ValueError):
    def __init__(self, report):
        super().__init__('trace_reported_incomplete')
        self.report = report


def parse_trace_failure(stderr, expected_limit):
    """Accept one fixed diagnostic only; never reflect arbitrary child stderr."""
    if not isinstance(stderr, bytes) or len(stderr) > capture.MAX_STDERR:
        return None
    try:
        text = stderr.decode('ascii', 'strict').replace('\r\n', '\n')
        if not text.endswith('\n') or len(text.splitlines()) != 1 or not text.startswith(TRACE_FAILURE_PREFIX):
            return None
        value = unique_json(text[len(TRACE_FAILURE_PREFIX):-1])
        fields = {'reason', 'attempted_lower_bound', 'attempted_capped', 'recorded', 'event_limit'}
        if not isinstance(value, dict) or set(value) != fields:
            return None
        limit, attempted, recorded = value['event_limit'], value['attempted_lower_bound'], value['recorded']
        if (value['reason'] not in TRACE_FAILURE_REASONS or type(limit) is not int or
                type(expected_limit) is not int or limit != expected_limit or not 1 <= limit <= 1000000 or
                type(attempted) is not int or not 0 <= attempted <= limit + 1 or
                type(recorded) is not int or not 0 <= recorded <= min(attempted, limit) or
                type(value['attempted_capped']) is not bool or
                (value['attempted_capped'] and attempted != limit + 1)):
            return None
        if value['reason'] == 'EVENT_LIMIT' and (recorded != limit or attempted <= recorded):
            return None
        if value['reason'] in ('SPAN_MISMATCH', 'OFFSET_OUT_OF_RANGE', 'SITE_INVALID') and (
                attempted == 0 or recorded >= attempted):
            return None
        if value['reason'] == 'OUTPUT_FAILURE' and (value['attempted_capped'] or attempted != recorded):
            return None
        return value
    except (UnicodeError, ValueError, TypeError, KeyError):
        return None


def traced_failure(code, stderr, expected_limit):
    # The generated diagnostic's classified failure path returns exactly 1.
    # A signal or different exit is not independently classified.
    return parse_trace_failure(stderr, expected_limit) if type(code) is int and code == 1 else None
EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()


def unique_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            prepare.require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


def safe_path(name):
    path = PurePosixPath(name)
    prepare_consumer.checked_path(Path('.'), name)
    return path


def verify_sdk(archive, evidence, sdk, max_total=MAX_SDK_TOTAL):
    """Read back installed SDK files from the identified engineering archive."""
    metadata = unique_json(prepare_consumer.read_regular(evidence, 1024 * 1024).decode('utf-8-sig'))
    prepare.require(isinstance(metadata, dict), 'candidate_evidence_invalid')
    archive_size, archive_hash = capture.input_identity(archive, maximum=256 * 1024 * 1024)
    prepare.require(archive_size <= 256 * 1024 * 1024 and
                    re.fullmatch(r'[0-9a-f]{40}', metadata.get('source_commit', '')) is not None,
                    'candidate_evidence_invalid')
    if zipfile.is_zipfile(archive):
        prepare.require(metadata.get('state') == 'PASS_SCOPED_ENGINEERING_CANDIDATES', 'candidate_evidence_invalid')
        candidate = metadata.get('windows', metadata.get('windows_host', {}))
        expected_hash, expected_size = candidate.get('sha256'), candidate.get('bytes')
    else:
        prepare.require(metadata.get('state') == 'PASS_SCOPED_LINUX_HOST_ENGINEERING_CANDIDATE', 'candidate_evidence_invalid')
        expected_hash, expected_size = metadata.get('candidate_sha256'), metadata.get('candidate_bytes')
    prepare.require(isinstance(expected_hash, str) and re.fullmatch(r'[0-9a-f]{64}', expected_hash) and
                    type(expected_size) is int and archive_size == expected_size and archive_hash == expected_hash,
                    'candidate_archive_mismatch')
    entries = {}
    total = 0
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            prepare.require(len(names) == len(set(names)) and len(names) <= 512, 'candidate_members_invalid')
            for item in z.infolist():
                if not item.filename.startswith('sdk/') or item.is_dir(): continue
                path = safe_path(item.filename)
                total += item.file_size
                prepare.require(0 <= item.file_size <= MAX_SDK_MEMBER and total <= max_total and
                                not (item.external_attr >> 16 & 0o170000) == 0o120000,
                                'candidate_member_invalid')
                with z.open(item) as stream: data = stream.read(item.file_size + 1)
                prepare.require(len(data) == item.file_size, 'candidate_member_invalid')
                entries[path.relative_to('sdk').as_posix()] = ('file', data)
    else:
        with tarfile.open(archive, 'r:gz') as t:
            members = t.getmembers()
            prepare.require(len(members) <= 512 and len({m.name for m in members}) == len(members), 'candidate_members_invalid')
            for item in members:
                if not item.name.startswith('sdk/') or item.isdir(): continue
                path = safe_path(item.name).relative_to('sdk').as_posix()
                if item.issym():
                    prepare.require('/' not in item.linkname and item.linkname not in ('', '.', '..'), 'candidate_link_invalid')
                    safe_path(item.linkname)
                    entries[path] = ('link', item.linkname)
                else:
                    total += item.size
                    prepare.require(item.isfile() and 0 <= item.size <= MAX_SDK_MEMBER and total <= max_total,
                                    'candidate_member_invalid')
                    stream = t.extractfile(item)
                    prepare.require(stream is not None, 'candidate_member_unreadable')
                    data = stream.read(item.size + 1)
                    prepare.require(len(data) == item.size, 'candidate_member_invalid')
                    entries[path] = ('file', data)
    prepare.require(entries and len({x.casefold() for x in entries}) == len(entries), 'candidate_members_invalid')
    prepare.require(not prepare_consumer.is_link(sdk.lstat()), 'installed_sdk_link_or_junction')
    actual = set()
    def onerror(error): raise error
    for current, dirs, files in os.walk(sdk, followlinks=False, onerror=onerror):
        current = Path(current)
        for name in dirs:
            value = (current / name).lstat()
            prepare.require(not prepare_consumer.is_link(value) and stat.S_ISDIR(value.st_mode), 'installed_sdk_link_or_junction')
        for name in files:
            actual.add((current / name).relative_to(sdk).as_posix())
    prepare.require(actual == set(entries), 'installed_sdk_file_set_mismatch')
    for name, (kind, value) in entries.items():
        path = sdk.joinpath(*safe_path(name).parts)
        if kind == 'link':
            prepare.require(path.is_symlink() and os.readlink(path) == value and
                            path.resolve(strict=True).is_relative_to(sdk.resolve()), 'installed_sdk_link_mismatch')
        else:
            local = prepare_consumer.read_regular(path, MAX_SDK_MEMBER)
            prepare.require(local == value, 'installed_sdk_file_mismatch')
    return metadata['source_commit'], expected_hash


def verify_paired(stage):
    prepare.require({item.name for item in stage.iterdir()} == {'manifest.json', 'paired_diagnostic.cpp'},
                    'paired_stage_file_set_mismatch')
    manifest = unique_json(prepare_consumer.read_regular(stage / 'manifest.json', prepare_consumer.MAX_LOCK).decode('utf-8'))
    prepare.require(isinstance(manifest, dict) and set(manifest) == {
        'state', 'scope', 'source_sha256', 'source_bytes', 'patched_sha256', 'components_sha256', 'limits'},
        'paired_stage_manifest_invalid')
    source = ROOT / 'consumers/dex/paired_v2.cpp'
    original = prepare_consumer.read_regular(source, prepare_consumer.MAX_FILE)
    transformed = prepare_paired.transform(original)
    expected_components = {name: hashlib.sha256(prepare_consumer.read_regular(ROOT / 'consumers/dex_trace' / name,
                                                                             prepare_consumer.MAX_FILE)).hexdigest()
                           for name in prepare_paired.COMPONENTS}
    prepare.require(manifest.get('state') == 'PREPARED_NOT_BUILT' and
                    manifest.get('scope') == 'PAIRED_CONSUMER_METADATA_READER_ONLY' and
                    manifest.get('source_sha256') == prepare_paired.PAIRED_SHA and
                    manifest.get('source_bytes') == prepare_paired.PAIRED_BYTES and
                    manifest.get('patched_sha256') == hashlib.sha256(transformed).hexdigest() and
                    manifest.get('components_sha256') == expected_components and
                    prepare_consumer.read_regular(stage / 'paired_diagnostic.cpp', prepare_consumer.MAX_FILE) == transformed,
                    'paired_stage_drift')
    return manifest


def cache_values(build, mode, dex_stage, paired_stage, sdk):
    text = (build / 'CMakeCache.txt').read_text(encoding='utf-8')
    expected = {'DEX_TRACE_MODE': mode, 'DEX_TRACE_STAGE': str(dex_stage),
                'PAIRED_TRACE_STAGE': str(paired_stage),
                'MeguvizMemory_DIR': str(sdk / 'lib/cmake/MeguvizMemory'),
                'CMAKE_HOME_DIRECTORY': str(ROOT / 'consumers/dex_trace/paired')}
    for key, value in expected.items():
        matches = re.findall(r'(?m)^' + re.escape(key) + r':[^=]+=(.*)$', text)
        prepare.require(len(matches) == 1 and (matches[0].strip() == value if key == 'DEX_TRACE_MODE' else
                        Path(matches[0].strip()).resolve() == Path(value).resolve()), 'build_cache_mismatch')


def parse_output(data, traced, variant, case, size, file_hash, consumed_hash, core, limit):
    text = data.decode('utf-8', 'strict').replace('\r\n', '\n')
    lines = text.splitlines()
    prepare.require(text.endswith('\n') and len(lines) >= 2 and all(lines), 'output_invalid')
    module = re.fullmatch(r'MGMEM_CORE_MODULE_HEX ([0-9a-f]+)', lines[0])
    prepare.require(module and len(module.group(1)) % 2 == 0, 'module_identity_missing')
    module_path = Path(bytes.fromhex(module.group(1)).decode('utf-8', 'strict')).resolve(strict=True)
    prepare.require(module_path == core.resolve(strict=True) and capture.sha(module_path) == capture.sha(core),
                    'loaded_core_identity_mismatch')
    value = unique_json(lines[1])
    prepare.require(isinstance(value, dict) and all(key in value for key in STABLE) and
                    type(value['schema_version']) is int and value['schema_version'] == 2 and
                    value['workload'] == 'dex_source_strategy_complete_owner_v2' and
                    value['variant'] == variant and value['case'] == case and value['success'] is True and
                    value['diagnostic'] is False and value['failure_reason'] is None and
                    all(type(value[k]) is int and 0 <= value[k] <= 2**64 - 1 for k in UNSIGNED_FIELDS) and
                    value['input_bytes'] == value['logical_requested_bytes'] == size and
                    value['input_budget_bytes'] == 32 * 1024 * 1024 and value['parser_budget_bytes'] == 256 * 1024 * 1024 and
                    value['resource_scope'] == 'source_capacity_plus_unique_input_backing_parser_separate_metadata_RSS_unknown' and
                    value['product_scope'] == 'independent_DexMeguviz_not_BIL_DexKit' and
                    type(value['expected_status']) is int and type(value['observed_status']) is int and
                    value['expected_status'] == value['observed_status'] == EXPECTED_STATUS[case] and
                    value['lease_final_correct'] is True and
                    value['source_final_released'] is True and value['final_resource_state'] == 'CLOSED_ZERO' and
                    value['closed_backing_bytes'] == value['reserved_final_bytes'] == 0 and
                    type(value['snapshot_released_before_last_read']) is bool and
                    type(value['final_release_on_other_thread']) is bool and
                    value['callback_retains'] == (0 if case in ('invalid-lease', 'borrow-budget') else 1) and
                    value['callback_releases_including_caller'] == value['callback_retains'] + 1,
                    'consumer_report_invalid')
    if case in ('normal', 'cross-thread'):
        prepare.require(value['source_sha256'] == file_hash and 0 < value['method_count'] <= 65536 and
                        value['snapshot_released_before_last_read'] is True and
                        (case != 'cross-thread' or value['final_release_on_other_thread'] is True), 'consumer_workload_incomplete')
    else:
        prepare.require(value['source_sha256'] is None and value['method_count'] == value['result_checksum'] == 0 and
                        value['snapshot_released_before_last_read'] is False and
                        value['final_release_on_other_thread'] is False,
                        'rejection_report_invalid')
    if not traced:
        prepare.require(len(lines) == 2, 'baseline_trace_unexpected')
        return value, None
    prepare.require(len(lines) >= 3, 'trace_header_missing')
    header = re.fullmatch(r'MGMEM_DEX_READER_TRACE_V1 ([0-9]+) ([0-9a-f]{64})', lines[2])
    prepare.require(header and header.group(2) == consumed_hash, 'trace_input_identity_mismatch')
    count = int(header.group(1))
    prepare.require(count <= limit and len(lines) == count + 3 and
                    ((count == 0) if case in ('invalid-lease', 'borrow-budget') else count > 0), 'trace_count_invalid')
    hits = set()
    for line in lines[3:]:
        event = re.fullmatch(r'E ([0-9]+) ([0-9]+)', line)
        prepare.require(event is not None, 'trace_event_invalid')
        site, offset = int(event.group(1)), int(event.group(2))
        prepare.require((1 <= site <= 31 or site == 1001) and offset < size, 'trace_event_out_of_scope')
        hits.add(site)
    return value, {'events': count, 'site_count': len(hits)}


def remaining(deadline):
    value = deadline - time.monotonic()
    prepare.require(value > 0, 'overall_timeout')
    return value


def _hash_value(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def output_binding(name, data):
    """Describe bytes already written to a private per-process output file."""
    return {'name': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def validate_pair_binding(value):
    """Validate the complete v2 binding, without claiming producer exit."""
    fields = {'schema_version', 'state', 'scope', 'producer_exit', 'candidate_source_commit',
              'candidate_sha256', 'producer_script_sha256', 'baseline_executable_sha256',
              'traced_executable_sha256', 'core_file_sha256', 'source_lock_sha256',
              'input_bytes', 'input_sha256', 'corrupt_snapshot_sha256', 'event_limit',
              'stdout_limit_bytes', 'stderr_limit_bytes', 'pairs'}
    prepare.require(isinstance(value, dict) and set(value) == fields and
                    type(value['schema_version']) is int and value['schema_version'] == 2 and
                    value['state'] == 'PAIR_OUTPUT_FILES_BOUND' and value['scope'] == BINDING_SCOPE and
                    value['producer_exit'] == 'NOT_ATTESTED' and
                    isinstance(value['candidate_source_commit'], str) and
                    re.fullmatch(r'[0-9a-f]{40}', value['candidate_source_commit']) is not None and
                    all(_hash_value(value[k]) for k in ('candidate_sha256', 'producer_script_sha256',
                        'baseline_executable_sha256', 'traced_executable_sha256', 'core_file_sha256',
                        'source_lock_sha256', 'input_sha256', 'corrupt_snapshot_sha256')) and
                    type(value['input_bytes']) is int and 1 <= value['input_bytes'] <= capture.MAX_INPUT and
                    type(value['event_limit']) is int and 1 <= value['event_limit'] <= 1000000 and
                    type(value['stdout_limit_bytes']) is int and value['stdout_limit_bytes'] == capture.MAX_STDOUT and
                    type(value['stderr_limit_bytes']) is int and value['stderr_limit_bytes'] == capture.MAX_STDERR,
                    'binding_header_invalid')
    pairs = value['pairs']
    expected = [(variant, case) for variant in VARIANTS for case in CASES]
    prepare.require(isinstance(pairs, list) and len(pairs) == len(expected), 'binding_pairs_invalid')
    for pair, (variant, case) in zip(pairs, expected):
        prepare.require(isinstance(pair, dict) and set(pair) == {'variant', 'case', 'snapshot_sha256',
            'snapshot_digest_evidence', 'events', 'sites', 'baseline', 'traced'} and
            pair['variant'] == variant and pair['case'] == case and
            pair['snapshot_sha256'] == (value['corrupt_snapshot_sha256'] if case == 'corrupt-input' else value['input_sha256']) and
            pair['snapshot_digest_evidence'] == 'TRACED_SCOPE_HEADER_REPORTED' and
            type(pair['events']) is int and 0 <= pair['events'] <= value['event_limit'] and
            type(pair['sites']) is int and 0 <= pair['sites'] <= min(32, pair['events']) and
            ((pair['events'] == pair['sites'] == 0) if case in ('invalid-lease', 'borrow-budget')
             else pair['events'] > 0 and pair['sites'] > 0), 'binding_pair_invalid')
        for mode in ('baseline', 'traced'):
            process = pair[mode]
            prepare.require(isinstance(process, dict) and set(process) == {'exit_code', 'stdout', 'stderr'} and
                            type(process['exit_code']) is int and process['exit_code'] == 0,
                            'binding_process_invalid')
            for stream in ('stdout', 'stderr'):
                record = process[stream]
                expected_name = mode + '-' + variant + '-' + case + '.' + stream
                maximum = value[stream + '_limit_bytes']
                prepare.require(isinstance(record, dict) and set(record) == {'name', 'bytes', 'sha256'} and
                                record['name'] == expected_name and type(record['bytes']) is int and
                                0 <= record['bytes'] <= maximum and _hash_value(record['sha256']) and
                                (record['bytes'] > 0 if stream == 'stdout' else
                                 record['bytes'] == 0 and record['sha256'] == EMPTY_SHA256),
                                'binding_output_invalid')
    return value


def verify_bound_outputs(directory, binding):
    """Re-read the exact regular files under the output directory and check bytes."""
    validate_pair_binding(binding)
    for pair in binding['pairs']:
        for mode in ('baseline', 'traced'):
            for stream in ('stdout', 'stderr'):
                record = pair[mode][stream]
                data = prepare_consumer.read_regular(Path(directory) / record['name'], binding[stream + '_limit_bytes'])
                prepare.require(len(data) == record['bytes'] and hashlib.sha256(data).hexdigest() == record['sha256'],
                                'binding_output_drift')


def publish_pair_binding(directory, binding):
    """Write only after complete validation; the caller must also attest its exit."""
    directory = Path(directory)
    verify_bound_outputs(directory, binding)
    target = directory / BINDING_NAME
    data = (json.dumps(binding, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    with target.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    prepare.require(prepare_consumer.read_regular(target, len(data)) == data, 'binding_write_drift')
    return target


def progress(out, completed, current, state='RUNNING'):
    pending = out / 'progress.pending'
    data = {'state': state, 'completed_pairs': completed, 'total_pairs': len(VARIANTS) * len(CASES),
            'current': current, 'performance_qualified': False}
    with pending.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(data, stream); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(pending, out / 'progress.json')


def main(args):
    prepare.require(1 <= args.overall_timeout_seconds <= 3600, 'overall_timeout_invalid')
    deadline = time.monotonic() + args.overall_timeout_seconds
    root = (ROOT / 'build').resolve()
    out = prepare_consumer.confined(args.out_dir, prepare_consumer.build_root())
    prepare.require(not out.exists(), 'output_directory_invalid')
    inputs = [args.input.resolve(strict=True), args.baseline.resolve(strict=True), args.traced.resolve(strict=True),
              args.core.resolve(strict=True), args.candidate.resolve(strict=True), args.candidate_evidence.resolve(strict=True),
              args.source_lock.resolve(strict=True)]
    prepare.require(all(p.is_file() and p.is_relative_to(ROOT) for p in inputs) and
                    1 <= args.max_events <= 1000000 and 1 <= args.timeout_seconds <= 300, 'inputs_invalid')
    source, baseline, traced, core, candidate, evidence, lock = inputs
    dex_stage = prepare_consumer.confined(args.dex_stage, prepare_consumer.build_root(), must_exist=True)
    paired_stage = prepare_consumer.confined(args.paired_stage, prepare_consumer.build_root(), must_exist=True)
    sdk = prepare_consumer.confined(args.sdk_prefix, prepare_consumer.build_root(), must_exist=True)
    prepare.require(all(p.is_dir() and p.is_relative_to(root) for p in (dex_stage, paired_stage, sdk)) and
                    core.is_relative_to(sdk) and baseline.is_relative_to(root) and traced.is_relative_to(root) and
                    baseline != traced, 'input_scope_invalid')
    prepare.require(all(not out.is_relative_to(p) and not p.is_relative_to(out)
                        for p in (dex_stage, paired_stage, sdk, baseline.parent, traced.parent)), 'output_overlaps_inputs')
    size, file_hash = capture.input_identity(source)
    data = capture.read_input(source)
    prepare.require(len(data) == size and hashlib.sha256(data).hexdigest() == file_hash and data[:4] == b'dex\n', 'input_changed_or_invalid')
    corrupt_hash = hashlib.sha256(b'\0' + data[1:]).hexdigest()
    del data
    dex_manifest = prepare_consumer.verify_prepared(dex_stage, lock)
    paired_manifest = verify_paired(paired_stage)
    candidate_commit, candidate_hash = verify_sdk(candidate, evidence, sdk)
    remaining(deadline)
    for exe, mode in ((baseline, 'baseline'), (traced, 'traced')):
        cache_values(exe.parent, mode, dex_stage, paired_stage, sdk)
    checked = inputs + [dex_stage / 'manifest.json', paired_stage / 'manifest.json',
                        baseline.parent / 'CMakeCache.txt', traced.parent / 'CMakeCache.txt',
                        ROOT / 'consumers/dex/paired_v2.cpp', Path(__file__).resolve(),
                        ROOT / 'consumers/dex_trace/prepare_consumer.py', ROOT / 'consumers/dex_trace/prepare_paired.py',
                        ROOT / 'consumers/dex_trace/prepare.py', ROOT / 'consumers/dex_trace/capture.py',
                        ROOT / 'consumers/image/trace_sha256.hpp']
    identities = {str(path): capture.sha(path) for path in checked}
    env = {'PATH': str(core.parent) + os.pathsep + os.defpath,
           'MGMEM_DEX_TRACE_MAX_EVENTS': str(args.max_events)}
    for key in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'):
        if key in os.environ: env[key] = os.environ[key]
    if os.name != 'nt': env['LD_LIBRARY_PATH'] = str(core.parent)
    out.mkdir(parents=True, exist_ok=False)
    summaries = []
    bindings = []
    progress(out, 0, None)
    for variant in VARIANTS:
        for case in CASES:
            remaining(deadline)
            progress(out, len(summaries), variant + ':' + case)
            reports = []
            process_bindings = {}
            for mode, exe in (('baseline', baseline), ('traced', traced)):
                code, stdout, stderr = capture.run_bounded([str(exe), str(source), '--variant', variant, '--case', case],
                                                           min(args.timeout_seconds, remaining(deadline)), env=env)
                stdout_name = mode + '-' + variant + '-' + case + '.stdout'
                stderr_name = mode + '-' + variant + '-' + case + '.stderr'
                with (out / stdout_name).open('xb') as stream: stream.write(stdout)
                with (out / stderr_name).open('xb') as stream: stream.write(stderr)
                if mode == 'traced':
                    diagnostic = traced_failure(code, stderr, args.max_events)
                    if diagnostic is not None:
                        safe = {'state': 'INCOMPLETE', 'scope': BINDING_SCOPE,
                                'evidence_scope': 'CHILD_REPORTED_NOT_INDEPENDENTLY_VERIFIED',
                                'variant': variant, 'case': case, **diagnostic}
                        # This is a failure sidecar, never a capture or replay PASS.
                        with (out / 'trace-incomplete.json').open('xb') as stream:
                            stream.write((json.dumps(safe, ensure_ascii=False, indent=2,
                                                     allow_nan=False) + '\n').encode('utf-8'))
                            stream.flush(); os.fsync(stream.fileno())
                        raise TraceIncomplete(safe)
                prepare.require(code == 0 and not stderr, 'consumer_process_incomplete')
                reports.append(parse_output(stdout, mode == 'traced', variant, case, size, file_hash,
                                            corrupt_hash if case == 'corrupt-input' else file_hash, core, args.max_events))
                process_bindings[mode] = {'exit_code': code, 'stdout': output_binding(stdout_name, stdout),
                                          'stderr': output_binding(stderr_name, stderr)}
            left, right = reports[0][0], reports[1][0]
            prepare.require(all(left[key] == right[key] for key in STABLE), 'consumer_result_mismatch')
            summaries.append({'variant': variant, 'case': case, 'events': reports[1][1]['events'],
                              'sites': reports[1][1]['site_count'],
                              'memory_domain': 'PRESENT' if variant in ('D-C1', 'D-C2') else 'NOT_APPLICABLE'})
            bindings.append({'variant': variant, 'case': case,
                             'snapshot_sha256': corrupt_hash if case == 'corrupt-input' else file_hash,
                             'snapshot_digest_evidence': 'TRACED_SCOPE_HEADER_REPORTED',
                             'events': reports[1][1]['events'], 'sites': reports[1][1]['site_count'],
                             'baseline': process_bindings['baseline'], 'traced': process_bindings['traced']})
            progress(out, len(summaries), None)
            if len(summaries) % 5 == 0:
                print(json.dumps({'state': 'RUNNING', 'completed_pairs': len(summaries),
                                  'total_pairs': len(VARIANTS) * len(CASES)}), flush=True)
    remaining(deadline)
    prepare.require(all(capture.sha(Path(path)) == value for path, value in identities.items()) and
                    capture.input_identity(source) == (size, file_hash), 'input_or_binary_drift')
    prepare.require(prepare_consumer.verify_prepared(dex_stage, lock) == dex_manifest and
                    verify_paired(paired_stage) == paired_manifest and
                    verify_sdk(candidate, evidence, sdk) == (candidate_commit, candidate_hash), 'prepared_inputs_drift')
    remaining(deadline)
    binding = {'schema_version': 2, 'state': 'PAIR_OUTPUT_FILES_BOUND', 'scope': BINDING_SCOPE,
               'producer_exit': 'NOT_ATTESTED', 'candidate_source_commit': candidate_commit,
               'candidate_sha256': candidate_hash,
               'producer_script_sha256': identities[str(Path(__file__).resolve())],
               'baseline_executable_sha256': identities[str(baseline)],
               'traced_executable_sha256': identities[str(traced)],
               'core_file_sha256': identities[str(core)], 'source_lock_sha256': identities[str(lock)],
               'input_bytes': size, 'input_sha256': file_hash, 'corrupt_snapshot_sha256': corrupt_hash,
               'event_limit': args.max_events, 'stdout_limit_bytes': capture.MAX_STDOUT,
               'stderr_limit_bytes': capture.MAX_STDERR, 'pairs': bindings}
    publish_pair_binding(out, binding)
    remaining(deadline)
    result = {'state': 'PASS_SCOPED_PAIRED_READER_CAPTURE', 'scope': 'SNAPSHOT_OPEN_CALLER_THREAD_METADATA_READER_ONLY',
              'candidate_source_commit': candidate_commit, 'candidate_sha256': candidate_hash,
              'input_sha256': file_hash, 'input_bytes': size, 'loaded_core_identity': 'CHILD_REPORTED_OS_MODULE_PATH_AND_DISK_HASH_MATCH',
              'cases': summaries, 'performance_qualified': False,
              'limits': ['Requested binary/source hashes are not signed build provenance or mapped-byte proof.',
                         'Excludes SHA, query, decoder, non-reader memory accesses and cross-thread lifecycle events.',
                         'Diagnostic access order is not the original machine instruction or hardware access order.',
                         'Ordinary allocation/copy changes are not Memory net benefit.']}
    progress(out, len(summaries), None, state='PAIR_CHECKS_PASSED_RECEIPT_PENDING')
    with (out / 'receipt.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, indent=2); stream.write('\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'baseline', 'traced', 'core', 'candidate', 'candidate-evidence', 'source-lock',
                 'dex-stage', 'paired-stage', 'sdk-prefix', 'out-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--max-events', type=int, default=250000)
    parser.add_argument('--timeout-seconds', type=int, default=60)
    parser.add_argument('--overall-timeout-seconds', type=int, default=300)
    try:
        result = main(parser.parse_args())
        print(json.dumps({'state': result['state'], 'pair_count': len(result['cases']),
                          'scope': result['scope'], 'performance_qualified': False}))
    except TraceIncomplete as error:
        print(json.dumps(error.report, ensure_ascii=False, allow_nan=False))
        raise SystemExit(2)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, zipfile.BadZipFile, tarfile.TarError) as error:
        print(json.dumps({'state': 'INCOMPLETE', 'reason': type(error).__name__}))
        raise SystemExit(2)
