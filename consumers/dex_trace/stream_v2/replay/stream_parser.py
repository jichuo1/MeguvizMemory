#!/usr/bin/env python3
"""Bounded decoder for the independent MDXTRC2 reader-event stream.

The caller owns input/producer qualification. This module validates one file,
never promotes a previous V1 receipt and never reports hardware/performance.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import sys
import time

DEX_TRACE = Path(__file__).resolve().parents[2]
if str(DEX_TRACE) not in sys.path:
    sys.path.insert(0, str(DEX_TRACE))
import paired_compare as paired
import prepare_consumer as safe

MAGIC = b'MDXTRC2\n'
HEADER_BYTES = 88
MAX_INPUT = 32 * 1024 * 1024
MAX_EVENTS = 64_000_000
MAX_MODULE_LINE = 262169
MAX_JSON_LINE = 65536
MAX_TRACE_BYTES = HEADER_BYTES + MAX_EVENTS * 7 + 9 + MAX_MODULE_LINE + MAX_JSON_LINE
VARIANTS = ('D-C0', 'D-Z0', 'D-C1', 'D-C2')
CASES = ('normal', 'cross-thread', 'invalid-lease', 'borrow-budget', 'corrupt-input')
SHA_CHARS = frozenset('0123456789abcdef')


def require(value, reason='TRACE_INVALID'):
    if not value:
        raise ValueError(reason)


def valid_sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in SHA_CHARS for c in value)


def _signature(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_nlink)


def _open_regular(path, maximum):
    """Confine ancestors, disallow links/hardlinks and compare opened identity."""
    path = safe.confined(path, safe.build_root(), must_exist=True)
    before = path.lstat()
    require(not safe.is_link(before) and stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
            0 <= before.st_size <= maximum)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        require(not safe.is_link(opened) and stat.S_ISREG(opened.st_mode) and
                _signature(opened) == _signature(before))
        return path, before, os.fdopen(fd, 'rb', buffering=65536)
    except BaseException:
        os.close(fd)
        raise


def _core_identity(path, deadline):
    """Hash a single canonical SDK file without materializing it in memory."""
    path, before, stream = _open_regular(path, safe.MAX_FILE)
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            if deadline is not None:
                require(time.monotonic() < deadline, 'TIMEOUT')
            block = stream.read(65536)
            if not block:
                break
            total += len(block)
            require(total <= safe.MAX_FILE)
            digest.update(block)
        after_fd = os.fstat(stream.fileno())
        after_path = path.lstat()
        require(not safe.is_link(after_path) and stat.S_ISREG(after_path.st_mode) and
                _signature(before) == _signature(after_fd) == _signature(after_path) and total == before.st_size)
        if deadline is not None:
            require(time.monotonic() < deadline, 'TIMEOUT')
        result = (path, digest.hexdigest())
    finally:
        stream.close()
    if deadline is not None:
        require(time.monotonic() < deadline, 'TIMEOUT')
    return result


def _consumer_tail(module, report, variant, case, size, file_sha256, core_path, deadline):
    """Validate V1 consumer semantics without its unbounded-to-EOF core hash."""
    try:
        match = re.fullmatch(rb'MGMEM_CORE_MODULE_HEX ([0-9a-f]+)\n', module)
        require(match is not None and len(match.group(1)) % 2 == 0)
        module_path = Path(bytes.fromhex(match.group(1).decode('ascii')).decode('utf-8', 'strict')).resolve(strict=True)
        core, core_sha = _core_identity(core_path, deadline)
        require(module_path == core.resolve(strict=True))
        value = paired.unique_json(report.decode('utf-8', 'strict'))
        require(isinstance(value, dict) and all(key in value for key in paired.STABLE) and
                type(value['schema_version']) is int and value['schema_version'] == 2 and
                value['workload'] == 'dex_source_strategy_complete_owner_v2' and
                value['variant'] == variant and value['case'] == case and value['success'] is True and
                value['diagnostic'] is False and value['failure_reason'] is None and
                all(type(value[k]) is int and 0 <= value[k] <= 2**64 - 1 for k in paired.UNSIGNED_FIELDS) and
                value['input_bytes'] == value['logical_requested_bytes'] == size and
                value['input_budget_bytes'] == 32 * 1024 * 1024 and value['parser_budget_bytes'] == 256 * 1024 * 1024 and
                value['resource_scope'] == 'source_capacity_plus_unique_input_backing_parser_separate_metadata_RSS_unknown' and
                value['product_scope'] == 'independent_DexMeguviz_not_BIL_DexKit' and
                type(value['expected_status']) is int and type(value['observed_status']) is int and
                value['expected_status'] == value['observed_status'] == paired.EXPECTED_STATUS[case] and
                value['lease_final_correct'] is True and value['source_final_released'] is True and
                value['final_resource_state'] == 'CLOSED_ZERO' and
                value['closed_backing_bytes'] == value['reserved_final_bytes'] == 0 and
                type(value['snapshot_released_before_last_read']) is bool and
                type(value['final_release_on_other_thread']) is bool and
                value['callback_retains'] == (0 if case in ('invalid-lease', 'borrow-budget') else 1) and
                value['callback_releases_including_caller'] == value['callback_retains'] + 1)
        if case in ('normal', 'cross-thread'):
            require(value['source_sha256'] == file_sha256 and 0 < value['method_count'] <= 65536 and
                    value['snapshot_released_before_last_read'] is True and
                    (case != 'cross-thread' or value['final_release_on_other_thread'] is True))
        else:
            require(value['source_sha256'] is None and value['method_count'] == value['result_checksum'] == 0 and
                    value['snapshot_released_before_last_read'] is False and
                    value['final_release_on_other_thread'] is False)
        return value, core_sha
    except (UnicodeError, KeyError, TypeError, OSError):
        raise ValueError('TRACE_INVALID') from None


def scan_file(path, *, size, file_sha256, consumed_sha256, limit, variant, case, core_path,
              expected_count=None, expected_trace_sha256=None, max_trace_bytes=MAX_TRACE_BYTES,
              deadline=None, on_event=None):
    """Stream-validate one complete trace, optionally calling on_event(site, offset).

    File identity and digest are checked after the entire stream. A callback
    has already run if a late mismatch occurs; callers with native resources
    must conservatively quarantine on any exception. Deadlines are cooperative.
    """
    require(type(size) is int and 1 <= size <= MAX_INPUT and valid_sha(file_sha256) and
            valid_sha(consumed_sha256) and type(limit) is int and 1 <= limit <= MAX_EVENTS and
            variant in VARIANTS and case in CASES and
            (expected_count is None or (type(expected_count) is int and 0 <= expected_count <= limit)) and
            (expected_trace_sha256 is None or valid_sha(expected_trace_sha256)) and
            type(max_trace_bytes) is int and HEADER_BYTES + 9 <= max_trace_bytes <= MAX_TRACE_BYTES)
    path, before, stream = _open_regular(path, max_trace_bytes)
    digest = hashlib.sha256()
    seen_bytes = 0

    def read_exact(length):
        nonlocal seen_bytes
        data = stream.read(length)
        require(len(data) == length)
        digest.update(data)
        seen_bytes += length
        require(seen_bytes <= max_trace_bytes)
        return data

    def line(maximum):
        nonlocal seen_bytes
        data = stream.readline(maximum + 1)
        require(0 < len(data) <= maximum and data.endswith(b'\n'))
        digest.update(data)
        seen_bytes += len(data)
        require(seen_bytes <= max_trace_bytes)
        return data

    def check_deadline():
        if deadline is not None:
            require(time.monotonic() < deadline, 'TIMEOUT')

    try:
        check_deadline()
        header = read_exact(HEADER_BYTES)
        require(header[:8] == MAGIC)
        input_size, event_limit = struct.unpack('<QQ', header[8:24])
        require(input_size == size and event_limit == limit and
                header[24:88] == consumed_sha256.encode('ascii'))
        count = 0
        sites = set()
        while True:
            if count % 4096 == 0:
                check_deadline()
            tag = read_exact(1)
            if tag == b'\x02':
                footer_count = struct.unpack('<Q', read_exact(8))[0]
                require(footer_count == count and (expected_count is None or count == expected_count))
                break
            require(tag == b'\x01' and count < limit)
            site, offset = struct.unpack('<HI', read_exact(6))
            require((1 <= site <= 31 or site == 1001) and offset < size)
            sites.add(site)
            count += 1
            if on_event is not None:
                on_event(site, offset)
        require((count == 0) if case in ('invalid-lease', 'borrow-budget') else count > 0)
        module = line(MAX_MODULE_LINE)
        report = line(MAX_JSON_LINE)
        require(stream.read(1) == b'' and seen_bytes == before.st_size)
        check_deadline()
        # Child-reported module path is checked against the selected file; it
        # is not proof of mapped bytes. Unlike the V1 parser, this bounds core
        # identity reading and never alters the V1 protocol.
        consumer, core_sha = _consumer_tail(module, report, variant, case, size,
                                            file_sha256, core_path, deadline)
        after_fd = os.fstat(stream.fileno())
        after_path = path.lstat()
        require(not safe.is_link(after_path) and stat.S_ISREG(after_path.st_mode) and
                _signature(before) == _signature(after_fd) == _signature(after_path))
        trace_hash = digest.hexdigest()
        require(expected_trace_sha256 is None or trace_hash == expected_trace_sha256)
        check_deadline()
        result = {'event_count': count, 'site_count': len(sites), 'trace_bytes': seen_bytes,
                  'trace_sha256': trace_hash, 'core_sha256': core_sha, 'consumer': consumer}
    finally:
        stream.close()
    check_deadline()
    return result
