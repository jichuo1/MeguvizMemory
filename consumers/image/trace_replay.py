#!/usr/bin/env python3
"""Replay a receipt-bound source-level image trace using self-owned buffers.

This is a functional diagnostic, not an instruction, concurrency, performance
or hardware-mechanism replay. Callers must supply the original P6 snapshot.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
from pathlib import Path
import re
import time


MAX_PIXELS = 8 * 1024 * 1024
MAX_INPUT = 3 * MAX_PIXELS + 1024 * 1024
MAX_TRACE = 2 * 1024 * 1024 * 1024
MAX_CORE = 256 * 1024 * 1024
MAX_EVENTS = 64_000_000
U64_MAX = (1 << 64) - 1
OPS = {'DOMAIN', 'ALLOC', 'ACQUIRE', 'ACQUIRE_BUSY', 'RELEASE_LEASE', 'RELEASE_OWNER',
       'BEGIN_CLOSE', 'COLLECT_BUSY', 'DRAIN_TIMEOUT', 'COLLECT_OK', 'DRAIN_OK', 'DESTROY',
       'DIRECT_ALLOC', 'DIRECT_READY', 'DIRECT_CLOSE_DEFERRED', 'DIRECT_FREE',
       'ENQUEUE', 'ADMIT', 'WORKER_START', 'WORKER_END', 'JOIN', 'COPY_IN', 'RGB_READ',
       'LUMA_WRITE', 'LUMA_READ', 'OUTPUT_WRITE', 'OUTPUT_READ', 'OUTPUT_IO_READ'}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def file_hash(path, maximum, deadline=None):
    digest = hashlib.sha256()
    total = 0
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError('hash_timeout')
            total += len(chunk)
            require(total <= maximum, 'hashed_file_limit')
            digest.update(chunk)
    return digest.hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    def bad_constant(_):
        raise ValueError('invalid_json_constant')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant)


def uint(token, maximum=U64_MAX):
    require(bool(re.fullmatch(r'0|[1-9][0-9]*', token)), 'invalid_unsigned_integer')
    require(len(token) <= 20, 'unsigned_integer_overflow')
    value = int(token)
    require(value <= maximum, 'unsigned_integer_overflow')
    return value


def ppm_number(token):
    # The original P6 consumer accepts leading zeroes, unlike canonical trace
    # integers. Preserve that input contract while checking every digit.
    require(0 < len(token) <= 32 and all(48 <= c <= 57 for c in token), 'ppm_invalid_number')
    value = 0
    for char in token:
        digit = char - 48
        require(value <= (U64_MAX - digit) // 10, 'ppm_number_overflow')
        value = value * 10 + digit
    return value


def read_ppm(path, expected_sha256=None):
    path = Path(path)
    require(path.is_file() and path.stat().st_size <= MAX_INPUT, 'input_file_limit')
    with path.open('rb') as stream:
        data = stream.read(MAX_INPUT + 1)
        require(len(data) <= MAX_INPUT and stream.read(1) == b'', 'input_file_limit')
    if expected_sha256 is not None:
        require(hashlib.sha256(data).hexdigest() == expected_sha256, 'input_consumed_identity_mismatch')
    index = 0
    def token():
        nonlocal index
        while index < len(data):
            char = data[index]; index += 1
            if char == ord('#'):
                while index < len(data) and data[index] != 10:
                    index += 1
                if index < len(data): index += 1
                continue
            if char not in b' \t\r\n':
                start = index - 1
                while index < len(data) and data[index] not in b' \t\r\n':
                    index += 1
                    require(index - start <= 32, 'ppm_token_limit')
                require(index < len(data), 'ppm_missing_separator')
                result = data[start:index]
                separator = data[index]; index += 1
                if separator == 13 and index < len(data) and data[index] == 10: index += 1
                return result
        raise ValueError('ppm_truncated_header')
    require(token() == b'P6', 'ppm_magic')
    width, height, maximum = (ppm_number(token()) for _ in range(3))
    require(width > 0 and height > 0 and maximum == 255 and width <= MAX_PIXELS and
            height <= MAX_PIXELS and width * height <= MAX_PIXELS, 'ppm_dimensions')
    rgb = data[index:]
    require(len(rgb) == width * height * 3, 'ppm_payload_size')
    return width, height, rgb


def oracle(width, height, rgb, deadline=None):
    pixels = width * height
    grey = bytearray(pixels)
    for i in range(pixels):
        if deadline is not None and i % 4096 == 0 and time.monotonic() > deadline:
            raise TimeoutError('oracle_timeout')
        grey[i] = (77 * rgb[3*i] + 150 * rgb[3*i+1] + 29 * rgb[3*i+2]) >> 8
    output = bytearray(pixels)
    for y in range(height):
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError('oracle_timeout')
        for x in range(width):
            if deadline is not None and x % 4096 == 0 and time.monotonic() > deadline:
                raise TimeoutError('oracle_timeout')
            total = sum(grey[yy*width + xx]
                        for yy in (max(0, y-1), y, min(height-1, y+1))
                        for xx in (max(0, x-1), x, min(width-1, x+1)))
            output[y*width+x] = total // 9
    return bytes(output)


def expected_events(width, height, frames, provider, layout, close_race):
    pixels = width * height
    sizes = (3 * pixels, (min(height, 3) * width if layout == 'ring' else pixels), pixels)
    main, worker, none = 'M', 'W', 255
    if provider == 'direct':
        for index, size in enumerate(sizes): yield main, 'DIRECT_ALLOC', index, 0, size
    else:
        yield main, 'DOMAIN', none, 0, 0
        for index, size in enumerate(sizes): yield main, 'ALLOC', index, 0, size
    for frame in range(frames):
        if provider == 'direct':
            yield main, 'DIRECT_READY', none, 0, 0
        else:
            yield main, 'ACQUIRE', 1, 0, 1
            yield main, 'ACQUIRE_BUSY', 1, sizes[1]-1, 1
            yield main, 'RELEASE_LEASE', 1, 0, 1
            for index, size in enumerate(sizes): yield main, 'ACQUIRE', index, 0, size
        yield main, 'ENQUEUE', none, frame, 0
        if close_race and frame + 1 == frames:
            if provider == 'direct':
                yield main, 'DIRECT_CLOSE_DEFERRED', none, 0, 0
            else:
                for index, size in enumerate(sizes): yield main, 'RELEASE_OWNER', index, 0, size
                yield main, 'BEGIN_CLOSE', none, 0, 0
                yield main, 'COLLECT_BUSY', none, 0, 0
                yield main, 'DRAIN_TIMEOUT', none, 0, 0
        yield main, 'ADMIT', none, frame, 0
        yield worker, 'WORKER_START', none, frame, 0
        yield worker, 'COPY_IN', 0, 0, sizes[0]
        def grey_row(row):
            base = (row % min(height, 3) if layout == 'ring' else row) * width
            for x in range(width):
                rgb_offset = 3 * (row * width + x)
                for channel in range(3): yield worker, 'RGB_READ', 0, rgb_offset + channel, 1
                yield worker, 'LUMA_WRITE', 1, base + x, 1
        if layout == 'full':
            for row in range(height): yield from grey_row(row)
        next_row = 0
        for y in range(height):
            if layout == 'ring':
                while next_row <= min(height - 1, y + 1):
                    yield from grey_row(next_row)
                    next_row += 1
            for x in range(width):
                for dy in (-1, 0, 1):
                    yy = max(0, y-1) if dy < 0 else min(y+1, height-1) if dy > 0 else y
                    row = (yy % min(height, 3) if layout == 'ring' else yy) * width
                    for dx in (-1, 0, 1):
                        xx = max(0, x-1) if dx < 0 else min(x+1, width-1) if dx > 0 else x
                        yield worker, 'LUMA_READ', 1, row + xx, 1
                yield worker, 'OUTPUT_WRITE', 2, y * width + x, 1
                yield worker, 'OUTPUT_READ', 2, y * width + x, 1
        if frame + 1 == frames: yield worker, 'OUTPUT_IO_READ', 2, 0, pixels
        if provider != 'direct':
            for index, size in enumerate(sizes): yield worker, 'RELEASE_LEASE', index, 0, size
        yield worker, 'WORKER_END', none, frame, 0
        yield main, 'JOIN', none, frame, 0
    if provider == 'direct':
        for index, size in enumerate(sizes): yield main, 'DIRECT_FREE', index, 0, size
    else:
        if not close_race:
            for index, size in enumerate(sizes): yield main, 'RELEASE_OWNER', index, 0, size
            yield main, 'BEGIN_CLOSE', none, 0, 0
        yield main, 'COLLECT_OK', none, 0, 0
        yield main, 'DRAIN_OK', none, 0, 0
        yield main, 'DESTROY', none, 0, 0


class Handle(ctypes.Structure):
    _fields_ = [('domain_id', ctypes.c_uint64), ('id', ctypes.c_uint64), ('runtime_origin', ctypes.c_uint64)]


class Options(ctypes.Structure):
    _fields_ = [('struct_size', ctypes.c_uint32), ('abi_major', ctypes.c_uint32),
                ('abi_minor', ctypes.c_uint32), ('required_flags', ctypes.c_uint32),
                ('mode', ctypes.c_uint32), ('reserved', ctypes.c_uint32),
                ('budget_bytes', ctypes.c_uint64), ('arena_chunk_bytes', ctypes.c_uint64),
                ('provider', ctypes.c_void_p)]


class Borrow(ctypes.Structure):
    _fields_ = [('lease', Handle), ('data', ctypes.c_void_p), ('length', ctypes.c_uint64)]


class Snapshot(ctypes.Structure):
    _fields_ = [('struct_size', ctypes.c_uint32), ('abi_major', ctypes.c_uint32),
                ('abi_minor', ctypes.c_uint32), ('state', ctypes.c_uint32)] + [
                    (name, ctypes.c_uint64) for name in ('domain_id', 'generation', 'budget_bytes',
                    'backing_bytes', 'reserved_bytes', 'peak_backing_bytes', 'live_bytes',
                    'retiring_bytes', 'retained_bytes', 'allocation_count', 'provider_allocation_count',
                    'cumulative_requested_bytes', 'active_allocations', 'active_leases', 'page_size')]


def load_core(path):
    core = ctypes.CDLL(str(Path(path).resolve(strict=True)))
    domain = ctypes.c_void_p
    ptr = ctypes.POINTER
    types = {
        'mgmem_abi_version_v1': ([], ctypes.c_uint32),
        'mgmem_capabilities_v1': ([], ctypes.c_uint64),
        'mgmem_domain_create_v1': ([ptr(Options), ptr(domain)], ctypes.c_uint32),
        'mgmem_allocate_v1': ([domain, ctypes.c_uint64, ctypes.c_uint64, ptr(Handle)], ctypes.c_uint32),
        'mgmem_borrow_acquire_v1': ([domain, Handle, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint32, ptr(Borrow)], ctypes.c_uint32),
        'mgmem_borrow_release_v1': ([domain, Handle], ctypes.c_uint32),
        'mgmem_release_v1': ([domain, Handle], ctypes.c_uint32),
        'mgmem_domain_begin_close_v1': ([domain], ctypes.c_uint32),
        'mgmem_domain_collect_v1': ([domain], ctypes.c_uint32),
        'mgmem_domain_drain_v1': ([domain, ctypes.c_uint64], ctypes.c_uint32),
        'mgmem_domain_destroy_v1': ([domain], ctypes.c_uint32),
        'mgmem_domain_snapshot_v1': ([domain, ptr(Snapshot), ctypes.c_uint32], ctypes.c_uint32),
    }
    for name, (arguments, result) in types.items():
        function = getattr(core, name)
        function.argtypes, function.restype = arguments, result
    require(core.mgmem_abi_version_v1() >> 16 == 1 and (core.mgmem_capabilities_v1() & 11) == 11,
            'core_capability_mismatch')
    return core


class ReplayWorkspace:
    def __init__(self, core, provider, sizes):
        self.core, self.provider, self.sizes = core, provider, sizes
        self.domain = ctypes.c_void_p()
        self.owners = [None] * 3
        self.leases = [None] * 3
        self.direct = [None] * 3
        self.addresses = [None] * 3
        self.closing = self.destroyed = self.uncertain = False

    def checked(self, value, expected=0):
        if value != expected:
            self.uncertain = True
            raise ValueError('core_status_mismatch')

    def confirmed(self, value, reason):
        if not value:
            self.uncertain = True
            raise ValueError(reason)

    def call(self, name, *arguments):
        try:
            return getattr(self.core, name)(*arguments)
        except BaseException:
            # The C call may already have consumed or created a resource.
            self.uncertain = True
            raise

    def apply(self, actor, operation, index, offset, length, rgb, state):
        core, d = self.core, self.domain
        if operation == 'DOMAIN':
            options = Options(ctypes.sizeof(Options), 1, 0, 0, 0 if self.provider == 'system' else 1,
                              0, 64 * 1024 * 1024, 65536, None)
            self.checked(self.call('mgmem_domain_create_v1', ctypes.byref(options), ctypes.byref(d)))
            self.confirmed(bool(d.value), 'domain_result_unknown')
        elif operation == 'ALLOC':
            owner = Handle(); self.checked(self.call('mgmem_allocate_v1', d, length, 64, ctypes.byref(owner)))
            self.confirmed(bool(owner.id and owner.domain_id and owner.runtime_origin), 'owner_result_unknown')
            self.owners[index] = owner
        elif operation == 'DIRECT_ALLOC':
            self.direct[index] = ctypes.create_string_buffer(length)
            self.addresses[index] = ctypes.addressof(self.direct[index])
        elif operation == 'DIRECT_FREE':
            self.addresses[index] = None; self.direct[index] = None
        elif operation == 'ACQUIRE':
            borrow = Borrow(); self.checked(self.call('mgmem_borrow_acquire_v1', d, self.owners[index], offset, length, 1, ctypes.byref(borrow)))
            owner = self.owners[index]
            pointer_limit = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
            self.confirmed(bool(borrow.lease.id and borrow.lease.domain_id == owner.domain_id and
                                borrow.lease.runtime_origin == owner.runtime_origin and borrow.data and
                                borrow.length == length and borrow.data >= offset and
                                borrow.data <= pointer_limit - length), 'borrow_result_unknown')
            self.leases[index] = borrow; self.addresses[index] = borrow.data - offset
        elif operation == 'ACQUIRE_BUSY':
            refused = Borrow()
            result = self.call('mgmem_borrow_acquire_v1', d, self.owners[index], offset, length, 0, ctypes.byref(refused))
            # A surprising successful descriptor may have been consumed or
            # malformed; do not guess at its identity or release it.
            self.checked(result, 6)
            self.confirmed(not refused.lease.domain_id and not refused.lease.id and
                           not refused.lease.runtime_origin and not refused.data and not refused.length,
                           'busy_descriptor_unknown')
        elif operation == 'RELEASE_LEASE':
            borrow = self.leases[index]; require(borrow is not None, 'lease_missing')
            self.leases[index] = None; self.addresses[index] = None
            self.checked(self.call('mgmem_borrow_release_v1', d, borrow.lease))
        elif operation == 'RELEASE_OWNER':
            owner = self.owners[index]; require(owner is not None, 'owner_missing')
            self.owners[index] = None; self.checked(self.call('mgmem_release_v1', d, owner))
        elif operation == 'BEGIN_CLOSE':
            self.checked(self.call('mgmem_domain_begin_close_v1', d)); self.closing = True
        elif operation == 'COLLECT_BUSY': self.checked(self.call('mgmem_domain_collect_v1', d), 6)
        elif operation == 'DRAIN_TIMEOUT': self.checked(self.call('mgmem_domain_drain_v1', d, 0), 10)
        elif operation == 'COLLECT_OK': self.checked(self.call('mgmem_domain_collect_v1', d))
        elif operation == 'DRAIN_OK':
            self.checked(self.call('mgmem_domain_drain_v1', d, 0))
            snapshot = Snapshot(); self.checked(self.call('mgmem_domain_snapshot_v1', d, ctypes.byref(snapshot), ctypes.sizeof(snapshot)))
            self.confirmed(snapshot.struct_size == ctypes.sizeof(Snapshot) and snapshot.abi_major == 1 and
                           snapshot.state == 2 and snapshot.backing_bytes == snapshot.reserved_bytes == 0 and
                           snapshot.active_allocations == snapshot.active_leases == 0, 'domain_snapshot_unknown')
        elif operation == 'DESTROY':
            self.domain = ctypes.c_void_p(); self.checked(self.call('mgmem_domain_destroy_v1', d)); self.destroyed = True
        elif operation == 'COPY_IN':
            require(self.addresses[0] and len(rgb) == length, 'copy_source_or_lease')
            ctypes.memmove(self.addresses[0] + offset, rgb, length)
        elif operation == 'RGB_READ':
            require(self.addresses[0], 'rgb_lease_missing')
            state['rgb'].append(ctypes.c_ubyte.from_address(self.addresses[0] + offset).value)
        elif operation == 'LUMA_WRITE':
            require(self.addresses[1] and len(state['rgb']) == 3, 'grey_input_sequence')
            red, green, blue = state['rgb']; state['rgb'].clear()
            ctypes.c_ubyte.from_address(self.addresses[1] + offset).value = (77*red + 150*green + 29*blue) >> 8
            state['converted'] += 1
        elif operation == 'LUMA_READ':
            require(self.addresses[1], 'luma_lease_missing')
            state['sum'] += ctypes.c_ubyte.from_address(self.addresses[1] + offset).value
            state['taps'] += 1
        elif operation == 'OUTPUT_WRITE':
            require(self.addresses[2] and state['taps'] == 9, 'blur_input_sequence')
            ctypes.c_ubyte.from_address(self.addresses[2] + offset).value = state['sum'] // 9
            state['sum'] = state['taps'] = 0
        elif operation == 'OUTPUT_READ':
            require(self.addresses[2], 'output_lease_missing')
            state['checksum'] += ctypes.c_ubyte.from_address(self.addresses[2] + offset).value
        elif operation == 'OUTPUT_IO_READ':
            require(self.addresses[2], 'output_lease_missing')
            state['output'] = ctypes.string_at(self.addresses[2] + offset, length)

    def cleanup(self):
        if self.uncertain:
            return 'UNKNOWN_QUARANTINED'
        if self.provider == 'direct':
            self.addresses = [None] * 3
            self.direct = [None] * 3
            return 'DIRECT_REFERENCES_CLEARED_NOT_OS_RECLAIM_PROOF'
        if self.destroyed:
            return 'CORE_ALREADY_CLOSED_AND_DESTROYED'
        if not self.domain:
            return 'NOT_STARTED'
        try:
            for i, borrow in enumerate(self.leases):
                if borrow is not None:
                    self.leases[i] = None; self.addresses[i] = None
                    self.checked(self.call('mgmem_borrow_release_v1', self.domain, borrow.lease))
            for i, owner in enumerate(self.owners):
                if owner is not None:
                    self.owners[i] = None
                    self.checked(self.call('mgmem_release_v1', self.domain, owner))
            if not self.closing:
                self.checked(self.call('mgmem_domain_begin_close_v1', self.domain)); self.closing = True
            self.checked(self.call('mgmem_domain_collect_v1', self.domain))
            self.checked(self.call('mgmem_domain_drain_v1', self.domain, 0))
            snapshot = Snapshot()
            self.checked(self.call('mgmem_domain_snapshot_v1', self.domain, ctypes.byref(snapshot), ctypes.sizeof(snapshot)))
            self.confirmed(snapshot.struct_size == ctypes.sizeof(Snapshot) and snapshot.abi_major == 1 and
                           snapshot.state == 2 and snapshot.backing_bytes == snapshot.reserved_bytes == 0 and
                           snapshot.active_allocations == snapshot.active_leases == 0, 'cleanup_snapshot_unknown')
            d, self.domain = self.domain, ctypes.c_void_p()
            self.checked(self.call('mgmem_domain_destroy_v1', d)); self.destroyed = True
            return 'CORE_CLOSED_ZERO_AND_DESTROYED'
        except Exception:
            self.uncertain = True
            return 'UNKNOWN_QUARANTINED'


class ReplayFailure(ValueError):
    def __init__(self, cause, cleanup_outcome):
        super().__init__(str(cause))
        self.cleanup_outcome = cleanup_outcome


def line(stream):
    raw = stream.readline(257)
    require(raw and len(raw) <= 256 and raw.endswith(b'\n') and b'\r' not in raw, 'invalid_or_truncated_line')
    try:
        return raw[:-1].decode('ascii')
    except UnicodeError as error:
        raise ValueError('invalid_trace_encoding') from error


class HashingReader:
    """Hash the bytes actually parsed, independently of pathname checks."""
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()
        self.total = 0

    def account(self, value):
        self.total += len(value)
        require(self.total <= MAX_TRACE, 'trace_consumed_file_limit')
        self.digest.update(value)
        return value

    def readline(self, size):
        return self.account(self.stream.readline(size))

    def read(self, size):
        return self.account(self.stream.read(size))


def replay(receipt_path, trace_path, input_path, core_path, deadline=None):
    receipt_path, trace_path, input_path = map(Path, (receipt_path, trace_path, input_path))
    require(receipt_path.is_file() and receipt_path.stat().st_size <= 16384, 'capture_receipt_missing')
    with receipt_path.open('rb') as stream:
        receipt_bytes = stream.read(16385)
        require(len(receipt_bytes) <= 16384 and stream.read(1) == b'', 'capture_receipt_missing')
    receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
    receipt = strict_json(receipt_bytes.decode('utf-8'))
    require(isinstance(receipt, dict) and type(receipt.get('schema_version')) is int and
            receipt['schema_version'] == 1 and receipt.get('state') == 'CAPTURE_PROCESS_AND_IDENTITY_CONFIRMED' and
            type(receipt.get('capture_exit_code')) is int and receipt.get('capture_exit_code') == 0 and receipt.get('trace_status') == 'COMPLETE' and
            receipt.get('capture_success') is True and
            receipt.get('event_structure') == 'NOT_YET_VALIDATED_REPLAY_REQUIRED', 'capture_not_confirmed')
    require(all(type(receipt.get(name)) is int for name in ('width', 'height', 'frames', 'trace_max_events')) and
            type(receipt.get('close_race')) is bool, 'capture_parameter_types')
    for name in ('trace_sha256', 'input_sha256', 'rgb_sha256', 'output_sha256'):
        require(isinstance(receipt.get(name), str) and re.fullmatch(r'[0-9a-f]{64}', receipt[name]) is not None,
                'capture_hash_invalid')
    require(trace_path.is_file() and trace_path.stat().st_size <= MAX_TRACE, 'trace_file_limit')
    receipt_provider = receipt.get('provider')
    require(receipt_provider in ('direct', 'system', 'arena'), 'capture_provider_invalid')
    before_trace, before_input = file_hash(trace_path, MAX_TRACE, deadline), file_hash(input_path, MAX_INPUT, deadline)
    if receipt_provider == 'direct':
        before_core = None  # The direct baseline does not load or call Memory.
    else:
        require(core_path is not None, 'core_path_required')
        core_path = Path(core_path)
        before_core = file_hash(core_path, MAX_CORE, deadline)
    require(before_trace == receipt.get('trace_sha256') and before_input == receipt.get('input_sha256') and
            re.fullmatch(r'[0-9a-f]{64}', before_trace) is not None, 'capture_identity_mismatch')
    width, height, rgb = read_ppm(input_path, before_input)
    rgb_sha256 = hashlib.sha256(rgb).hexdigest()
    require(rgb_sha256 == receipt['rgb_sha256'], 'capture_rgb_identity_mismatch')
    expected_output = oracle(width, height, rgb, deadline)
    require(hashlib.sha256(f'P5\n{width} {height}\n255\n'.encode('ascii') + expected_output).hexdigest() ==
            receipt.get('output_sha256'), 'capture_output_identity_mismatch')
    state = {'rgb': [], 'sum': 0, 'taps': 0, 'checksum': 0, 'converted': 0, 'output': None}
    workspace = None
    try:
        with trace_path.open('rb') as raw_source:
            source = HashingReader(raw_source)
            require(line(source) == 'MGMEM_IMAGE_TRACE_V1', 'trace_magic')
            header = line(source).split(' ')
            require(len(header) == 8 and header[0] == 'H', 'trace_header')
            w, h, frames = (uint(x) for x in header[1:4])
            provider, layout, race = header[4:7]
            event_limit = uint(header[7], MAX_EVENTS)
            require((w, h) == (width, height) and 1 <= frames <= 32 and provider in ('direct', 'system', 'arena') and
                    layout in ('full', 'ring') and race in ('0', '1') and event_limit > 0, 'trace_header_values')
            require(receipt.get('width') == width and receipt.get('height') == height and
                    receipt.get('frames') == frames and receipt.get('provider') == provider and
                    receipt.get('layout') == layout and receipt.get('close_race') is (race == '1') and
                    receipt.get('trace_max_events') == event_limit, 'capture_parameter_mismatch')
            payload = line(source).split(' ')
            require(len(payload) == 2 and payload[0] == 'I' and payload[1] == rgb_sha256,
                    'trace_rgb_identity_mismatch')
            sizes = (3 * width * height, (min(height, 3) * width if layout == 'ring' else width * height), width * height)
            require(sum(sizes) <= 64 * 1024 * 1024, 'workspace_budget_limit')
            core = None if provider == 'direct' else load_core(core_path)
            workspace = ReplayWorkspace(core, provider, sizes)
            count = 0
            for expected in expected_events(width, height, frames, provider, layout, race == '1'):
                count += 1
                if deadline is not None and count % 4096 == 0 and time.monotonic() > deadline:
                    raise TimeoutError('replay_timeout')
                require(count <= event_limit and count <= MAX_EVENTS, 'trace_event_limit')
                fields = line(source).split(' ')
                require(len(fields) == 7 and fields[0] == 'E' and uint(fields[1]) == count and
                        fields[2] in ('M', 'W') and fields[3] in OPS, 'trace_event_syntax')
                actual = (fields[2], fields[3], uint(fields[4], 255), uint(fields[5]), uint(fields[6]))
                require(actual == expected, 'trace_event_sequence_or_range')
                actor, operation, index, offset, length = actual
                if index != 255:
                    require(index < 3 and offset <= sizes[index] and length <= sizes[index] - offset,
                            'trace_range_invalid')
                workspace.apply(actor, operation, index, offset, length, rgb, state)
            footer = line(source).split(' ')
            require(len(footer) == 7 and footer[:2] == ['END', 'COMPLETE'] and
                    uint(footer[2], MAX_EVENTS) == uint(footer[3], MAX_EVENTS) == count and
                    uint(footer[4]) == state['checksum'] == sum(expected_output) * frames and
                    uint(footer[5]) == state['converted'] == width * height * frames and
                    uint(footer[6]) == frames and source.read(1) == b'', 'trace_footer_mismatch')
            require(state['output'] == expected_output and not state['rgb'] and state['taps'] == state['sum'] == 0,
                    'replay_output_mismatch')
            require(workspace.destroyed if provider != 'direct' else all(item is None for item in workspace.direct),
                    'replay_resources_not_closed')
            require(source.digest.hexdigest() == before_trace, 'trace_consumed_identity_mismatch')
        require(file_hash(trace_path, MAX_TRACE, deadline) == before_trace and file_hash(input_path, MAX_INPUT, deadline) == before_input and
                file_hash(receipt_path, 16384, deadline) == receipt_hash and
                (before_core is None or file_hash(core_path, MAX_CORE, deadline) == before_core), 'replay_input_changed')
        return {'state': 'PASS_BOUND_TRACE_FUNCTIONAL_REPLAY', 'events': count, 'frames': frames,
                'provider': provider, 'layout': layout, 'close_race': race == '1',
                'trace_sha256': before_trace, 'input_sha256': before_input,
                'requested_core_file_sha256': before_core,
                'loaded_module_identity': 'NOT_APPLICABLE' if before_core is None else 'NOT_VERIFIED',
                'core_use': 'NOT_APPLICABLE' if before_core is None else 'REQUESTED_EXPLICIT_SHARED_CORE',
                'checksum': state['checksum'],
                'source_scope': 'INSTRUMENTED_SOURCE_SEMANTIC_OPERATIONS_ONLY',
                'replay_threads': 'SINGLE_THREAD_LOGICAL_ACTOR_ORDER_ONLY',
                'device': 'NOT_RUN', 'performance': 'NOT_MEASURED', 'tlb_mechanism': 'NOT_ESTABLISHED'}
    except Exception as error:
        cleanup = workspace.cleanup() if workspace is not None else 'NOT_STARTED'
        raise ReplayFailure(error, cleanup) from error
