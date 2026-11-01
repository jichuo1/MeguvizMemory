#!/usr/bin/env python3
"""Replay one MDXTRC2 event stream in an isolated process, not a benchmark."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
import struct
import sys
import time

HERE = Path(__file__).resolve().parent
DEX_TRACE = HERE.parents[1]
for directory in (str(DEX_TRACE), str(HERE)):
    if directory not in sys.path:
        sys.path.insert(0, directory)
import stream_parser as wire
import prepare_consumer as safe
import paired_replay_worker as legacy

FIELDS = {'schema_version', 'input_path', 'trace_path', 'variant', 'case',
          'input_sha256', 'input_bytes', 'consumed_sha256', 'trace_sha256',
          'trace_bytes', 'event_limit', 'event_count', 'core_path', 'core_sha256', 'max_trace_bytes'}


def require(value, reason='INPUT_INVALID'):
    if not value:
        raise ValueError(reason)


def path_in_build(value):
    require(isinstance(value, str) and value)
    return safe.confined(Path(value), safe.build_root(), must_exist=True)


def execute(request, deadline=None):
    require(isinstance(request, dict) and set(request) == FIELDS and
            type(request['schema_version']) is int and request['schema_version'] == 2 and
            request['variant'] in wire.VARIANTS and request['case'] in wire.CASES and
            all(wire.valid_sha(request[key]) for key in ('input_sha256', 'consumed_sha256', 'trace_sha256', 'core_sha256')))
    for key in ('input_bytes', 'trace_bytes', 'event_limit', 'event_count', 'max_trace_bytes'):
        require(type(request[key]) is int)
    require(1 <= request['input_bytes'] <= wire.MAX_INPUT and
            1 <= request['event_limit'] <= wire.MAX_EVENTS and
            0 <= request['event_count'] <= request['event_limit'] and
            wire.HEADER_BYTES + 9 <= request['trace_bytes'] <= request['max_trace_bytes'] <= wire.MAX_TRACE_BYTES)
    source, trace, core = (path_in_build(request[k]) for k in ('input_path', 'trace_path', 'core_path'))
    require(source != trace and source != core and trace != core)
    core_stat = core.lstat()
    require(not safe.is_link(core_stat) and stat.S_ISREG(core_stat.st_mode) and core_stat.st_nlink == 1)
    payload = safe.read_regular(source, wire.MAX_INPUT)
    require(len(payload) == request['input_bytes'] and hashlib.sha256(payload).hexdigest() == request['input_sha256'])
    if request['case'] == 'corrupt-input':
        payload = b'\0' + payload[1:]
    require(hashlib.sha256(payload).hexdigest() == request['consumed_sha256'])
    options = dict(size=request['input_bytes'], file_sha256=request['input_sha256'],
                   consumed_sha256=request['consumed_sha256'], limit=request['event_limit'],
                   variant=request['variant'], case=request['case'], core_path=core,
                   expected_count=request['event_count'], expected_trace_sha256=request['trace_sha256'],
                   max_trace_bytes=request['max_trace_bytes'], deadline=deadline)
    # Do not allocate a domain until the complete bound file and tail pass.
    first = wire.scan_file(trace, **options)
    require(first['trace_bytes'] == request['trace_bytes'], 'TRACE_INVALID')
    # The selected candidate identity must be verified before Workspace loads
    # any shared core. Later scans still recheck it; this is not a signed or
    # mapped-image attestation against a same-permission concurrent adversary.
    require(first['core_sha256'] == request['core_sha256'], 'INPUT_INVALID')
    ws = None
    try:
        ws = legacy.Workspace(request['variant'], payload, core)
        del payload
        digest = hashlib.sha256()
        def consume(site, offset):
            digest.update(struct.pack('<IQB', site, offset, ws.read(offset)))
        second = wire.scan_file(trace, **options, on_event=consume)
        require(second == first, 'TRACE_INVALID')
        outcome = ws.close()
        require(ws.finished and outcome != 'UNKNOWN_QUARANTINED', 'CORE_OR_MEMORY_UNKNOWN')
        return {'schema_version': 2, 'state': 'PASS_STREAM_V2_READER_BYTE_WORKER',
                'variant': request['variant'], 'case': request['case'], 'events': second['event_count'],
                'sites': second['site_count'], 'trace_sha256': second['trace_sha256'],
                'trace_bytes': second['trace_bytes'], 'core_sha256': second['core_sha256'],
                'checksum_sha256': digest.hexdigest(),
                'core_use': 'REQUESTED_SHARED_CORE_NOT_LOAD_ATTESTED' if ws.core is not None else 'NOT_APPLICABLE',
                'cleanup_outcome': outcome, 'device': 'NOT_RUN', 'performance': 'NOT_MEASURED'}
    except BaseException as error:
        # A native call or handle may have been consumed; never retry cleanup.
        # The isolated process exiting is not proof of normal reclamation.
        if ws is not None and ws.core is not None:
            ws.uncertain = True
        raise RuntimeError('CORE_OR_MEMORY_UNKNOWN') from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=int, default=300)
    args = parser.parse_args()
    try:
        require(1 <= args.timeout_seconds <= 3600)
        request_path = path_in_build(str(args.request))
        request = safe.json_object(safe.read_regular(request_path, 32768).decode('utf-8'))
        value = execute(request, deadline=time.monotonic() + args.timeout_seconds)
        print(json.dumps(value, separators=(',', ':'), allow_nan=False))
        return 0
    except BaseException as error:
        reason = str(error) if str(error) in ('INPUT_INVALID', 'TRACE_INVALID', 'CORE_OR_MEMORY_UNKNOWN', 'TIMEOUT') else 'UNEXPECTED'
        outcome = 'UNKNOWN_QUARANTINED' if reason == 'CORE_OR_MEMORY_UNKNOWN' else 'NOT_STARTED'
        print(json.dumps({'schema_version': 2, 'state': 'INCOMPLETE', 'reason': reason,
                          'cleanup_outcome': outcome, 'device': 'NOT_RUN', 'performance': 'NOT_MEASURED'},
                         separators=(',', ':')))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
