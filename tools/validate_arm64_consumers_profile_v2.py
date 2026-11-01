#!/usr/bin/env python3
"""Run the finite v2 consumer functional suite after current device release.

3 core safety commands, then 32 DEX + 156 original image + 8 total-only image cases. This is
not a pilot or a performance experiment. Import and offline fixtures use no ADB.
Local ADB timeout leaves remote child state UNKNOWN: no kill or cleanup follows.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from time import monotonic as transport_clock

import freeze_arm64_input_profile as input_profile
from run_arm64_validation import Blocked, Failed, BUSY_PROCESS, CORE_CASES, digest, nonempty_evidence, parse_last_json, utc_now, verify_arm64_elf
from run_paired_device import PairedRunner, public_value, command_hash, RECEIPT_SNAPSHOT_SECONDS
from paired_analysis_v2 import strict_json
from verify_dex_borrow_v2 import CASES as DEX_CASES, VARIANTS as DEX_VARIANTS, INPUT_SHA256, METHODS, CHECKSUM
from verify_image_workspace_v2 import DIMENSIONS, VARIANTS as IMAGE_VARIANTS, model, oracle, rgb_fixture
from verify_image_perf_v2 import output_hash


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CASES = {"dex": 32, "image_workspace": 156, "image_perf": 8}
FIXED_BINARIES = {
    "libmgmem.so": "install/android-arm64-v8a/lib/libmgmem.so",
    "mgmem_dex_paired_v2": "build/dex-borrow-v2-arm64/mgmem_dex_paired_v2",
    "mgmem_dex_paired_v2_diagnostic": "build/dex-borrow-v2-arm64/mgmem_dex_paired_v2_diagnostic",
    "mgmem_image_paired": "build/image-v2-arm64/mgmem_image_paired",
    "mgmem_image_perf": "build/image-perf-v2-arm64/mgmem_image_perf",
    "mgmem_core_tests": "build/android-arm64-v8a/mgmem_core_tests",
    "mgmem_c_consumer": "build/android-arm64-v8a/mgmem_c_consumer",
    "mgmem_cpp_consumer": "build/android-arm64-v8a/mgmem_cpp_consumer",
}
CORE_PROGRAMS = ("mgmem_core_tests", "mgmem_c_consumer", "mgmem_cpp_consumer")
PROFILE_RUNTIME_SOURCES = ("validate_arm64_consumers_profile_v2.py", "run_arm64_validation.py", "run_paired_device.py",
                           "paired_analysis_v2.py", "verify_dex_borrow_v2.py", "verify_image_workspace_v2.py",
                           "verify_image_perf_v2.py")
OUTPUT_LIMIT = 4 * 1024 * 1024
TRANSPORT_STOP_WAIT_SECONDS = 1.0
PIPE_JOIN_WAIT_SECONDS = 0.5


def write_json(path, value):
    """Use a fresh exclusive pending file; never reuse an existing pending file."""
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    pending = path.with_name(path.name + "." + secrets.token_hex(16) + ".pending")
    with pending.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)


def bounded_adb(command, timeout):
    """Bound both pipes and the local transport; a remote child is never killed."""
    deadline = transport_clock() + timeout
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buffers, exceeded, errors = [bytearray(), bytearray()], [False, False], []
    abort = threading.Event()
    def drain(index, pipe):
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                room = OUTPUT_LIMIT - len(buffers[index])
                buffers[index].extend(chunk[:max(0, room)])
                if len(chunk) > room:
                    exceeded[index] = True
                    abort.set()
                    break
        except (OSError, ValueError):
            errors.append(True)
            abort.set()
        finally:
            pipe.close()
    threads = [threading.Thread(target=drain, args=(i, pipe), daemon=True)
               for i, pipe in enumerate((process.stdout, process.stderr))]
    for thread in threads:
        thread.start()
    timed_out, confirmed = False, False
    while True:
        remaining = deadline - transport_clock()
        if remaining <= 0:
            timed_out = True
            break
        if abort.is_set():
            break
        try:
            result = process.poll()
            if transport_clock() >= deadline:
                timed_out = True
                confirmed = result is not None
                break
            if result is not None:
                confirmed = True
                break
        except OSError:
            errors.append(True)
            abort.set()
            break
        remaining = deadline - transport_clock()
        if remaining <= 0:
            timed_out = True
            break
        abort.wait(min(remaining, 0.05))
    if not confirmed:
        try:
            process.kill()  # Only this local transport; device-side state is unknown.
        except OSError:
            pass
        try:
            process.wait(timeout=TRANSPORT_STOP_WAIT_SECONDS)
            confirmed = True
        except (subprocess.TimeoutExpired, OSError):
            pass
    for thread in threads:
        thread.join(timeout=PIPE_JOIN_WAIT_SECONDS)
    if any(thread.is_alive() for thread in threads):
        errors.append(True)
    if transport_clock() >= deadline:
        timed_out = True
    overflow, read_error = any(exceeded), bool(errors)
    # Never retain partial output from an aborted or unconfirmed transport.
    unsafe = timed_out or overflow or read_error or not confirmed
    return (process.returncode if confirmed and not unsafe else None,
            b"" if unsafe else bytes(buffers[0]), b"" if unsafe else bytes(buffers[1]),
            timed_out, overflow, read_error, "LOCAL_EXIT_CONFIRMED" if confirmed else "LOCAL_EXIT_UNCONFIRMED")


def parse_occupancy(processes, activity, allowed):
    require(processes.endswith("\n") and activity.endswith("\n"), "inventory_truncated", Blocked)
    lines = [line.strip() for line in processes.splitlines() if line.strip()]
    require(lines and re.fullmatch(r"PID\s+NAME\s+ARGS", lines[0]) is not None, "process_header_invalid", Blocked)
    rows = [line.split(None, 2) for line in lines[1:]]
    require(len(rows) >= 5 and all(len(row) >= 2 and row[0].isdigit() and row[1] for row in rows) and
            len({row[0] for row in rows}) == len(rows) and any(row[1] == "init" for row in rows) and
            any(row[1] == "system_server" for row in rows), "process_inventory_invalid", Blocked)
    require("ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)" in activity and
            "ACTIVITY MANAGER RUNNING PROCESSES (dumpsys activity processes)" in activity and
            not re.search(r"(?i)permission\s+denial|permission\s+denied|can.t find service|error dumping service", activity),
            "activity_inventory_invalid", Blocked)
    busy = [row for row in rows if BUSY_PROCESS.search(" ".join(row[1:])) and
            row[1] not in allowed and (len(row) < 3 or row[2] not in allowed)]
    active = []
    for line in activity.splitlines():
        if not re.search(r"(?i)activeinstrumentation", line):
            continue
        if re.search(r"\bmActiveInstrumentation\s*[:=]\s*(?:null|None|\[\]|\{\}|0)\s*$", line):
            continue
        components = re.findall(r"[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+", line)
        if "ActiveInstrumentation{" not in line or not components or not all(item in allowed for item in components):
            active.append(line)
    return {"parsed_processes": len(rows), "busy_count": len(busy), "blocked_instrumentation_count": len(active),
            "result": "PASS_OBSERVATIONAL_ONLY" if not busy and not active else "BLOCKED"}


def require(condition, reason, failure=Failed):
    if not condition:
        raise failure(reason)


def zero_integer(value):
    return type(value) is int and value == 0


def current_release(path, serial):
    """An expiry is mandatory for this new execution; old releases cannot recur."""
    try:
        value = strict_json(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise Blocked("owner_release_file_missing_or_invalid") from error
    require(isinstance(value, dict) and value.get("state") == "RELEASED_FOR_MEMORY",
            "current_owner_has_not_released_device", Blocked)
    require(nonempty_evidence(value.get("owner_evidence")), "owner_release_evidence_missing", Blocked)
    require(value.get("device_serial_sha256") == hashlib.sha256(serial.encode()).hexdigest(),
            "admission_device_identity_missing_or_different", Blocked)
    try:
        expiry = datetime.fromisoformat(value["expires_at_utc"].replace("Z", "+00:00"))
        require(expiry.tzinfo is not None and datetime.now(timezone.utc) < expiry,
                "owner_release_expired", Blocked)
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        raise Blocked("owner_release_needs_explicit_valid_expiry") from error
    return value


def dex_checks(native, variant, case, diagnostic, strict_contract=False):
    checks = {
        "schema_workload": type(native.get("schema_version")) is int and native["schema_version"] == 2 and
                           native.get("workload") == "dex_source_strategy_complete_owner_v2" and "phases" in native,
        "success": native.get("success") is True,
        "case": native.get("case") == case, "variant": native.get("variant") == variant,
        "source_final_released": native.get("source_final_released") is True,
        "balanced_callbacks": (type(native.get("callback_retains")) is int and
                               type(native.get("callback_releases_including_caller")) is int and
                               native["callback_releases_including_caller"] == native["callback_retains"] + 1)
                              if strict_contract else native.get("callback_releases_including_caller") == native.get("callback_retains", -9) + 1,
        "lease_final_correct": native.get("lease_final_correct") is True,
        "final_resources": native.get("final_resource_state") == "CLOSED_ZERO" and
                           type(native.get("closed_backing_bytes")) is int and native["closed_backing_bytes"] == 0,
        "unknown_resources_not_zero": all(key in native and native[key] is None for key in
                                          ("memory_metadata_bytes", "rss_pss_bytes", "energy", "provider_attempt_count")),
        "source_capacity": type(native.get("external_source_capacity_bytes")) is int and native["external_source_capacity_bytes"] >= 10540184,
        "unique_input_backing": native.get("unique_owned_backing_peak_bytes") == (0 if variant == "D-Z0" else 10540184),
        "retained": native.get("retained_before_collect_bytes") == (10540184 if variant == "D-C2" else 0),
        "build_kind": native.get("diagnostic") is diagnostic,
        "phase_policy": (native.get("phases") is None) is (not diagnostic),
        "expected_rejection_or_completion": "expected_status" in native and native.get("expected_status") == native.get("observed_status"),
    }
    if case not in ("invalid-lease", "corrupt-input", "borrow-budget"):
        checks.update({"source_digest": native.get("source_sha256") == INPUT_SHA256, "methods": native.get("method_count") == METHODS})
    if case in ("normal", "cross-thread"):
        checks.update({"complete_method_and_text_checksum": native.get("result_checksum") == CHECKSUM,
                       "result_outlives_snapshot": native.get("snapshot_released_before_last_read") is True})
    if case in ("cross-thread", "cancel-job"):
        checks["final_sdk_callback_on_other_thread"] = native.get("final_release_on_other_thread") is True
    if strict_contract:
        statuses = {"normal": 0, "cross-thread": 0, "invalid-lease": 1, "corrupt-input": 2,
                    "borrow-budget": 4, "query-closed": 7, "cancel-job": 9}
        expected = statuses.get(case)
        retains = 0 if case in ("invalid-lease", "borrow-budget") else 1
        checks["case_status_contract"] = expected is not None and all(
            type(native.get(key)) is int and native[key] == expected for key in ("expected_status", "observed_status"))
        checks["exact_callback_accounting"] = (
            type(native.get("callback_retains")) is int and native["callback_retains"] == retains and
            type(native.get("callback_releases_including_caller")) is int and
            native["callback_releases_including_caller"] == retains + 1)
        checks["resource_counters_nonnegative_integers"] = all(
            type(native.get(key)) is int and native[key] >= 0 for key in (
                "input_bytes", "method_count", "result_checksum", "logical_requested_bytes", "unique_owned_backing_peak_bytes",
                "external_source_capacity_bytes", "borrowed_view_peak_bytes", "source_plus_input_unique_capacity_bytes",
                "retained_before_collect_bytes", "reserved_final_bytes", "provider_published_count", "parser_peak_owned_bytes",
                "input_budget_bytes", "parser_budget_bytes", "closed_backing_bytes"))
        checks["final_reserved_zero"] = zero_integer(native.get("reserved_final_bytes"))
        checks["fixed_input_and_budget_contract"] = all(
            type(native.get(key)) is int and native[key] == value for key, value in {
                "input_bytes": 10540184, "logical_requested_bytes": 10540184,
                "input_budget_bytes": 32 * 1024 * 1024, "parser_budget_bytes": 256 * 1024 * 1024}.items())
    return checks


def core_checks(program, stdout):
    native = parse_last_json(stdout)
    if program == "mgmem_core_tests":
        cases = {line[5:].strip() for line in stdout.splitlines() if line.startswith("PASS ")}
        checks = {"complete_core_summary": native == {"suite": "mgmem-core-v1", "tests": 11, "checks": 145033, "status": "PASS"},
                  "all_named_core_cases": cases == CORE_CASES}
    elif program == "mgmem_c_consumer":
        checks = {"C_ABI_consumer": native.get("consumer") == "pure_c_abi" and native.get("success") is True,
                  "C_backing_closed_zero": type(native.get("backing_after_close")) is int and native["backing_after_close"] == 0}
    elif program == "mgmem_cpp_consumer":
        checks = {"complete_CPP_RAII_summary": native == {"suite": "mgmem-cpp-raii", "cases": 5, "status": "PASS"}}
    else:
        raise Failed("unexpected_core_program")
    return native, checks


def image_checks(native, spec):
    checks = {"closed": native.get("closed") is True, "cleanup_ok": native.get("cleanup_ok") is True,
              "closed_backing_zero": type(native.get("closed_backing_bytes")) is int and native["closed_backing_bytes"] == 0,
              "performance_not_qualified": native.get("performance_qualification") == "NOT_STARTED"}
    error = spec.get("expected_error")
    if error:
        checks.update({"expected_failure": native.get("success") is False,
                       "expected_error": error in native.get("error", "")})
    else:
        checks.update({"success": native.get("success") is True,
                       "all_frames_consumed": native.get("completed_frames") == spec["frames"],
                       "checksum_equal": native.get("checksum") == spec["checksum"] * spec["frames"],
                       "gray_converted_once": native.get("gray_conversions") == spec["width"] * spec["height"] * spec["frames"],
                       "provider_layout": native.get("provider") == spec["provider"] and native.get("layout") == spec["layout"]})
        checks.update({key + "_model_equal": native.get(key) == expected
                       for key, expected in model(spec["width"], spec["height"], spec["layout"], spec["provider"]).items()})
    if spec["provider"] != "direct" and (not error or error in ("injected worker exception", "cannot open output file")):
        checks["whole_allocation_exclusive"] = native.get("whole_allocation_exclusive") is True
        if "--close-race" in spec["extra"]:
            checks["close_with_admitted_worker"] = native.get("close_while_leased_verified") is True
    if not error:
        number_fields = ("completed_frames", "checksum", "gray_conversions", "closed_backing_bytes") + tuple(
            model(spec["width"], spec["height"], spec["layout"], spec["provider"]).keys())
        checks["resource_counters_nonnegative_integers"] = all(
            type(native.get(key)) is int and native[key] >= 0 for key in number_fields)
    return checks


def make_specs():
    """Pure bounded plan. Paths and fixture bodies are generated separately."""
    dex = [{"group": "dex", "id": f"dex-{v}-{case}", "variant": v, "case": case, "diagnostic": False}
           for v in DEX_VARIANTS for case in DEX_CASES]
    dex += [{"group": "dex", "id": f"dex-{v}-diagnostic", "variant": v, "case": "normal", "diagnostic": True} for v in DEX_VARIANTS]
    image = []
    for width, height in DIMENSIONS:
        for provider, layout in IMAGE_VARIANTS:
            for frames in ((1,) if width == 1920 else (1, 3)):
                image.append({"group": "image_workspace", "id": f"image-{width}x{height}-{provider}-{layout}-{frames}",
                              "width": width, "height": height, "provider": provider, "layout": layout,
                              "frames": frames, "extra": ["--close-race"], "input": f"image-{width}x{height}.ppm"})
    for provider, layout in IMAGE_VARIANTS:
        image.append({"group": "image_workspace", "id": f"standard-{provider}-{layout}", "width": 17, "height": 13,
                      "provider": provider, "layout": layout, "frames": 3, "extra": [], "input": "image-17x13.ppm"})
        for kind, expected_error, input_name, extra in (
            ("worker", "injected worker exception", "image-17x13.ppm", ["--close-race", "--fault-worker"]),
            ("output", "cannot open output file", "image-17x13.ppm", ["--close-race"]),
            ("truncated", "truncated input", "image-truncated.ppm", []),
            ("overflow", "size overflow", "image-overflow.ppm", [])):
            image.append({"group": "image_workspace", "id": f"{kind}-{provider}-{layout}", "provider": provider,
                          "layout": layout, "frames": 1, "extra": extra, "input": input_name,
                          "expected_error": expected_error, "output_is_directory": kind == "output"})
    perf = [{"group": "image_perf", "id": f"perf-{provider}-{layout}-{'capture' if capture else 'formal'}",
             "provider": provider, "layout": layout, "capture": capture, "frames": 3, "input": "image-1920x1080.ppm"}
            for provider, layout in (("direct", "full"), ("direct", "ring"), ("system", "ring"), ("arena", "ring"))
            for capture in (True, False)]
    specs = dex + image + perf
    require({name: sum(spec["group"] == name for spec in specs) for name in EXPECTED_CASES} == EXPECTED_CASES,
            "internal_functional_plan_count_mismatch")
    return specs


class ConsumerRunner(PairedRunner):
    def __init__(self, args, output):
        args.phase = "functional"
        args.input = args.dex_input
        super().__init__(args, output)
        self.protocol = {"limits": {"phase_max_wall_seconds": args.overall_timeout}}
        self.receipt.update({"schema_version": 2, "scope": "ARM64_V2_CONSUMERS_FUNCTIONAL_ONLY_NOT_PERFORMANCE",
                             "expected_cases": EXPECTED_CASES, "completed_cases": {}, "tests": {},
                             "core_safety": {"status": "NOT_RUN", "tests": 0, "results": {}},
                             "qualification": {"functional": "NOT_RUN", "performance": "NOT_STARTED",
                                               "BIL_integration": "NOT_SELECTED", "16KB": "NOT_OBSERVED",
                                               "MTE_HWASAN_RSS_energy_PMU_PFN": "NOT_QUALIFIED"}})
        self.cache = ROOT / "build" / ("arm64-consumers-v2-" + secrets.token_hex(6))
        self.cache.mkdir(parents=True, exist_ok=False)
        self.assets, self.oracles = {}, {}
        self.input_profile = getattr(args, "input_profile", None)
        self.input_profile_sha256 = getattr(args, "input_profile_sha256", None)
        require((self.input_profile is None) == (self.input_profile_sha256 is None), "profile_and_expected_sha_must_be_paired", Blocked)
        self.profile_snapshot = None
        self.profile_runtime_sources = None
        self.input_binding = None
        if self.input_profile is not None:
            require(output.is_relative_to(input_profile.build_root()), "profile_receipt_must_be_private_under_build", Blocked)
            self.receipt["scope"] = "ARM64_V2_PROFILE_CONSUMERS_FUNCTIONAL_ONLY_NOT_PERFORMANCE"
            self.receipt["profile_checks"] = {"policy": "PRIVATE_PHASE_CHECKPOINTS_NOT_CONTINUOUS",
                                               "performance_freeze_integration": "NOT_IMPLEMENTED"}

    def profile_checkpoint(self):
        if self.input_profile is None:
            return
        try:
            selected = input_profile.validate(self.input_profile, self.input_profile_sha256)
            runtime_sources = {}
            source_names = {"tools/" + name for name in PROFILE_RUNTIME_SOURCES} | set(input_profile.HELPERS)
            for name in sorted(source_names):
                _, data = input_profile.regular(ROOT / name, ROOT)
                runtime_sources[name] = input_profile.record(data)
            tool_paths = {"adb": self.args.adb.resolve(), "python": Path(sys.executable).resolve()}
            runtime_tools = {name: input_profile.external_file(path) for name, path in tool_paths.items()}
            tool_manifest = {"schema_version": 1, "tools": {
                name: {"path": str(tool_paths[name]), **runtime_tools[name]} for name in sorted(tool_paths)}}
            manifest_path = self.root_output / "private_runtime_tools.json"
            if self.input_binding is None:
                require(not os.path.lexists(manifest_path), "private_tool_manifest_already_exists", Blocked)
                write_json(manifest_path, tool_manifest)
            _, manifest_bytes = input_profile.regular(manifest_path, input_profile.build_root())
            require(strict_json(manifest_bytes.decode("utf-8")) == tool_manifest,
                    "private_tool_manifest_changed", Blocked)
            require(re.fullmatch(r"[0-9a-f]{64}", self.input_profile_sha256 or "") is not None and
                    re.fullmatch(r"[0-9a-f]{32}", selected.get("profile_id", "")) is not None,
                    "profile_identity_invalid", Blocked)
            binding = {"schema_version": 1, "profile_sha256": self.input_profile_sha256,
                       "profile_id": selected["profile_id"], "runtime_sources": runtime_sources,
                       "runtime_tools": runtime_tools,
                       "runtime_tools_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                       "profile_toolchain_entries": selected["snapshot"]["toolchain_entries"],
                       "identity_scope": "PRIVATE_PRE_ADB_AND_PHASE_CHECKPOINTS_NOT_LOADED_IMAGE_ATTESTATION"}
            require(binding["profile_toolchain_entries"] and all(
                isinstance(value, dict) and type(value.get("bytes")) is int and value["bytes"] > 0 and
                isinstance(value.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
                for value in binding["profile_toolchain_entries"].values()), "profile_toolchain_identity_invalid", Blocked)
            if self.input_binding is None:
                self.input_binding = copy.deepcopy(binding)
                self.receipt["input_binding"] = copy.deepcopy(binding)
            require(binding == self.input_binding and self.receipt.get("input_binding") == self.input_binding,
                    "profile_runtime_identity_changed", Blocked)
            if self.profile_runtime_sources is None:
                self.profile_runtime_sources = runtime_sources
            require(runtime_sources == self.profile_runtime_sources, "profile_runtime_source_changed", Blocked)
            if self.profile_snapshot is None:
                self.profile_snapshot = selected
            require(selected == self.profile_snapshot, "profile_snapshot_changed", Blocked)
            if self.assets and "deployment_manifest" in self.receipt:
                for item in self.receipt["deployment_manifest"]:
                    self.check_asset_identity(item)
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise Blocked("private_input_profile_validation_failed") from error

    def check_asset_identity(self, item):
        path = self.assets[item["name"]]
        if item["name"] in self.binary_names:
            _, data = input_profile.regular(path, input_profile.build_root())
            current = input_profile.record(data)
            require(current == self.profile_role_identities[item["name"]], "profile_role_drift:" + item["name"], Blocked)
            expected_role = next(role for role, name in input_profile.ROLES.items() if name == item["name"])
            require(item.get("role") == expected_role, "profile_role_receipt_mismatch:" + item["name"], Blocked)
        else:
            _, data = input_profile.regular(path, input_profile.build_root())
            current = input_profile.record(data)
            if item["name"] in ("input.dex", "image-1920x1080.ppm"):
                require(item.get("private_path") == path.relative_to(ROOT).as_posix(),
                        "private_input_receipt_mismatch:" + item["name"], Blocked)
        require(current == {"bytes": item["bytes"], "sha256": item["sha256"]},
                "profile_asset_drift:" + item["name"], Blocked)

    def redact(self, value):
        value = super().redact(value)
        if isinstance(value, str):
            # Public command output retains bytes hashes, not local machine paths.
            return re.sub(r"[A-Za-z]:[\\/][^\r\n]*", "<redacted-local-path>", value)
        return value

    def save(self, force=False):
        commands = self.receipt.get("commands", [])
        if commands:
            command = self.redact(public_value(commands[-1]))
            record_id = command["record_id"]
            digest_value = command_hash(command)
            signature = (record_id, digest_value)
            if signature != self._journal_signature:
                revision = self._journal_revisions.get(record_id, 0) + 1
                event = {"schema_version": 1, "event": "command_revision", "recorded_at_utc": utc_now(),
                         "record_id": record_id, "revision": revision, "command_sha256": digest_value, "command": command}
                with (self.root_output / (self.phase + "-commands.jsonl")).open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                self._journal_signature = signature
                self._journal_revisions[record_id] = revision
                self._journal_events += 1
        self.receipt["command_journal"] = {"path": self.phase + "-commands.jsonl", "events": self._journal_events,
                                           "durability": "Private journal fsynced before snapshot.",
                                           "snapshot_interval_seconds": RECEIPT_SNAPSHOT_SECONDS}
        now, state = time.monotonic(), self.receipt.get("state")
        if force or self._last_snapshot_at is None or state != self._last_snapshot_state or now - self._last_snapshot_at >= RECEIPT_SNAPSHOT_SECONDS:
            write_json(self.root_output / (self.phase + "-receipt.json"), self.redact(public_value(self.receipt)))
            self._last_snapshot_at, self._last_snapshot_state = time.monotonic(), state

    def occupancy(self, phase, allowed):
        processes = self.command(phase + "-processes", ["shell", "ps", "-A", "-o", "PID,NAME,ARGS"], capture_private_inventory=True)
        self.require_command(processes, phase + "_process_inventory_unavailable")
        instrument = self.command(phase + "-instrumentation", ["shell", "dumpsys", "activity"], capture_private_inventory=True)
        self.require_command(instrument, phase + "_instrumentation_inventory_unavailable")
        result = parse_occupancy(processes["_inventory_stdout"], instrument["_inventory_stdout"], allowed)
        self.receipt[phase + "_occupancy"] = {**result, "checked_at_utc": utc_now()}
        self.save()
        require(result["result"] == "PASS_OBSERVATIONAL_ONLY", phase + "_other_test_or_diagnostic_owner_still_present", Blocked)

    def command(self, name, arguments, timeout=20, hash_stdout=False, capture_private_inventory=False):
        """Same PairedRunner durable journal, plus exact byte hashes and clocks."""
        if hasattr(self, "admission_sha"):
            self.admission_gate()
        effective_timeout = min(timeout, self.remaining())
        self.counter += 1
        command = [str(self.args.adb), "-s", self.args.serial] + arguments
        begin = time.monotonic_ns()
        timed_out, launch_error, overflow, read_error, transport = False, None, False, False, "NOT_LAUNCHED"
        try:
            code, stdout_bytes, stderr_bytes, timed_out, overflow, read_error, transport = bounded_adb(command, effective_timeout)
            if timed_out or overflow or read_error or transport != "LOCAL_EXIT_CONFIRMED":
                self.receipt["remote_process_state_after_abort"] = "UNKNOWN_DO_NOT_ASSUME_STOPPED"
        except OSError:
            code, stdout_bytes, stderr_bytes, launch_error = None, b"", b"", "LOCAL_ADB_LAUNCH_FAILED"
        end = time.monotonic_ns()
        stdout, stderr = stdout_bytes.decode("utf-8", "replace"), stderr_bytes.decode("utf-8", "replace")
        public_stdout, public_stderr = self.redact(stdout), self.redact(stderr)
        boot_hash = hashlib.sha256(stdout.strip().encode()).hexdigest() if hash_stdout else None
        if hash_stdout:
            public_stdout = "sha256:" + boot_hash + "\n"
        if capture_private_inventory:
            public_stdout = "private_inventory_sha256:" + hashlib.sha256(stdout_bytes).hexdigest() + "\n"
            public_stderr = "private_stderr_sha256:" + hashlib.sha256(stderr_bytes).hexdigest() + "\n"
        prefix = f"{self.counter:04d}-{name}"
        row = {"record_id": self.counter, "name": name, "command": self.redact(command),
               "timeout_seconds": effective_timeout, "requested_timeout_seconds": timeout,
               "exit_code": code, "timed_out": timed_out,
               "output_limit_exceeded": overflow, "pipe_read_error": read_error,
               "local_transport_state": transport,
               "launch_error": self.redact(launch_error), "stdout": public_stdout, "stderr": public_stderr,
               "raw_stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(), "raw_stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
               "host_begin_monotonic_ns": begin, "host_end_monotonic_ns": end,
               "diagnostic_runner_elapsed_seconds": (end-begin)/1e9,
               "stdout_file": prefix + ".stdout.txt", "stderr_file": prefix + ".stderr.txt"}
        if hash_stdout:
            row.update({"stdout_value_sha256": boot_hash, "stdout_value_is_boot_uuid": bool(re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", stdout.strip()))})
        if capture_private_inventory:
            row["capture_policy"] = "RAW_IN_MEMORY_ONLY_FILTERED_PUBLIC_OUTPUT"
        with (self.output / row["stdout_file"]).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(public_stdout)
        with (self.output / row["stderr_file"]).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(public_stderr)
        self.receipt["commands"].append(row)
        self.save()
        require(not overflow and not read_error and transport in ("LOCAL_EXIT_CONFIRMED", "NOT_LAUNCHED"),
                "adb_output_exceeded_limit_or_pipe_failed_or_transport_unknown", Blocked)
        if capture_private_inventory:
            return {**row, "_inventory_stdout": stdout, "_inventory_stderr": stderr}
        return row

    def admission_gate(self, first=False):
        value = current_release(self.args.admission, self.args.serial)
        if first:
            idle = value.get("explicitly_idle_processes", [])
            require(isinstance(idle, list), "idle_exemptions_must_be_array", Blocked)
            allowed = {}
            for item in idle:
                require(isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"] and
                        item["name"] not in allowed and nonempty_evidence(item.get("evidence")),
                        "idle_exemption_needs_unique_name_and_owner_evidence", Blocked)
                allowed[item["name"]] = True
            self.allowed = allowed
            self.admission_sha = digest(self.args.admission)
            evidence = json.dumps(value["owner_evidence"], sort_keys=True, ensure_ascii=False).encode()
            self.receipt["admission"] = {"file_name": self.args.admission.name, "sha256": self.admission_sha,
                                         "state": "RELEASED_FOR_MEMORY", "expires_at_utc": value["expires_at_utc"],
                                         "owner_evidence_sha256": hashlib.sha256(evidence).hexdigest(),
                                         "explicitly_idle_names": sorted(allowed),
                                         "policy": "Evidence body remains in local admission; identity only is public."}
            self.save(force=True)
        else:
            require(digest(self.args.admission) == self.admission_sha, "owner_admission_changed_during_functional_run", Blocked)

    def boot_gate(self, label):
        value = self.boot_hash(label)
        require(value == self.receipt["device"]["boot_id_sha256"], "device_rebooted_during_functional_run")

    def snapshot_dex_input(self):
        _, dex_data = input_profile.regular(self.args.dex_input, ROOT)
        require(len(dex_data) == 10540184 and hashlib.sha256(dex_data).hexdigest() == INPUT_SHA256,
                "real_DEX_input_identity_mismatch", Blocked)
        dex_path = self.cache / "input.dex"
        with dex_path.open("xb") as stream:
            stream.write(dex_data)
        return dex_path

    def local_assets(self):
        self.profile_checkpoint()
        paths = dict(FIXED_BINARIES)
        self.profile_role_identities = {}
        if self.input_profile is not None:
            roles = self.profile_snapshot["snapshot"]["roles"]
            by_role = {item["role"]: item for item in roles}
            require(len(by_role) == len(roles) and all(role in by_role and by_role[role]["name"] == name
                    for role, name in input_profile.ROLES.items()), "profile_roles_invalid", Blocked)
            paths = {name: by_role[role]["path"] for role, name in input_profile.ROLES.items()}
            self.profile_role_identities = {name: {"bytes": by_role[role]["bytes"], "sha256": by_role[role]["sha256"]}
                                            for role, name in input_profile.ROLES.items()}
            require(set(paths) == set(FIXED_BINARIES), "profile_role_name_mismatch", Blocked)
        for name, relative in paths.items():
            path = ROOT / relative
            require(path.is_file(), "required_binary_missing:" + name, Blocked)
            verify_arm64_elf(path)
            self.assets[name] = path
        self.binary_names = tuple(paths)
        self.assets["input.dex"] = self.snapshot_dex_input()
        for width, height in DIMENSIONS:
            name = f"image-{width}x{height}.ppm"
            rgb = rgb_fixture(width, height)
            path = self.cache / name
            path.write_bytes(f"P6\n{width} {height}\n255\n".encode() + rgb)
            pgm, checksum = oracle(width, height, rgb)
            self.assets[name] = path
            self.oracles[name] = {"sha256": hashlib.sha256(pgm).hexdigest(), "checksum": checksum,
                                  "output_hash_fnv64": output_hash(pgm, 3), "width": width, "height": height}
        for name, body in (("image-truncated.ppm", b"P6\n2 2\n255\n\0"),
                           ("image-overflow.ppm", b"P6\n18446744073709551615 2\n255\n")):
            path = self.cache / name
            path.write_bytes(body)
            self.assets[name] = path
        self.receipt["deployment_manifest"] = [{"name": name, "bytes": path.stat().st_size, "sha256": digest(path)}
                                                 for name, path in self.assets.items()]
        roles_by_name = {name: role for role, name in input_profile.ROLES.items()}
        for item in self.receipt["deployment_manifest"]:
            if item["name"] in roles_by_name:
                item["role"] = roles_by_name[item["name"]]
            if item["name"] in ("input.dex", "image-1920x1080.ppm"):
                item["private_path"] = self.assets[item["name"]].relative_to(ROOT).as_posix()
        self.receipt["oracles"] = self.oracles
        if self.input_profile is not None:
            for item in self.receipt["deployment_manifest"]:
                self.check_asset_identity(item)

    def profile(self):
        metadata = {}
        for key, argv in {"model": ["getprop", "ro.product.model"], "api": ["getprop", "ro.build.version.sdk"],
                          "abi_list": ["getprop", "ro.product.cpu.abilist"], "kernel": ["uname", "-r"],
                          "page_bytes": ["getconf", "PAGESIZE"],
                          "allowed_cpu_list": ["grep", "Cpus_allowed_list", "/proc/self/status"]}.items():
            row = self.checked("profile-" + key.replace("_", "-"), ["shell"] + argv)
            metadata[key] = row["stdout"].strip()
        require("arm64-v8a" in metadata["abi_list"].split(","), "device_is_not_arm64", Blocked)
        require(metadata["api"].isdigit() and metadata["page_bytes"].isdigit(), "profile_API_or_page_unavailable", Blocked)
        metadata["api"], metadata["page_bytes"] = int(metadata["api"]), int(metadata["page_bytes"])
        require(metadata["api"] >= 27, "device_API_below_selected_binary_minimum_27", Blocked)
        allowed = metadata["allowed_cpu_list"].split(":", 1)
        require(len(allowed) == 2 and re.fullmatch(r"[0-9,-]+", allowed[1].strip()), "allowed_CPU_profile_unparsed", Blocked)
        cpus = set()
        for span in allowed[1].strip().split(","):
            bounds = list(map(int, span.split("-")))
            require(len(bounds) in (1, 2) and 0 <= bounds[0] <= bounds[-1] <= 65535, "allowed_CPU_range_invalid", Blocked)
            cpus.update(range(bounds[0], bounds[-1] + 1))
        require(self.args.cpu in cpus, "requested_CPU_not_allowed", Blocked)
        metadata["allowed_cpu_list"] = allowed[1].strip()
        metadata["requested_cpu"] = self.args.cpu
        metadata["boot_id_sha256"] = self.boot_hash("boot-before")
        self.receipt["device"] = metadata
        self.save(force=True)

    def stage(self):
        self.profile_checkpoint()
        self.checked("create-owned-directory", ["shell", "mkdir", self.remote])
        self.receipt["device_directory_created"] = True
        for item in self.receipt["deployment_manifest"]:
            name = item["name"]
            if self.input_profile is not None:
                self.check_asset_identity(item)
            self.checked("push-" + name.replace(".", "-"), ["push", str(self.assets[name]), self.remote + "/" + name], timeout=60)
            row = self.checked("hash-" + name.replace(".", "-"), ["shell", "sha256sum", self.remote + "/" + name])
            actual = row["stdout"].split()[0] if row["stdout"].split() else ""
            require(actual == item["sha256"], "remote_stage_hash_mismatch:" + name)
            if self.input_profile is not None:
                self.check_asset_identity(item)
            item["device_sha256"] = actual
        self.checked("set-owned-executable-bits", ["shell", "chmod", "700"] +
                     [self.remote + "/" + name for name in self.binary_names if name != "libmgmem.so"])
        self.checked("create-output-failure-directory", ["shell", "mkdir", self.remote + "/output-is-directory"])
        self.boot_gate("boot-after-stage")
        self.profile_checkpoint()

    def device_asset_checkpoint(self):
        for item in self.receipt["deployment_manifest"]:
            row = self.checked("recheck-" + item["name"].replace(".", "-"),
                               ["shell", "sha256sum", self.remote + "/" + item["name"]])
            values = row["stdout"].split()
            require(values and values[0] == item["sha256"], "remote_asset_drift:" + item["name"], Blocked)

    def verify_pgm(self, case_id, remote, expected):
        local = self.cache / (case_id + ".pgm")
        self.checked(case_id + "-pull-pgm", ["pull", remote, str(local)])
        actual = digest(local)
        require(actual == expected, "complete_PGM_oracle_mismatch:" + case_id)
        return {"expected_pgm_sha256": expected, "actual_pgm_sha256": actual, "body_policy": "LOCAL_BUILD_CACHE_ONLY"}

    def core_safety(self):
        """Fresh execution on this admitted device, not prior PMA110 evidence."""
        for program in CORE_PROGRAMS:
            row = {"schema_version": 2, "scope": "FUNCTIONAL_NOT_PERFORMANCE", "group": "core_safety",
                   "case_id": program, "success": False, "native": None, "checks": {}, "started_at_utc": utc_now()}
            try:
                command = self.command("core-" + program, ["shell", "LD_LIBRARY_PATH=" + self.remote, self.remote + "/" + program],
                                       timeout=self.args.task_timeout)
                row["command_result"] = command
                require(command["exit_code"] == 0 and not command["timed_out"] and command["launch_error"] is None and
                        not command["stderr"].strip(), "fresh_core_command_failed:" + program)
                native, checks = core_checks(program, command["stdout"])
                row.update({"native": native, "checks": checks, "success": all(checks.values())})
                require(row["success"], "fresh_core_contract_failed:" + program)
                self.receipt["core_safety"]["results"][program] = {"native": native, "checks": checks, "status": "PASS"}
                self.receipt["core_safety"]["tests"] += 1
            except Exception as error:
                row["failure_reason"] = str(error)
                raise
            finally:
                row["completed_at_utc"] = utc_now()
                self.append(row)
        self.receipt["core_safety"]["status"] = "PASS"
        self.admission_gate()
        self.occupancy("after-core-safety", self.allowed)
        self.boot_gate("boot-after-core-safety")
        self.save(force=True)

    def one(self, spec):
        self.admission_gate()
        row = {"schema_version": 2, "scope": "FUNCTIONAL_NOT_PERFORMANCE", "group": spec["group"],
               "case_id": spec["id"], "success": False, "native": None, "checks": {}, "started_at_utc": utc_now()}
        command = None
        try:
            if spec["group"] == "dex":
                executable = "mgmem_dex_paired_v2_diagnostic" if spec["diagnostic"] else "mgmem_dex_paired_v2"
                argv = [self.remote + "/input.dex", "--variant", spec["variant"], "--case", spec["case"], "--cpu", str(self.args.cpu)]
            else:
                output = self.remote + "/" + spec["id"] + ".pgm"
                if spec.get("output_is_directory"):
                    output = self.remote + "/output-is-directory"
                argv = [self.remote + "/" + spec["input"]]
                executable = "mgmem_image_paired" if spec["group"] == "image_workspace" else "mgmem_image_perf"
                if spec["group"] == "image_workspace":
                    argv += [output]
                argv += ["--provider", spec["provider"], "--layout", spec["layout"], "--frames", str(spec["frames"])]
                if spec["group"] == "image_workspace":
                    argv += spec["extra"]
                else:
                    argv += ["--cpu", str(self.args.cpu)]
                    if spec["capture"]:
                        argv += ["--verify-output", output]
            command = self.command(spec["id"] + "-native", ["shell", "LD_LIBRARY_PATH=" + self.remote, self.remote + "/" + executable] + argv,
                                   timeout=self.args.task_timeout)
            row["command_result"] = command
            require(not command["timed_out"] and command["launch_error"] is None, "native_timeout_or_launch_failure:" + spec["id"])
            expected_code = 1 if spec.get("expected_error") else 0
            require(command["exit_code"] == expected_code and not command["stderr"].strip(), "native_exit_or_stderr_failure:" + spec["id"])
            native = strict_json(command["stdout"])
            require(isinstance(native, dict), "native_output_not_object")
            row["native"] = native
            if spec["group"] == "dex":
                checks = dex_checks(native, spec["variant"], spec["case"], spec["diagnostic"],
                                    strict_contract=self.input_profile is not None)
                checks["CPU_affinity"] = all(type(native.get(k)) is int and native[k] == self.args.cpu for k in ("selected_cpu", "cpu_before", "cpu_after"))
            elif spec["group"] == "image_workspace":
                expected = self.oracles.get(spec["input"])
                if expected:
                    spec = {**spec, "checksum": expected["checksum"]}
                checks = image_checks(native, spec)
                if not spec.get("expected_error"):
                    row["output_oracle"] = self.verify_pgm(spec["id"], output, expected["sha256"])
                    checks["complete_PGM_byte_oracle"] = True
                else:
                    absence = self.command(spec["id"] + "-no-output", ["shell", "test", "!", "-f", output])
                    checks["no_output_created"] = absence["exit_code"] == 0 and not absence["timed_out"]
            else:
                expected = self.oracles[spec["input"]]
                resource_model = model(expected["width"], expected["height"], spec["layout"], spec["provider"])
                checks = {"success": native.get("success") is True, "normal_case": native.get("case") == "normal",
                          "schema_workload": type(native.get("schema_version")) is int and native["schema_version"] == 2 and
                                             native.get("workload") == "image_workspace_complete_owner_v2",
                          "native_variant": native.get("variant") == "I-" + {"direct": "D", "system": "S", "arena": "A"}[spec["provider"]] + ("F" if spec["layout"] == "full" else "R"),
                          "input_sha": native.get("source_sha256") == digest(self.assets[spec["input"]]),
                          "input_size": native.get("input_bytes") == self.assets[spec["input"]].stat().st_size,
                          "provider_layout": native.get("provider") == spec["provider"] and native.get("layout") == spec["layout"],
                          "dimensions": native.get("width") == expected["width"] and native.get("height") == expected["height"],
                          "complete_frames": native.get("completed_frames") == native.get("frames") == 3,
                          "all_pixel_sum": native.get("result_checksum") == expected["checksum"] * 3,
                          "complete_output_digest": native.get("output_hash_fnv64") == expected["output_hash_fnv64"],
                          "gray_once": native.get("gray_conversions") == expected["width"] * expected["height"] * 3,
                          "diagnostic_mode": native.get("diagnostic") is spec["capture"], "no_phases": native.get("phases") is None,
                          "source_final_release": native.get("source_final_released") is True,
                          "lease_lifetime": native.get("lease_final_correct") is True,
                          "closed_zero": native.get("final_resource_state") == "CLOSED_ZERO" and type(native.get("closed_backing_bytes")) is int and native["closed_backing_bytes"] == 0,
                          "final_reserved_zero": zero_integer(native.get("reserved_final_bytes")),
                          "positive_total": type(native.get("total_ns")) is int and native["total_ns"] > 0,
                          "payload_model": native.get("logical_requested_bytes") == resource_model["logical_payload_bytes"],
                          "backing_model": native.get("unique_owned_backing_peak_bytes") == resource_model["peak_unique_owned_backing_bytes"],
                          "provider_model": native.get("provider_allocation_count") == resource_model["provider_allocation_count"],
                          "unknowns_not_zero": all(key in native and native[key] is None for key in ("arena_used_bytes", "input_backing_padding_bytes", "memory_metadata_bytes", "rss_pss_bytes", "energy")),
                          "actual_steady_calls": native.get("steady_state_provider_allocations") == 0,
                          "CPU_affinity": all(type(native.get(k)) is int and native[k] == self.args.cpu for k in ("cpu_requested", "cpu_before", "cpu_after")),
                          "resource_counters_nonnegative_integers": all(type(native.get(k)) is int and native[k] >= 0 for k in (
                              "input_bytes", "width", "height", "completed_frames", "frames", "result_checksum",
                              "output_hash_fnv64", "gray_conversions", "logical_requested_bytes", "unique_owned_backing_peak_bytes",
                              "provider_allocation_count", "steady_state_provider_allocations", "closed_backing_bytes"))}
                if spec["capture"]:
                    row["output_oracle"] = self.verify_pgm(spec["id"], output, expected["sha256"])
                    checks["complete_PGM_byte_oracle"] = True
            row["checks"] = checks
            row["success"] = all(value is True for value in checks.values())
            require(row["success"], "functional_contract_failed:" + spec["id"])
        except Exception as error:
            row["failure_reason"] = str(error)
            raise
        finally:
            row["completed_at_utc"] = utc_now()
            self.append(row)
            if row["success"]:
                counts = self.receipt["completed_cases"]
                counts[spec["group"]] = counts.get(spec["group"], 0) + 1
            self.save()

    def run(self):
        self.admission_gate(first=True)  # Before the first ADB invocation.
        require(self.args.adb.is_file(), "ADB_executable_missing", Blocked)
        self.local_assets()
        self.occupancy("before", self.allowed)
        self.profile()
        self.stage()
        self.core_safety()
        self.device_asset_checkpoint()
        self.profile_checkpoint()
        previous = None
        for spec in make_specs():
            if previous is not None and previous != spec["group"]:
                self.admission_gate()
                self.occupancy("after-" + previous.replace("_", "-"), self.allowed)
                self.boot_gate("boot-after-" + previous.replace("_", "-"))
                self.device_asset_checkpoint()
                self.profile_checkpoint()
            self.one(spec)
            previous = spec["group"]
        self.admission_gate()
        self.occupancy("after", self.allowed)
        self.boot_gate("boot-after")
        self.device_asset_checkpoint()
        self.profile_checkpoint()
        require(self.receipt["completed_cases"] == EXPECTED_CASES, "functional_case_count_incomplete")
        require(self.input_binding is not None and self.receipt.get("input_binding") == self.input_binding,
                "profile_runtime_identity_missing_or_changed", Blocked)
        deployment = {"schema_version": 2, "state": "FUNCTIONAL_STAGED_IDENTITY_ONLY", "device_directory": self.remote,
                      "device_serial_sha256": self.receipt["device_serial_sha256"], "profile": self.receipt["device"],
                      "input_binding": self.input_binding,
                      "artifacts": self.receipt["deployment_manifest"],
                      "reuse_rule": "Performance capture independently stages and hashes its own directory; this is functional provenance only."}
        write_json(self.root_output / "deployment.json", deployment)
        self.profile_checkpoint()
        self.receipt.update({"state": "PASS", "all_passed": True})
        self.receipt["qualification"]["functional"] = "PASS_RECORDED_DEVICE_BOOT_PAGE_ARTIFACTS_ONLY"
        self.receipt["qualification"]["16KB"] = "ACTUAL_16KB_NATIVE_EXECUTION_SUBSET" if self.receipt["device"]["page_bytes"] == 16384 else "NOT_OBSERVED_ON_CURRENT_PAGE_SIZE"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--dex-input", type=Path, default=ROOT / "artifacts/local/perf-inputs/bilibili-9.17.0-classes.dex")
    parser.add_argument("--cpu", type=int, default=2)
    parser.add_argument("--task-timeout", type=int, default=30)
    parser.add_argument("--overall-timeout", type=int, default=600)
    parser.add_argument("--input-profile", type=Path, required=True)
    parser.add_argument("--input-profile-sha256", required=True, help="Independently held expected SHA-256; keep private.")
    args = parser.parse_args()
    require((args.input_profile is None) == (args.input_profile_sha256 is None), "profile_and_expected_sha_must_be_paired", Blocked)
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", args.serial) is not None, "invalid_device_identifier", Blocked)
    require(0 <= args.cpu <= 255 and 1 <= args.task_timeout <= 60 and 60 <= args.overall_timeout <= 1200,
            "invalid_bounded_limits", Blocked)
    if args.input_profile is not None:
        output = input_profile.confined(args.out_dir, input_profile.build_root(), exists=False)
    else:
        output = args.out_dir.resolve()
    require(output.is_relative_to(ROOT) and not output.exists(), "output_must_be_new_directory_inside_workspace", Blocked)
    output.mkdir(parents=True)
    args.adb, args.admission = args.adb.resolve(), args.admission.resolve()
    runner = ConsumerRunner(args, output)
    code = 1
    try:
        runner.run()
        code = 0
    except Blocked as error:
        runner.receipt.update({"state": "BLOCKED", "reason": str(error) if re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", str(error)) else "PRIVATE_VALIDATION_BLOCKED"})
        code = 2
    except (Failed, OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        runner.receipt.update({"state": "FAIL", "reason": str(error) if re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", str(error)) else "PRIVATE_VALIDATION_FAILED"})
    except KeyboardInterrupt:
        runner.receipt.update({"state": "INTERRUPTED_INCOMPLETE", "reason": "operator_interrupt_remote_state_not_assumed_stopped"})
    finally:
        if code != 0:
            runner.receipt["all_passed"] = False
            runner.receipt["qualification"]["functional"] = "INCOMPLETE"
            runner.receipt["qualification"]["16KB"] = "NOT_QUALIFIED"
        runner.receipt["completed_at_utc"] = utc_now()
        try:
            runner.save(force=True)
        except (OSError, ValueError, RuntimeError, TypeError):
            code = 1
            runner.receipt.update({"state": "FAIL", "reason": "PRIVATE_RECEIPT_WRITE_FAILED", "all_passed": False})
            runner.receipt["qualification"]["functional"] = "INCOMPLETE"
            runner.receipt["qualification"]["16KB"] = "NOT_QUALIFIED"
    print(json.dumps({"state": runner.receipt["state"], "all_passed": runner.receipt["all_passed"],
                      "performance_qualified": False}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    try:
        exit_code = main()
    except (Blocked, Failed, OSError, ValueError, RuntimeError, KeyError, TypeError):
        print(json.dumps({"state": "INCOMPLETE", "reason": "PRIVATE_SETUP_FAILED",
                          "all_passed": False, "performance_qualified": False}))
        exit_code = 2
    raise SystemExit(exit_code)
