#!/usr/bin/env python3
"""Run the finite v2 consumer functional suite after current device release.

3 core safety commands, then 32 DEX + 156 original image + 8 total-only image cases. This is
not a pilot or a performance experiment. Import and offline fixtures use no ADB.
Local ADB timeout leaves remote child state UNKNOWN: no kill or cleanup follows.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import time

from run_arm64_validation import Blocked, Failed, CORE_CASES, digest, nonempty_evidence, parse_last_json, utc_now, verify_arm64_elf
from run_paired_device import PairedRunner, public_value, write_json
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


def require(condition, reason, failure=Failed):
    if not condition:
        raise failure(reason)


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


def dex_checks(native, variant, case, diagnostic):
    checks = {
        "schema_workload": type(native.get("schema_version")) is int and native["schema_version"] == 2 and
                           native.get("workload") == "dex_source_strategy_complete_owner_v2" and "phases" in native,
        "success": native.get("success") is True,
        "case": native.get("case") == case, "variant": native.get("variant") == variant,
        "source_final_released": native.get("source_final_released") is True,
        "balanced_callbacks": native.get("callback_releases_including_caller") == native.get("callback_retains", -9) + 1,
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

    def redact(self, value):
        value = super().redact(value)
        if isinstance(value, str):
            # Public command output retains bytes hashes, not local machine paths.
            return re.sub(r"[A-Za-z]:[\\/][^\r\n]*", "<redacted-local-path>", value)
        return value

    def command(self, name, arguments, timeout=20, hash_stdout=False, capture_private_inventory=False):
        """Same PairedRunner durable journal, plus exact byte hashes and clocks."""
        if hasattr(self, "admission_sha"):
            self.admission_gate()
        effective_timeout = min(timeout, self.remaining())
        self.counter += 1
        command = [str(self.args.adb), "-s", self.args.serial] + arguments
        begin = time.monotonic_ns()
        timed_out, launch_error = False, None
        try:
            result = subprocess.run(command, capture_output=True, timeout=effective_timeout, check=False)
            code, stdout_bytes, stderr_bytes = result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as error:
            code, stdout_bytes, stderr_bytes, timed_out = None, error.stdout or b"", error.stderr or b"", True
            self.receipt["remote_process_state_after_timeout"] = "UNKNOWN_DO_NOT_ASSUME_STOPPED"
        except OSError as error:
            code, stdout_bytes, stderr_bytes, launch_error = None, b"", b"", str(error)
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
        (self.output / row["stdout_file"]).write_text(public_stdout, encoding="utf-8", newline="\n")
        (self.output / row["stderr_file"]).write_text(public_stderr, encoding="utf-8", newline="\n")
        self.receipt["commands"].append(row)
        self.save()
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

    def local_assets(self):
        for name, relative in FIXED_BINARIES.items():
            path = ROOT / relative
            require(path.is_file(), "required_binary_missing:" + name, Blocked)
            verify_arm64_elf(path)
            self.assets[name] = path
        require(self.args.dex_input.is_file() and digest(self.args.dex_input) == INPUT_SHA256 and
                self.args.dex_input.stat().st_size == 10540184, "real_DEX_input_identity_mismatch", Blocked)
        self.assets["input.dex"] = self.args.dex_input
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
        self.receipt["oracles"] = self.oracles

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
        self.checked("create-owned-directory", ["shell", "mkdir", self.remote])
        self.receipt["device_directory_created"] = True
        for item in self.receipt["deployment_manifest"]:
            name = item["name"]
            self.checked("push-" + name.replace(".", "-"), ["push", str(self.assets[name]), self.remote + "/" + name], timeout=60)
            row = self.checked("hash-" + name.replace(".", "-"), ["shell", "sha256sum", self.remote + "/" + name])
            actual = row["stdout"].split()[0] if row["stdout"].split() else ""
            require(actual == item["sha256"], "remote_stage_hash_mismatch:" + name)
            item["device_sha256"] = actual
        self.checked("set-owned-executable-bits", ["shell", "chmod", "700"] +
                     [self.remote + "/" + name for name in FIXED_BINARIES if name != "libmgmem.so"])
        self.checked("create-output-failure-directory", ["shell", "mkdir", self.remote + "/output-is-directory"])
        self.boot_gate("boot-after-stage")

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
                checks = dex_checks(native, spec["variant"], spec["case"], spec["diagnostic"])
                checks["CPU_affinity"] = all(native.get(k) == self.args.cpu for k in ("selected_cpu", "cpu_before", "cpu_after"))
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
                          "positive_total": type(native.get("total_ns")) is int and native["total_ns"] > 0,
                          "payload_model": native.get("logical_requested_bytes") == resource_model["logical_payload_bytes"],
                          "backing_model": native.get("unique_owned_backing_peak_bytes") == resource_model["peak_unique_owned_backing_bytes"],
                          "provider_model": native.get("provider_allocation_count") == resource_model["provider_allocation_count"],
                          "unknowns_not_zero": all(key in native and native[key] is None for key in ("arena_used_bytes", "input_backing_padding_bytes", "memory_metadata_bytes", "rss_pss_bytes", "energy")),
                          "actual_steady_calls": native.get("steady_state_provider_allocations") == 0,
                          "CPU_affinity": all(native.get(k) == self.args.cpu for k in ("cpu_requested", "cpu_before", "cpu_after"))}
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
        previous = None
        for spec in make_specs():
            if previous is not None and previous != spec["group"]:
                self.admission_gate()
                self.occupancy("after-" + previous.replace("_", "-"), self.allowed)
                self.boot_gate("boot-after-" + previous.replace("_", "-"))
            self.one(spec)
            previous = spec["group"]
        self.admission_gate()
        self.occupancy("after", self.allowed)
        self.boot_gate("boot-after")
        require(self.receipt["completed_cases"] == EXPECTED_CASES, "functional_case_count_incomplete")
        self.receipt.update({"state": "PASS", "all_passed": True})
        self.receipt["qualification"]["functional"] = "PASS_RECORDED_DEVICE_BOOT_PAGE_ARTIFACTS_ONLY"
        self.receipt["qualification"]["16KB"] = "ACTUAL_16KB_NATIVE_EXECUTION_SUBSET" if self.receipt["device"]["page_bytes"] == 16384 else "NOT_OBSERVED_ON_CURRENT_PAGE_SIZE"
        deployment = {"schema_version": 2, "state": "FUNCTIONAL_STAGED_AND_PASSED", "device_directory": self.remote,
                      "device_serial_sha256": self.receipt["device_serial_sha256"], "profile": self.receipt["device"],
                      "artifacts": self.receipt["deployment_manifest"],
                      "reuse_rule": "Performance capture independently stages and hashes its own directory; this is functional provenance only."}
        write_json(self.root_output / "deployment.json", deployment)


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
    args = parser.parse_args()
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", args.serial) is not None, "invalid_device_identifier", Blocked)
    require(0 <= args.cpu <= 255 and 1 <= args.task_timeout <= 60 and 60 <= args.overall_timeout <= 1200,
            "invalid_bounded_limits", Blocked)
    output = args.out_dir.resolve()
    require(output.is_relative_to(ROOT) and not output.exists(), "output_must_be_new_directory_inside_workspace", Blocked)
    output.mkdir(parents=True)
    args.adb, args.admission, args.dex_input = args.adb.resolve(), args.admission.resolve(), args.dex_input.resolve()
    runner = ConsumerRunner(args, output)
    code = 1
    try:
        runner.run()
        code = 0
    except Blocked as error:
        runner.receipt.update({"state": "BLOCKED", "reason": str(error)})
        code = 2
    except (Failed, OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        runner.receipt.update({"state": "FAIL", "reason": str(error)})
    except KeyboardInterrupt:
        runner.receipt.update({"state": "INTERRUPTED_INCOMPLETE", "reason": "operator_interrupt_remote_state_not_assumed_stopped"})
    finally:
        runner.receipt["completed_at_utc"] = utc_now()
        runner.save(force=True)
    print(json.dumps({"state": runner.receipt["state"], "all_passed": runner.receipt["all_passed"],
                      "performance_qualified": False, "receipt": str(output / "functional-receipt.json")}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
