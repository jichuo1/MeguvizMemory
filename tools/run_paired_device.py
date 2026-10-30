#!/usr/bin/env python3
"""Bounded arm64 paired capture after explicit owner release and safety PASS.

Only this experiment's new random device directory is written. No process is
killed, no app/settings/sysfs policy is changed, and an ADB timeout leaves remote
process state UNKNOWN. Native timing excludes the host's diagnostic commands.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time

from run_arm64_validation import Blocked, Failed, Runner, digest, utc_now, verify_arm64_elf


REMOTE = re.compile(r"^/data/local/tmp/meguvizmemory-paired-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")
BOOT = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
SHA256 = re.compile(r"[0-9a-f]{64}")
ORDERS = (("A1", "A3", "A3", "A1"), ("A3", "A1", "A1", "A3"))
ATOMIC_REPLACE_RETRY_SECONDS = 0.9
RECEIPT_SNAPSHOT_SECONDS = 2.0


def integer(value, minimum=0, maximum=(1 << 64) - 1):
    return not isinstance(value, bool) and isinstance(value, int) and minimum <= value <= maximum


def write_json(path, value):
    pending = path.with_name(path.name + ".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                       encoding="utf-8", newline="\n")
    deadline = time.monotonic() + ATOMIC_REPLACE_RETRY_SECONDS
    while True:
        try:
            pending.replace(path)
            return
        except PermissionError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.025, remaining))
            if time.monotonic() >= deadline:
                # Keep both the last complete target and the pending update.
                # Never claim success or replace atomic publication with a
                # non-atomic direct write when a Windows reader holds the target.
                raise


def public_value(value):
    """Private in-memory inventory keys must never reach durable output."""
    if isinstance(value, dict):
        return {key: public_value(item) for key, item in value.items()
                if not str(key).startswith("_")}
    if isinstance(value, list):
        return [public_value(item) for item in value]
    return value


def command_hash(command):
    canonical = json.dumps(command, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_command_journal(path):
    """Recover latest public command revisions; corruption is an explicit error."""
    commands, revisions = {}, {}
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        record_id, revision = event.get("record_id"), event.get("revision")
        command = event.get("command")
        if (event.get("event") != "command_revision" or not integer(record_id, 1) or
                not integer(revision, 1) or revision != revisions.get(record_id, 0) + 1 or
                not isinstance(command, dict) or command.get("record_id") != record_id or
                public_value(command) != command or command_hash(command) != event.get("command_sha256")):
            raise ValueError("invalid_or_incomplete_command_journal_revision")
        commands[record_id], revisions[record_id] = command, revision
    return commands


def load_json(path):
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise Blocked("expected_JSON_object: " + path.name)
    return value


class PairedRunner(Runner):
    def __init__(self, args, output):
        self.root_output = output
        self.phase = args.phase
        command_output = output / "commands" / args.phase
        command_output.mkdir(parents=True, exist_ok=False)
        super().__init__(args, command_output)
        self.remote = "/data/local/tmp/meguvizmemory-paired-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(6)
        self.receipt.update({"scope": "ARM64_REAL_CPU_A1_A3_PAIRED_CAPTURE", "phase": args.phase,
                             "device_directory": self.remote, "native_session_rows": 0,
                             "qualification": {"scope": "INDEPENDENT_NATIVE_PARSER_TASK_ONLY", "BIL_auto_enable": False,
                                               "16kb": "UNAVAILABLE", "MTE": "UNAVAILABLE", "energy": "UNAVAILABLE"}})
        self.started = time.monotonic()
        self.protocol = None
        self.deployment = None
        self.plan = None
        self.allowed = {}
        self.workload_started = False
        self.local_manifest = {}
        self._last_snapshot_at = None
        self._last_snapshot_state = None
        self._journal_signature = None
        self._journal_revisions = {}
        self._journal_events = 0

    def redact(self, value):
        value = super().redact(value)
        if isinstance(value, str):
            return value.replace(str(self.args.input), "<local-input-path-redacted>")
        return value

    def save(self, force=False):
        commands = self.receipt.get("commands", [])
        if commands:
            # Runner.command and publish_inventory only add/update the last
            # public row. Hash that row alone; full-history serialization is
            # reserved for the throttled snapshot below.
            command = self.redact(public_value(commands[-1]))
            record_id = command["record_id"]
            digest_value = command_hash(command)
            signature = (record_id, digest_value)
            if signature != self._journal_signature:
                revision = self._journal_revisions.get(record_id, 0) + 1
                event = {"schema_version": 1, "event": "command_revision",
                         "recorded_at_utc": utc_now(), "record_id": record_id,
                         "revision": revision, "command_sha256": digest_value,
                         "command": command}
                journal = self.root_output / (self.phase + "-commands.jsonl")
                with journal.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                self._journal_signature = signature
                self._journal_revisions[record_id] = revision
                self._journal_events += 1
        self.receipt["command_journal"] = {
            "path": self.phase + "-commands.jsonl", "events": self._journal_events,
            "durability": "Each changed last public command revision is appended and fsynced before snapshot.",
            "snapshot_interval_seconds": RECEIPT_SNAPSHOT_SECONDS,
        }
        now, state = time.monotonic(), self.receipt.get("state")
        if (force or self._last_snapshot_at is None or state != self._last_snapshot_state or
                now - self._last_snapshot_at >= RECEIPT_SNAPSHOT_SECONDS):
            write_json(self.root_output / (self.phase + "-receipt.json"),
                       self.redact(public_value(self.receipt)))
            self._last_snapshot_at = time.monotonic()
            self._last_snapshot_state = state

    def append(self, row):
        with (self.root_output / (self.phase + ".jsonl")).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(self.redact(row), ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.receipt["native_session_rows"] += 1
        self.save()

    def remaining(self):
        maximum = self.protocol["limits"]["phase_max_wall_seconds"] if self.protocol else 600
        left = maximum - (time.monotonic() - self.started)
        if left <= 0:
            raise Blocked("phase_wall_time_limit_reached")
        return left

    def checked(self, name, arguments, timeout=20, private=False):
        row = self.command(name, arguments, timeout=min(timeout, self.remaining()),
                           capture_private_inventory=private)
        self.require_command(row, name + "_failed")
        return row

    def boot_hash(self, label):
        row = self.command(label, ["shell", "cat", "/proc/sys/kernel/random/boot_id"],
                           timeout=min(20, self.remaining()), hash_stdout=True)
        self.require_command(row, "boot_identity_unavailable")
        if not row.get("stdout_value_is_boot_uuid"):
            raise Blocked("invalid_boot_identity")
        return row["stdout_value_sha256"]

    def validate_inputs(self):
        self.allowed = self.admission()  # Before the first ADB command.
        safety = load_json(self.args.safety)
        device = safety.get("device", {})
        if (not isinstance(device, dict) or safety.get("state") != "PASS" or safety.get("all_passed") is not True or
                "arm64-v8a" not in str(device.get("abi_list", "")).split(",") or
                device.get("page_bytes") != 4096):
            raise Blocked("requires_PASS_arm64_4096_page_safety_receipt")
        if safety.get("device_serial_sha256") != self.receipt["device_serial_sha256"]:
            raise Blocked("safety_receipt_is_for_different_device")
        if not isinstance(device.get("boot_id_sha256"), str) or not SHA256.fullmatch(device["boot_id_sha256"]):
            raise Blocked("safety_receipt_has_no_boot_hash")
        self.safety_boot_hash = device["boot_id_sha256"]
        self.receipt["safety_receipt"] = {"sha256": digest(self.args.safety), "state": "PASS",
                                          "abi": "arm64-v8a", "page_bytes": 4096}
        self.protocol = load_json(self.args.protocol)
        protocol = self.protocol
        if self.phase == "formal":
            if protocol.get("state") != "FROZEN_FOR_FORMAL" or self.args.plan is None:
                raise Blocked("formal_requires_frozen_protocol_and_plan")
            expected = protocol.get("final_plan_sha256")
            if not isinstance(expected, str) or not SHA256.fullmatch(expected) or digest(self.args.plan) != expected:
                raise Blocked("formal_plan_hash_mismatch")
            self.plan = load_json(self.args.plan)
            entries = self.plan.get("planned_blocks", [])
            count = self.plan.get("formal_blocks")
            if (self.plan.get("state") != "FROZEN_SAMPLE_SIZE_AND_ORDER_FROM_PILOT" or
                    not integer(count, 32, 96) or count % 2 or len(entries) != count or
                    not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries) or
                    [entry.get("block_id") for entry in entries] != list(range(count)) or
                    any(tuple(entry.get("order", [])) not in ORDERS for entry in entries) or
                    sum(tuple(entry["order"]) == ORDERS[0] for entry in entries) != count // 2):
                raise Blocked("invalid_frozen_formal_sequence")
            identity = self.plan.get("semantic_identity", {})
            if (identity.get("input_sha256") != protocol.get("input_sha256") or
                    identity.get("method_count") != protocol.get("expected_method_count") or
                    identity.get("result_checksum") != protocol.get("expected_result_checksum")):
                raise Blocked("formal_plan_semantics_differ_from_protocol")
            self.receipt["plan_sha256"] = expected
        elif protocol.get("state") not in (("PILOT_READY", "FROZEN_FOR_FORMAL") if self.phase == "stage" else ("PILOT_READY",)):
            raise Blocked("pilot_or_diagnostic_requires_PILOT_READY_protocol")
        if (not integer(protocol.get("cpu"), 0, 255) or not integer(protocol.get("expected_method_count"), 1) or
                not integer(protocol.get("expected_result_checksum")) or
                not isinstance(protocol.get("input_sha256"), str) or not SHA256.fullmatch(protocol["input_sha256"])):
            raise Blocked("invalid_workload_protocol_contract")
        environment = protocol.get("environment", {})
        limits = protocol.get("limits", {})
        expected_environment = {"battery_max_deci_c": 400, "thermal_status_max": 0,
                                "background_cpu_busy_max": 0.5, "background_window_seconds": 0.1,
                                "cooldown_attempts": 6, "cooldown_seconds": 2}
        if environment != expected_environment:
            raise Blocked("environment_contract_differs_from_reviewed_limits")
        if (not isinstance(limits, dict) or not integer(limits.get("single_launch_timeout_seconds"), 1, 20) or
                not integer(limits.get("phase_max_wall_seconds"), 1, 600) or
                not integer(limits.get("maximum_native_task_seconds"), 1, 5)):
            raise Blocked("invalid_or_unbounded_capture_limits")
        if not self.args.adb.is_file():
            raise Blocked("ADB_missing")
        for role, path, name in (("executable", self.args.exe, "mgmem_dex_paired"),
                                 ("library", self.args.library, "libmgmem.so"),
                                 ("input", self.args.input, "input.dex")):
            if not path.is_file():
                raise Blocked("missing_local_" + role)
            if role != "input":
                verify_arm64_elf(path)
            self.local_manifest[role] = {"name": name, "bytes": path.stat().st_size, "sha256": digest(path)}
        if self.local_manifest["input"]["sha256"] != protocol["input_sha256"]:
            raise Blocked("local_input_hash_differs_from_protocol")
        frozen_artifacts = protocol.get("artifact_sha256", {})
        if (not isinstance(frozen_artifacts, dict) or
                frozen_artifacts.get("native_exe") != self.local_manifest["executable"]["sha256"] or
                frozen_artifacts.get("installed_library") != self.local_manifest["library"]["sha256"]):
            raise Blocked("local_artifacts_differ_from_frozen_protocol")
        if protocol.get("safety_receipt_sha256") != digest(self.args.safety):
            raise Blocked("safety_receipt_differs_from_frozen_protocol")
        tested_libraries = [item for item in safety.get("binary_manifest", []) if item.get("name") == "libmgmem.so"]
        if len(tested_libraries) != 1 or tested_libraries[0].get("sha256") != self.local_manifest["library"]["sha256"]:
            raise Blocked("selected_library_differs_from_arm64_safety_tested_artifact")
        self.receipt.update({"protocol_sha256": digest(self.args.protocol), "local_artifacts": self.local_manifest,
                             "workload_contract": self.workload_contract(), "shared_device_lock_acquired": False})
        self.save()

    def workload_contract(self):
        return {key: self.protocol[key] for key in ("cpu", "input_sha256", "expected_method_count", "expected_result_checksum")}

    def verify_deployment(self):
        deployment = load_json(self.root_output / "deployment.json")
        if (deployment.get("state") != "STAGED" or deployment.get("device_serial_sha256") != self.receipt["device_serial_sha256"] or
                not REMOTE.fullmatch(str(deployment.get("device_directory", ""))) or
                deployment.get("artifacts") != self.local_manifest or deployment.get("workload_contract") != self.workload_contract()):
            raise Blocked("deployment_identity_or_local_artifact_mismatch")
        self.remote = deployment["device_directory"]
        self.receipt["device_directory"] = self.remote
        if self.boot_hash("boot-before") != deployment.get("boot_id_sha256"):
            raise Blocked("device_boot_differs_from_staged_deployment")
        if deployment["boot_id_sha256"] != self.safety_boot_hash:
            raise Blocked("safety_receipt_is_from_another_boot")
        self.deployment = deployment
        for role, record in self.local_manifest.items():
            row = self.checked("verify-device-" + role, ["shell", "sha256sum", self.remote + "/" + record["name"]])
            if not row["stdout"].split() or row["stdout"].split()[0] != record["sha256"]:
                raise Blocked("staged_device_artifact_hash_mismatch: " + role)
        self.save()

    def stage(self):
        boot = self.boot_hash("stage-boot-before")
        if boot != self.safety_boot_hash:
            raise Blocked("device_rebooted_since_arm64_safety_validation")
        self.checked("create-exclusive-owned-directory", ["shell", "mkdir", self.remote])
        self.receipt["device_directory_created"] = True
        for role, path in (("executable", self.args.exe), ("library", self.args.library), ("input", self.args.input)):
            record = self.local_manifest[role]
            destination = self.remote + "/" + record["name"]
            self.checked("push-" + role, ["push", str(path), destination])
            actual = self.checked("hash-" + role, ["shell", "sha256sum", destination])["stdout"].split()
            if not actual or actual[0] != record["sha256"]:
                raise Failed("staged_hash_mismatch: " + role)
        self.checked("set-owned-execute-bit", ["shell", "chmod", "700", self.remote + "/mgmem_dex_paired"])
        if self.boot_hash("stage-boot-after") != boot:
            raise Failed("boot_changed_during_staging")
        self.deployment = {"schema_version": 1, "state": "STAGED", "created_at_utc": utc_now(),
                           "device_serial_sha256": self.receipt["device_serial_sha256"], "device_directory": self.remote,
                           "boot_id_sha256": boot, "artifacts": self.local_manifest,
                           "workload_contract": self.workload_contract(), "protocol_at_stage_sha256": self.receipt["protocol_sha256"],
                           "cleanup": "Own directory retained. No automatic deletion, overwrite or process termination."}

    def environment(self, label):
        battery = self.checked(label + "-battery", ["shell", "cat", "/sys/class/power_supply/battery/temp"])
        text = battery["stdout"].strip()
        battery_value = int(text) if re.fullmatch(r"-?[0-9]+", text) else None
        thermal = self.checked(label + "-thermal", ["shell", "dumpsys", "thermalservice"], private=True)
        statuses = re.findall(r"(?im)^\s*Thermal Status:\s*([0-9]+)\s*$", thermal["_inventory_stdout"])
        thermal_status = int(statuses[0]) if len(statuses) == 1 else None
        self.publish_inventory(thermal, {"thermal_status": thermal_status,
                                        "scope": "Only exact Thermal Status integer retained; not a CPU temperature reading."})
        proc = self.checked(label + "-proc-stat", ["shell", "cat", "/proc/stat"], private=True)
        cpu = self.protocol["cpu"]
        selected = [line.strip() for line in proc["_inventory_stdout"].splitlines() if re.match(r"^cpu" + str(cpu) + r"\s+", line)]
        proc_observed_ns = time.monotonic_ns()
        ticks = None
        if len(selected) == 1:
            fields = selected[0].split()[1:]
            if len(fields) >= 8 and all(re.fullmatch(r"[0-9]+", item) for item in fields[:8]):
                ticks = [int(item) for item in fields[:8]]
        self.publish_inventory(proc, {"selected_cpu": cpu, "selected_proc_stat_line": selected[0] if len(selected) == 1 else None})
        frequency = self.command(label + "-frequency", ["shell", "cat", "/sys/devices/system/cpu/cpu" + str(cpu) + "/cpufreq/scaling_cur_freq"],
                                 timeout=min(20, self.remaining()))
        frequency_text = frequency["stdout"].strip()
        if frequency["timed_out"]:
            raise Blocked("optional_frequency_query_timed_out_remote_state_unknown")
        frequency_value = int(frequency_text) if frequency["exit_code"] == 0 and re.fullmatch(r"[0-9]+", frequency_text) else None
        return {"captured_at_utc": utc_now(), "battery_deci_c": battery_value,
                "battery_sensor_is_CPU_temperature": False, "thermal_status": thermal_status,
                "selected_cpu": cpu, "selected_cpu_ticks_first_eight": ticks,
                "selected_cpu_stat_observed_monotonic_ns": proc_observed_ns,
                "scaling_cur_freq_khz": frequency_value,
                "frequency_observation": "AVAILABLE" if frequency_value is not None else "UNAVAILABLE"}

    def thermal_valid(self, value):
        reasons = []
        if not integer(value.get("battery_deci_c"), -1000, 2000):
            reasons.append("battery_temperature_unavailable_or_invalid")
        elif value["battery_deci_c"] > self.protocol["environment"]["battery_max_deci_c"]:
            reasons.append("battery_temperature_above_limit")
        if not integer(value.get("thermal_status"), 0, 6):
            reasons.append("thermal_status_unavailable_or_invalid")
        elif value["thermal_status"] > self.protocol["environment"]["thermal_status_max"]:
            reasons.append("thermal_status_above_limit")
        return reasons

    def before_environment(self, prefix):
        attempts = []
        configuration = self.protocol["environment"]
        for attempt in range(configuration["cooldown_attempts"]):
            first = self.environment(prefix + "-pre" + str(attempt) + "a")
            if self.remaining() <= configuration["background_window_seconds"]:
                raise Blocked("phase_wall_time_limit_before_background_window")
            time.sleep(configuration["background_window_seconds"])
            second = self.environment(prefix + "-pre" + str(attempt) + "b")
            reasons = self.thermal_valid(first) + self.thermal_valid(second)
            one, two = first["selected_cpu_ticks_first_eight"], second["selected_cpu_ticks_first_eight"]
            busy = None
            if one is None or two is None:
                reasons.append("selected_cpu_stat_unavailable")
            else:
                delta = [right - left for left, right in zip(one, two)]
                total, idle = sum(delta), delta[3] + delta[4]
                if any(value < 0 for value in delta) or total <= 0 or not 0 <= idle <= total:
                    reasons.append("invalid_selected_cpu_tick_delta")
                else:
                    busy = (total - idle) / total
                    if busy > configuration["background_cpu_busy_max"]:
                        reasons.append("selected_cpu_background_busy_above_limit")
            record = {"attempt": attempt + 1, "first": first, "second": second,
                      "selected_cpu_background_busy": busy,
                      "actual_stat_observation_window_seconds": (second["selected_cpu_stat_observed_monotonic_ns"] - first["selected_cpu_stat_observed_monotonic_ns"]) / 1e9,
                      "gate_reasons": sorted(set(reasons)), "valid": not reasons}
            attempts.append(record)
            self.receipt["latest_prelaunch_gate"] = record
            self.save()
            if not reasons:
                return {"valid": True, "attempts": attempts}
            if attempt + 1 < configuration["cooldown_attempts"]:
                if self.remaining() <= configuration["cooldown_seconds"]:
                    break
                time.sleep(configuration["cooldown_seconds"])
        return {"valid": False, "attempts": attempts}

    def native_reasons(self, native, variant, diagnostic):
        checks = {
            "native_success": native.get("success") is True,
            "variant": native.get("variant") == variant,
            "input_hash": native.get("source_sha256") == self.protocol["input_sha256"],
            "input_length": native.get("input_bytes") == self.local_manifest["input"]["bytes"],
            "method_count": native.get("method_count") == self.protocol["expected_method_count"],
            "result_checksum": native.get("result_checksum") == self.protocol["expected_result_checksum"],
            "closed_backing": integer(native.get("closed_backing_bytes")) and native.get("closed_backing_bytes") == 0,
            "last_lease_lifetime": native.get("lease_final_correct") is True,
            "cpu_affinity": native.get("affinity_selected") is True and all(integer(native.get(key), 0, 255) and native[key] == self.protocol["cpu"]
                                                                           for key in ("selected_cpu", "cpu_before", "cpu_after")),
            "timing": integer(native.get("total_ns"), 1, self.protocol["limits"]["maximum_native_task_seconds"] * 1000000000),
            "diagnostic_round": native.get("diagnostic") is diagnostic,
            "memory_variant": native.get("memory_allocator_and_lease_used") is (variant == "A3"),
            "A3_observation_cost_included": native.get("a3_observation_API_cost_included") is True,
        }
        return ["native_contract_" + key for key, good in checks.items() if not good]

    def one(self, block_id, position, variant):
        diagnostic = self.phase == "diagnostic"
        row = {"schema_version": 1, "record_kind": "native_session", "phase": self.phase,
               "block_id": block_id, "position": position, "variant": variant,
               "success": False, "valid_environment": False, "native": None,
               "protocol_sha256": self.receipt["protocol_sha256"], "started_at_utc": utc_now(),
               "environment_before": None, "environment_after": None, "gate_reasons": [],
               "remote_process_state_after_timeout": None, "command_result": None}
        prefix = "b" + str(block_id).zfill(3) + "p" + str(position)
        try:
            self.remaining()
            if digest(self.args.protocol) != self.receipt["protocol_sha256"]:
                raise Blocked("protocol_changed_during_capture")
            if digest(self.args.admission) != self.receipt["admission"]["sha256"]:
                raise Blocked("owner_admission_changed_during_capture")
            before = self.before_environment(prefix)
            row["environment_before"] = before
            if not before["valid"]:
                raise Blocked("prelaunch_environment_not_qualified_after_bounded_cooldown")
            command = ["shell", "LD_LIBRARY_PATH=" + self.remote, self.remote + "/mgmem_dex_paired",
                       self.remote + "/input.dex", "--variant", variant, "--cpu", str(self.protocol["cpu"])]
            if diagnostic:
                command.append("--diagnostic")
            self.workload_started = True
            result = self.command(prefix + "-native-" + variant, command,
                                  timeout=min(self.protocol["limits"]["single_launch_timeout_seconds"], self.remaining()))
            row["command_result"] = result
            if result["timed_out"]:
                row["remote_process_state_after_timeout"] = "UNKNOWN_DO_NOT_ASSUME_STOPPED"
                raise Failed("native_timeout_remote_process_state_unknown")
            if result["exit_code"] != 0 or result["launch_error"]:
                raise Failed("native_command_failed")
            native = json.loads(result["stdout"].strip())
            if not isinstance(native, dict):
                raise Failed("native_result_is_not_JSON_object")
            row["native"] = native
            row["gate_reasons"].extend(self.native_reasons(native, variant, diagnostic))
            after = self.environment(prefix + "-post")
            row["environment_after"] = after
            thermal_reasons = self.thermal_valid(after)
            row["valid_environment"] = not thermal_reasons
            row["gate_reasons"].extend(thermal_reasons)
            row["success"] = not row["gate_reasons"]
        except (Blocked, Failed, OSError, ValueError, RuntimeError) as failure:
            row["gate_reasons"].append(str(failure))
        finally:
            row["completed_at_utc"] = utc_now()
            self.append(row)
        if not row["success"] or not row["valid_environment"]:
            raise Failed("phase_stopped_after_recorded_session_failure")

    def run(self):
        self.validate_inputs()
        self.occupancy("before", self.allowed)
        before_passed = True
        failed = None
        try:
            if self.phase == "stage":
                self.stage()
            else:
                self.verify_deployment()
                if self.phase == "diagnostic":
                    sequence = [{"block_id": 0, "order": ["A1", "A3"]}]
                elif self.phase == "pilot":
                    sequence = [{"block_id": i, "order": list(ORDERS[i % 2])} for i in range(6)]
                else:
                    sequence = self.plan["planned_blocks"]
                for block in sequence:
                    for position, variant in enumerate(block["order"]):
                        self.one(block["block_id"], position, variant)
                    print(json.dumps({"phase": self.phase, "completed_block": block["block_id"], "state": "RECORDED"}), flush=True)
        except (Blocked, Failed, OSError, ValueError, RuntimeError) as failure:
            failed = failure
        finally:
            try:
                if before_passed:
                    self.occupancy("after", self.allowed)
                expected_boot = self.deployment["boot_id_sha256"] if self.deployment else self.safety_boot_hash
                if self.boot_hash("phase-boot-after") != expected_boot:
                    raise Failed("boot_changed_during_phase")
                if digest(self.args.protocol) != self.receipt["protocol_sha256"]:
                    raise Failed("protocol_changed_during_phase")
                if self.phase == "formal" and digest(self.args.plan) != self.receipt["plan_sha256"]:
                    raise Failed("formal_plan_changed_during_phase")
            except (Blocked, Failed, OSError, ValueError, RuntimeError) as integrity:
                self.receipt["phase_integrity_failure"] = str(integrity)
                if self.phase != "stage":
                    self.append({"schema_version": 1, "record_kind": "phase_integrity_failure", "phase": self.phase,
                                 "block_id": 1000000, "position": 0, "variant": "A1", "success": False,
                                 "valid_environment": False, "native": None, "reason": str(integrity),
                                 "protocol_sha256": self.receipt["protocol_sha256"], "captured_at_utc": utc_now()})
                failed = failed or integrity
        if failed:
            raise failed
        if self.phase == "stage":
            write_json(self.root_output / "deployment.json", self.redact(self.deployment))
        self.receipt.update({"state": "PASS", "all_passed": True,
                             "completed_at_utc": utc_now(), "cleanup": "Own directory retained. No automatic kill or delete."})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("adb", "admission", "safety", "exe", "library", "input", "protocol", "out-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--phase", choices=("stage", "diagnostic", "pilot", "formal"), required=True)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", args.serial):
        parser.error("invalid bounded serial")
    root = Path(__file__).resolve().parents[1]
    output = args.out_dir.resolve()
    if not output.is_relative_to(root) or output == root:
        parser.error("--out-dir must be a child of this workspace")
    if args.phase == "stage":
        if output.exists():
            parser.error("stage output already exists; retain previous evidence and choose a new directory")
        output.mkdir(parents=True, exist_ok=False)
    elif not output.is_dir() or not (output / "deployment.json").is_file():
        parser.error("stage deployment is required before other phases")
    if ((output / (args.phase + "-receipt.json")).exists() or
            (output / (args.phase + ".jsonl")).exists() or (output / "commands" / args.phase).exists()):
        parser.error("phase evidence already exists; never overwrite or silently append a second experiment")
    for name in ("adb", "admission", "safety", "exe", "library", "input", "protocol", "plan"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    runner = PairedRunner(args, output)
    code = 1
    try:
        runner.run()
        code = 0
    except Blocked as failure:
        runner.receipt.update({"state": "BLOCKED", "all_passed": False, "reason": str(failure)})
        code = 2
    except (Failed, OSError, ValueError, RuntimeError) as failure:
        runner.receipt.update({"state": "FAIL", "all_passed": False, "reason": str(failure)})
    finally:
        runner.receipt["completed_at_utc"] = utc_now()
        runner.save(force=True)
    print(json.dumps({"phase": args.phase, "state": runner.receipt["state"],
                      "all_passed": runner.receipt["all_passed"],
                      "receipt": str(output / (args.phase + "-receipt.json"))}), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
