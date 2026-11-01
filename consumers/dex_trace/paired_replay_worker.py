#!/usr/bin/env python3
"""Replay bound DEX reader byte offsets in an isolated process; no performance claim."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import struct
import sys

import prepare_consumer as bounded

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'consumers' / 'image'))
import trace_replay as image  # Existing ABI declarations and core loader.

MAX_INPUT = 32 * 1024 * 1024
MAX_OUTPUT = 32 * 1024 * 1024
VARIANTS = ('D-C0', 'D-Z0', 'D-C1', 'D-C2')
CASES = ('normal', 'cross-thread', 'invalid-lease', 'borrow-budget', 'corrupt-input')
STATUSES = {'normal': 0, 'cross-thread': 0, 'invalid-lease': 1, 'borrow-budget': 4, 'corrupt-input': 2}
SHA = re.compile(r'^[0-9a-f]{64}$')
FIELDS = {'schema_version', 'input_path', 'stdout_path', 'variant', 'case', 'input_sha256',
          'input_bytes', 'stdout_sha256', 'stdout_bytes', 'consumed_sha256', 'event_count',
          'event_limit', 'core_path'}


def require(value, reason='INPUT_INVALID'):
    if not value:
        raise ValueError(reason)


def path_in_build(value):
    require(isinstance(value, str) and value, 'INPUT_INVALID')
    return bounded.confined(Path(value), bounded.build_root(), must_exist=True)


def read_bound(path, maximum, size, digest):
    require(type(size) is int and 0 <= size <= maximum and isinstance(digest, str) and SHA.fullmatch(digest), 'INPUT_INVALID')
    data = bounded.read_regular(path, maximum)
    require(len(data) == size and hashlib.sha256(data).hexdigest() == digest, 'INPUT_INVALID')
    return data


def event_sequence(raw, variant, case, consumed_sha, size, expected_count, event_limit):
    """Validate the complete selected trace, including zero-event rejections."""
    require(isinstance(raw, bytes) and raw.endswith(b'\n'), 'TRACE_INVALID')
    stream = io.BytesIO(raw.replace(b'\r\n', b'\n'))
    module = stream.readline(262180)
    require(re.fullmatch(rb'MGMEM_CORE_MODULE_HEX [0-9a-f]+\n', module) is not None, 'TRACE_INVALID')
    report_raw = stream.readline(65537)
    require(report_raw.endswith(b'\n') and len(report_raw) <= 65536, 'TRACE_INVALID')
    try:
        report = bounded.json_object(report_raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise ValueError('TRACE_INVALID') from None
    require(isinstance(report, dict) and report.get('variant') == variant and report.get('case') == case and
            report.get('success') is True and type(report.get('expected_status')) is int and
            type(report.get('observed_status')) is int and
            report['expected_status'] == report['observed_status'] == STATUSES[case], 'TRACE_INVALID')
    header = stream.readline(256)
    match = re.fullmatch(rb'MGMEM_DEX_READER_TRACE_V1 ([0-9]+) ([0-9a-f]{64})\n', header)
    require(match is not None and match.group(2).decode('ascii') == consumed_sha, 'TRACE_INVALID')
    count = int(match.group(1))
    require(type(expected_count) is int and type(event_limit) is int and 1 <= event_limit <= 1000000 and
            0 <= count <= event_limit and count == expected_count and
            (count == 0 if case in ('invalid-lease', 'borrow-budget') else count > 0), 'TRACE_INVALID')
    events = []
    for _ in range(count):
        line = stream.readline(128)
        match = re.fullmatch(rb'E ([0-9]+) ([0-9]+)\n', line)
        require(match is not None, 'TRACE_INVALID')
        site, offset = int(match.group(1)), int(match.group(2))
        require((1 <= site <= 31 or site == 1001) and offset < size, 'TRACE_INVALID')
        events.append((site, offset))
    require(stream.read(1) == b'', 'TRACE_INVALID')
    return events


class Workspace:
    """Only this process's allocations; uncertain core calls are never retried."""
    def __init__(self, variant, payload, core_path):
        self.variant = variant
        self.source = self.buffer = None
        self.domain = ctypes.c_void_p()
        self.owner = None
        self.writer = self.reader = None
        self.address = 0
        self.core = None
        self.uncertain = False
        self.finished = False
        try:
            size = len(payload)
            require(0 < size <= MAX_INPUT)
            # The source is the replay process's preloaded input, not proof of
            # copying or zero-copy behavior in the original DEX consumer.
            self.source = ctypes.create_string_buffer(payload, size)
            if variant == 'D-Z0':
                self.address = ctypes.addressof(self.source)
            elif variant == 'D-C0':
                self.buffer = ctypes.create_string_buffer(size + 63)
                self.address = (ctypes.addressof(self.buffer) + 63) & ~63
                ctypes.memmove(self.address, ctypes.addressof(self.source), size)
            else:
                self.core = image.load_core(core_path)
                self.confirmed((self.core.mgmem_capabilities_v1() & 27) == 27)
                options = image.Options(ctypes.sizeof(image.Options), 1, 0, 0,
                    0 if variant == 'D-C1' else 1, 0, MAX_INPUT, size, None)
                self.checked('mgmem_domain_create_v1', ctypes.byref(options), ctypes.byref(self.domain))
                self.confirmed(bool(self.domain.value))
                owner = image.Handle()
                self.checked('mgmem_allocate_v1', self.domain, size, 64, ctypes.byref(owner))
                self.confirmed(bool(owner.domain_id and owner.id and owner.runtime_origin))
                self.owner = owner
                self.writer = self.borrow(size, 1)
                self.address = int(self.writer.data)
                try:
                    ctypes.memmove(self.address, ctypes.addressof(self.source), size)
                except BaseException:
                    self.uncertain = True
                    raise
                self.checked('mgmem_borrow_release_v1', self.domain, self.writer.lease)
                self.writer = None
                self.reader = self.borrow(size, 0)
                self.address = int(self.reader.data)
        except BaseException:
            # Setup may have partially entered native; callers must not retry.
            if self.core is not None and self.domain.value:
                self.uncertain = True
            raise

    def confirmed(self, value):
        if not value:
            self.uncertain = True
            raise ValueError('CORE_OR_MEMORY_UNKNOWN')

    def checked(self, method, *args):
        try:
            value = getattr(self.core, method)(*args)
        except BaseException:
            self.uncertain = True
            raise
        if value != 0:
            self.uncertain = True
            raise ValueError('CORE_OR_MEMORY_UNKNOWN')

    def borrow(self, size, access):
        result = image.Borrow()
        self.checked('mgmem_borrow_acquire_v1', self.domain, self.owner, 0, size, access, ctypes.byref(result))
        limit = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
        self.confirmed(bool(result.lease.id and result.lease.domain_id == self.owner.domain_id and
            result.lease.runtime_origin == self.owner.runtime_origin and result.data and result.length == size and
            result.data <= limit - size and result.data % 64 == 0))
        return result

    def read(self, offset):
        try:
            return ctypes.c_ubyte.from_address(self.address + offset).value
        except BaseException:
            if self.core is not None:
                self.uncertain = True
            raise

    def close(self):
        if self.core is None:
            self.address = 0
            self.source = self.buffer = None
            self.finished = True
            return 'DIRECT_REFERENCES_CLEARED_NOT_OS_RECLAIM_PROOF'
        if self.uncertain:
            return 'UNKNOWN_QUARANTINED'
        try:
            # Model a safe local close after reader access; this is not the
            # original SDK callback, cross-thread, or lifecycle trace.
            self.checked('mgmem_release_v1', self.domain, self.owner)
            self.owner = None
            self.checked('mgmem_domain_begin_close_v1', self.domain)
            self.checked('mgmem_borrow_release_v1', self.domain, self.reader.lease)
            self.reader = None
            self.address = 0
            self.checked('mgmem_domain_collect_v1', self.domain)
            self.checked('mgmem_domain_drain_v1', self.domain, 0)
            snapshot = image.Snapshot()
            self.checked('mgmem_domain_snapshot_v1', self.domain, ctypes.byref(snapshot), ctypes.sizeof(snapshot))
            self.confirmed(snapshot.struct_size == ctypes.sizeof(image.Snapshot) and snapshot.abi_major == 1 and
                snapshot.state == 2 and snapshot.backing_bytes == 0 and snapshot.reserved_bytes == 0 and
                snapshot.live_bytes == 0 and snapshot.retiring_bytes == 0 and snapshot.retained_bytes == 0 and
                snapshot.active_allocations == 0 and snapshot.active_leases == 0)
            domain = self.domain
            self.domain = ctypes.c_void_p()
            self.checked('mgmem_domain_destroy_v1', domain)
            self.source = None
            self.finished = True
            return 'CORE_CLOSED_ZERO_AND_DESTROYED'
        except BaseException:
            self.uncertain = True
            return 'UNKNOWN_QUARANTINED'


def execute(request):
    require(isinstance(request, dict) and set(request) == FIELDS and
            type(request['schema_version']) is int and request['schema_version'] == 1 and
            request['variant'] in VARIANTS and request['case'] in CASES and
            isinstance(request['consumed_sha256'], str) and SHA.fullmatch(request['consumed_sha256']), 'INPUT_INVALID')
    for key in ('input_bytes', 'stdout_bytes', 'event_count', 'event_limit'):
        require(type(request[key]) is int, 'INPUT_INVALID')
    require(0 < request['input_bytes'] <= MAX_INPUT and 0 <= request['stdout_bytes'] <= MAX_OUTPUT and
            1 <= request['event_limit'] <= 1000000 and 0 <= request['event_count'] <= request['event_limit'], 'INPUT_INVALID')
    input_path, stdout_path, core_path = (path_in_build(request[k]) for k in ('input_path', 'stdout_path', 'core_path'))
    core_stat = core_path.lstat()
    require(not bounded.is_link(core_stat) and stat.S_ISREG(core_stat.st_mode) and core_stat.st_nlink == 1,
            'INPUT_INVALID')
    payload = read_bound(input_path, MAX_INPUT, request['input_bytes'], request['input_sha256'])
    raw = read_bound(stdout_path, MAX_OUTPUT, request['stdout_bytes'], request['stdout_sha256'])
    if request['case'] == 'corrupt-input':
        payload = b'\0' + payload[1:]
    require(hashlib.sha256(payload).hexdigest() == request['consumed_sha256'], 'INPUT_INVALID')
    events = event_sequence(raw, request['variant'], request['case'], request['consumed_sha256'],
                            len(payload), request['event_count'], request['event_limit'])
    ws = None
    try:
        ws = Workspace(request['variant'], payload, core_path)
        del payload
        digest = hashlib.sha256()
        for site, offset in events:
            digest.update(struct.pack('<IQB', site, offset, ws.read(offset)))
        outcome = ws.close()
        require(ws.finished and outcome != 'UNKNOWN_QUARANTINED', 'CORE_OR_MEMORY_UNKNOWN')
        # The request is locally writable and does not attest the parent
        # capture's successful exit; only the outer runner can bind that fact.
        return {'schema_version': 1, 'state': 'PASS_SCOPED_READER_BYTE_WORKER', 'variant': request['variant'],
                'case': request['case'], 'events': len(events), 'checksum_sha256': digest.hexdigest(),
                'core_use': 'REQUESTED_SHARED_CORE_NOT_LOAD_ATTESTED' if ws.core is not None else 'NOT_APPLICABLE',
                'cleanup_outcome': outcome, 'device': 'NOT_RUN', 'performance': 'NOT_MEASURED'}
    except BaseException as error:
        # A Python/native call may have consumed a handle. Never infer the
        # effect or run a second cleanup after an unknown outcome.
        if ws is not None and ws.core is not None:
            ws.uncertain = True
        raise RuntimeError('CORE_OR_MEMORY_UNKNOWN') from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args()
    try:
        request_path = path_in_build(str(args.request))
        request = bounded.json_object(bounded.read_regular(request_path, 32768).decode('utf-8'))
        value = execute(request)
        print(json.dumps(value, separators=(',', ':'), allow_nan=False))
        return 0
    except BaseException as error:
        reason = str(error) if str(error) in ('INPUT_INVALID', 'TRACE_INVALID', 'CORE_OR_MEMORY_UNKNOWN') else 'UNEXPECTED'
        outcome = 'UNKNOWN_QUARANTINED' if reason == 'CORE_OR_MEMORY_UNKNOWN' else 'NOT_STARTED'
        print(json.dumps({'schema_version': 1, 'state': 'INCOMPLETE', 'reason': reason,
                          'cleanup_outcome': outcome, 'device': 'NOT_RUN', 'performance': 'NOT_MEASURED'},
                         separators=(',', ':')))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
