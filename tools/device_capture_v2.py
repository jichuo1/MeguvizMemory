#!/usr/bin/env python3
"""Bounded v2 physical capture; callers must supply a frozen suite and admission.

No device operations occur during import or validation. Only a newly created
random directory under /data/local/tmp is written. A timeout never implies the
remote child exited. This module cannot grant performance or BIL qualification.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import time
from types import SimpleNamespace

from paired_analysis_v2 import canonical, digest as object_sha, strict_json
from run_arm64_validation import Blocked, Failed, Runner, digest, utc_now, verify_arm64_elf
from run_paired_device import public_value, write_json


SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
REMOTE = re.compile(r"/data/local/tmp/meguvizmemory-v2-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}\Z")
BOOT = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


def require(ok, reason):
    if not ok:
        raise Blocked(reason)


def uint(value, low=0, high=2**64-1):
    return type(value) is int and low <= value <= high


def load(path):
    value = strict_json(Path(path).read_text(encoding="utf-8-sig"))
    require(isinstance(value, dict), "JSON_object_required")
    return value


class Journal:
    """New file, append/fsync hash chain; no resume, repair, or replacement."""
    def __init__(self, path):
        self.path = Path(path)
        self.stream = self.path.open("x", encoding="utf-8", newline="\n")
        self.sequence, self.previous = 0, "0" * 64

    def append(self, record):
        event = {"schema_version": 2, "sequence": self.sequence + 1,
                 "previous_sha256": self.previous, "record_sha256": object_sha(record), "record": record}
        event["event_sha256"] = object_sha(event)
        self.stream.write(canonical(event).decode("utf-8") + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.sequence += 1
        self.previous = event["event_sha256"]

    def close(self):
        self.stream.close()


def environment_script(cpu, window=None, battery_source="sysfs"):
    """One shell invocation; CPU samples and sensor reads have explicit domains.

Each CPU stat read is bracketed by /proc/uptime. Its ten-millisecond clock
resolution is retained in the public record. No host/device clock subtraction.
"""
    require(uint(cpu, 0, 255), "invalid_cpu")
    require(battery_source in ("sysfs", "dumpsys_battery"), "invalid_frozen_battery_source")
    if window is not None:
        require(isinstance(window, (int, float)) and not isinstance(window, bool) and .1 <= window <= 2,
                "invalid_background_window")
    commands = ["set -e"]
    if window is not None:
        for index in (1, 2):
            if index == 2:
                commands.append("sleep " + str(window))
            commands += ["echo MGMEM_V2_STAT%d_BEGIN" % index, "cat /proc/uptime",
                         "grep '^cpu%d ' /proc/stat" % cpu, "cat /proc/uptime"]
    battery = ("cat /sys/class/power_supply/battery/temp" if battery_source == "sysfs" else
               "dumpsys battery | sed -n 's/^[[:space:]]*temperature:[[:space:]]*//p'")
    # The service pipeline may exit zero with no matches. Host parsing still
    # requires exactly one integer; absence, duplicates and bad text fail closed.
    commands += ["echo MGMEM_V2_BATTERY", battery,
                 "echo MGMEM_V2_THERMAL", "dumpsys thermalservice",
                 "echo MGMEM_V2_THERMAL_CLOCK", "cat /proc/uptime",
                 "echo MGMEM_V2_FREQUENCY",
                 "cat /sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq 2>/dev/null || echo UNAVAILABLE" % cpu,
                 "echo MGMEM_V2_END", "cat /proc/uptime"]
    return "; ".join(commands)


def parse_environment(text, cpu, limits, cadence, before):
    battery_source = cadence.get("battery_source", "sysfs")
    require(battery_source in ("sysfs", "dumpsys_battery"), "invalid_frozen_battery_source")
    sections, current = {}, None
    for line in text.splitlines():
        if line.startswith("MGMEM_V2_"):
            require(line not in sections, "duplicate_environment_marker")
            current = line
            sections[current] = []
        elif current is not None:
            sections[current].append(line.strip())

    def one(name):
        rows = sections.get("MGMEM_V2_" + name, [])
        require(len(rows) == 1, "missing_environment_field:" + name)
        return rows[0]

    def uptime(text):
        parts = text.split()
        require(len(parts) == 2 and re.fullmatch(r"[0-9]+\.[0-9]+", parts[0]), "invalid_device_uptime")
        value = int(Decimal(parts[0]) * 1_000_000_000)
        require(value > 0, "invalid_device_uptime")
        return value

    battery = one("BATTERY")
    require(re.fullmatch(r"-?[0-9]+", battery), "invalid_battery_reading")
    battery = int(battery)
    statuses = re.findall(r"(?m)^\s*Thermal Status:\s*([0-9]+)\s*$", "\n".join(sections.get("MGMEM_V2_THERMAL", [])))
    require(len(statuses) == 1, "missing_or_ambiguous_thermal_status")
    thermal, thermal_time, end = int(statuses[0]), uptime(one("THERMAL_CLOCK")), uptime(one("END"))
    require(thermal_time <= end, "device_environment_clock_reversed")
    frequency = one("FREQUENCY")
    frequency = int(frequency) if frequency.isdigit() else None
    reasons = []
    if not limits.get("battery_min_deci_c", -1000) <= battery <= limits["battery_max_deci_c"]:
        reasons.append("battery_temperature_outside_frozen_limits")
    if not 0 <= thermal <= limits["thermal_status_max"]:
        reasons.append("thermal_status_outside_frozen_limits")
    result = {"complete": True, "valid": not reasons, "gate_reasons": reasons,
              "clock_domain": "DEVICE_BOOTTIME_PROC_UPTIME_NS", "clock_resolution_ns": 10_000_000,
              "battery_deci_c": battery, "battery_sensor_is_CPU_temperature": False,
              "battery_source": battery_source,
              "thermal_status": thermal, "thermal_observed_monotonic_ns": thermal_time,
              "device_end_monotonic_ns": end, "scaling_cur_freq_khz": frequency,
              "frequency_observation": "AVAILABLE" if frequency is not None else "UNAVAILABLE"}
    if not before:
        return result
    samples = []
    for index in (1, 2):
        rows = sections.get("MGMEM_V2_STAT%d_BEGIN" % index, [])
        require(len(rows) == 3, "missing_CPU_stat_sample")
        start, stop = uptime(rows[0]), uptime(rows[2])
        fields = rows[1].split()
        require(len(fields) >= 9 and fields[0] == "cpu" + str(cpu) and all(x.isdigit() for x in fields[1:9]),
                "invalid_selected_CPU_stat")
        require(start <= stop, "reversed_CPU_sample_bounds")
        samples.append((start, stop, [int(x) for x in fields[1:9]]))
    first, second = samples
    lower, upper = second[0] - first[1], second[1] - first[0]
    require(0 < lower <= upper and second[1] <= thermal_time, "invalid_busy_clock_bounds")
    delta = [b-a for a, b in zip(first[2], second[2])]
    require(all(x >= 0 for x in delta) and sum(delta) > 0, "invalid_CPU_tick_delta")
    total, idle = sum(delta), delta[3] + delta[4]
    busy = (total - idle) / total
    require(0 <= busy <= 1, "invalid_background_busy")
    if busy > limits["background_cpu_busy_max"]:
        reasons.append("background_busy_outside_frozen_limits")
    if lower < cadence.get("busy_interval_min_ns", 300_000_000) or upper > cadence.get("busy_interval_max_ns", 1_500_000_000):
        reasons.append("actual_busy_window_outside_frozen_bounds")
    result.update({"valid": not reasons, "background_cpu_busy": busy,
                   "selected_cpu": cpu, "first_cpu_ticks": first[2], "second_cpu_ticks": second[2],
                   "busy_begin_monotonic_ns": (first[0] + first[1]) // 2,
                   "busy_end_monotonic_ns": (second[0] + second[1]) // 2,
                   "first_stat_before_monotonic_ns": first[0], "first_stat_after_monotonic_ns": first[1],
                   "second_stat_before_monotonic_ns": second[0], "second_stat_after_monotonic_ns": second[1],
                   "busy_observation_lower_bound_ns": lower, "busy_observation_upper_bound_ns": upper,
                   "busy_timestamp_meaning": "CPU-read midpoint estimate with explicit device-clock brackets, not a host timestamp"})
    return result


class CaptureV2(Runner):
    def __init__(self, root, adb, serial, admission_path, protocol_path, plan_path,
                 out_dir, phase, window_id=None, deployment_path=None, host_clock_epoch_id=None,
                 enabled_families=None, pilot_journal_path=None):
        self.root = Path(root).resolve()
        self.root_output = Path(out_dir).resolve()
        require(self.root_output.is_relative_to(self.root) and self.root_output != self.root, "output_outside_workspace")
        require(phase in ("stage", "pilot", "formal"), "invalid_capture_phase")
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", serial), "invalid_serial")
        self.phase, self.window_id = phase, window_id
        self.protocol_path, self.plan_path = Path(protocol_path).resolve(), Path(plan_path).resolve()
        self.deployment_path = Path(deployment_path).resolve() if deployment_path else None
        self.enabled_families = set(enabled_families) if enabled_families is not None else None
        self.pilot_journal_path = Path(pilot_journal_path).resolve() if pilot_journal_path else None
        self.pilot_evidence_hashes = {}
        self.source_file_hashes = {}
        self.root_output.mkdir(parents=True, exist_ok=False)
        command_dir = self.root_output / "commands"
        command_dir.mkdir()
        args = SimpleNamespace(adb=Path(adb).resolve(), serial=serial, admission=Path(admission_path).resolve())
        self.command_journal = Journal(self.root_output / "command-events.jsonl")
        self.task_journal = Journal(self.root_output / "tasks.jsonl")
        self._last_signature, self._snapshot_at = None, 0
        self.started_ns = time.monotonic_ns()
        require(host_clock_epoch_id is None or (isinstance(host_clock_epoch_id, str) and SAFE_NAME.fullmatch(host_clock_epoch_id)),
                "invalid_host_clock_epoch_identity")
        self.host_epoch = host_clock_epoch_id or self._host_epoch()
        self.protocol = self.plan = None
        self.admission_deadline = None
        super().__init__(args, command_dir)
        self.remote = "/data/local/tmp/meguvizmemory-v2-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(6)
        self.receipt.update({"scope": "PHYSICAL_ARM64_V2_SUITE_CAPTURE", "phase": phase,
                             "window_id": window_id, "device_directory": self.remote,
                             "started_at_utc": utc_now(), "state": "NOT_RUN", "all_passed": False,
                             "native_tasks_recorded": 0, "performance_qualified": False,
                             "host_clock_epoch_id": self.host_epoch,
                             "cleanup": "Owned directory retained; no automatic delete, kill, global policy or app changes."})

    @staticmethod
    def _host_epoch():
        # Parent-managed execution can span processes while sharing one monotonic
        # host clock; the boot estimate is only an identifier, not a time origin.
        return "host-monotonic-boot-" + str(round((time.time_ns() - time.monotonic_ns()) / 10_000_000_000))

    def redact(self, value):
        value = super().redact(value)
        if isinstance(value, str):
            for prefix in (str(self.root), str(self.root).replace("\\", "/"), str(self.args.adb)):
                value = value.replace(prefix, "<workspace-or-tool>")
        return value

    def save(self, force=False):
        if self.receipt.get("commands"):
            row = self.redact(public_value(self.receipt["commands"][-1]))
            signature = (row["record_id"], object_sha(row))
            if signature != self._last_signature:
                self.command_journal.append({"event": "command_revision", "recorded_at_utc": utc_now(), "command": row})
                self._last_signature = signature
        now = time.monotonic()
        if force or now - self._snapshot_at >= 2:
            write_json(self.root_output / "receipt.json", self.redact(public_value(self.receipt)))
            self._snapshot_at = now

    def remaining(self):
        cap = self.protocol["freeze"]["overall_wall_cap_seconds"] if self.protocol else 60
        left = cap - (time.monotonic_ns() - self.started_ns) / 1e9
        require(left > 0, "overall_wall_cap_exceeded")
        return left

    def command(self, name, arguments, timeout=20, **kwargs):
        self.check_immutable()
        return super().command(name, arguments, timeout=min(timeout, self.remaining()), **kwargs)

    def checked(self, name, arguments, timeout=20, **kwargs):
        row = self.command(name, arguments, timeout, **kwargs)
        self.require_command(row, name + "_failed")
        return row

    def check_immutable(self, include_source=False):
        if self.protocol is not None:
            require(self.admission_deadline is not None and datetime.now(timezone.utc) < self.admission_deadline,
                    "owner_release_admission_expired_during_capture")
            require(digest(self.protocol_path) == self.protocol_file_sha and digest(self.plan_path) == self.plan_file_sha,
                    "protocol_or_plan_changed_during_capture")
            require(digest(self.args.admission) == self.admission_file_sha, "admission_changed_during_capture")
            require(all(digest(path) == expected for path,expected in self.pilot_evidence_hashes.items()),
                    "pilot_journal_changed_during_formal_capture")
            if include_source:
                require(all(digest(path) == expected for path,expected in self.source_file_hashes.items()),
                        "source_closure_changed_during_capture")

    def validate(self):
        self.allowed = self.admission()  # No ADB before this succeeds.
        admission = load(self.args.admission)
        require(admission.get("device_serial_sha256") == self.receipt["device_serial_sha256"], "admission_requires_exact_device_hash")
        require(admission.get("expires_at_utc"), "admission_requires_bounded_expiry")
        self.admission_deadline = datetime.fromisoformat(admission["expires_at_utc"].replace("Z", "+00:00"))
        require(self.admission_deadline.tzinfo is not None, "admission_expiry_requires_timezone")
        self.admission_file_sha = digest(self.args.admission)
        self.protocol, self.plan = load(self.protocol_path), load(self.plan_path)
        self.protocol_file_sha, self.plan_file_sha = digest(self.protocol_path), digest(self.plan_path)
        p, f = self.protocol, self.protocol.get("freeze", {})
        require(p.get("state") == "FROZEN_BEFORE_PILOT" and self.plan.get("state") == "FROZEN_ORDER_BEFORE_PILOT", "requires_frozen_protocol_and_plan")
        require(p.get("device_execution") == "AUTHORIZED_CURRENT_BATCH", "device_execution_not_authorized_by_frozen_protocol")
        from paired_suite_v2 import freeze_errors
        require(not freeze_errors(p, self.plan), "suite_protocol_or_plan_not_ready:" + ",".join(freeze_errors(p, self.plan)))
        require(self.plan.get("protocol_canonical_sha256") == object_sha(p), "plan_protocol_identity_mismatch")
        for name in ("single_task_timeout_seconds", "session_wall_cap_seconds", "overall_wall_cap_seconds"):
            require(uint(f.get(name), 1, 86400), "missing_or_unbounded_limit:" + name)
        require(uint(f.get("maximum_environment_waits"), 1, 20), "invalid_maximum_environment_waits")
        require(isinstance(f.get("cadence"), dict) and isinstance(f.get("resource_budgets"), dict), "missing_cadence_or_budgets")
        require(f["cadence"].get("battery_source", "sysfs") in ("sysfs", "dumpsys_battery"), "invalid_frozen_battery_source")
        qualification = f.get("functional_receipts")
        require(isinstance(qualification, list) and qualification, "locatable_functional_receipts_required")
        verified_receipts = []
        for item in qualification:
            relative, expected_hash = item.get("path"), item.get("sha256")
            require(isinstance(relative, str) and ":" not in relative and "\\" not in relative and
                    not PurePosixPath(relative).is_absolute() and ".." not in PurePosixPath(relative).parts,
                    "functional_receipt_requires_portable_relative_path")
            path = (self.root / relative).resolve()
            require(path.is_relative_to(self.root) and path.is_file() and SHA.fullmatch(str(expected_hash)), "functional_receipt_missing_or_invalid_identity")
            require(digest(path) == expected_hash, "functional_receipt_hash_mismatch")
            receipt = load(path)
            expected = item.get("expected")
            if expected is not None:
                require(isinstance(expected, dict) and expected and
                        all(key in receipt and type(receipt[key]) is type(value) and receipt[key] == value for key,value in expected.items()),
                        "functional_receipt_success_contract_failed")
            else:
                require(receipt.get("all_passed") is True or receipt.get("status") == "PASS" or
                        receipt.get("state") in ("PASS", "PASS_HOST_FUNCTIONAL_ONLY", "PASS_CAPTURE_ONLY"),
                        "functional_receipt_not_successful")
            verified_receipts.append({"path": relative, "sha256": expected_hash,
                                      "success_contract": expected or "EXPLICIT_COMMON_SUCCESS_STATE"})
        self.receipt["functional_receipts_verified"] = verified_receipts
        if "source_closure" in f:
            source_closure = f["source_closure"]
            require(isinstance(source_closure, list) and source_closure, "source_closure_cannot_be_empty")
            for item in source_closure:
                relative, expected = item.get("path"), item.get("sha256")
                require(isinstance(relative, str) and ":" not in relative and "\\" not in relative and
                        not PurePosixPath(relative).is_absolute() and ".." not in PurePosixPath(relative).parts,
                        "source_requires_portable_relative_path")
                path = (self.root / relative).resolve()
                require(path.is_relative_to(self.root) and path.is_file() and SHA.fullmatch(str(expected)) and
                        digest(path) == expected, "source_closure_identity_mismatch")
                self.source_file_hashes[path] = expected
            self.receipt["source_closure_verified"] = source_closure
        profile = f.get("device_profile", {})
        require(profile.get("selected_cpu", profile.get("cpu")) == 2 and profile.get("abi") == "arm64-v8a" and uint(profile.get("page_bytes"), 4096), "invalid_frozen_device_profile")
        require(SHA.fullmatch(str(profile.get("boot_id_sha256", ""))), "missing_frozen_device_boot_identity")
        self.cpu = profile.get("selected_cpu", profile.get("cpu"))
        if f.get("host_clock_epoch_id"):
            require(isinstance(f["host_clock_epoch_id"], str) and SAFE_NAME.fullmatch(f["host_clock_epoch_id"]), "invalid_frozen_host_clock_epoch")
            self.host_epoch = f["host_clock_epoch_id"]
            self.receipt["host_clock_epoch_id"] = self.host_epoch
        closure = f.get("artifact_closure")
        require(isinstance(closure, list) and closure, "missing_artifact_closure")
        self.artifacts = {}
        names = set()
        for item in closure:
            role, name, relative = item.get("role"), item.get("name"), item.get("path")
            require(isinstance(role, str) and SAFE_NAME.fullmatch(role) and role not in self.artifacts, "invalid_or_duplicate_artifact_role")
            require(isinstance(name, str) and SAFE_NAME.fullmatch(name) and name not in names, "invalid_or_duplicate_device_filename")
            require(isinstance(relative, str) and ":" not in relative and "\\" not in relative and not PurePosixPath(relative).is_absolute()
                    and ".." not in PurePosixPath(relative).parts, "artifact_requires_portable_relative_path")
            path = (self.root / relative).resolve()
            require(path.is_relative_to(self.root) and path.is_file() and SHA.fullmatch(str(item.get("sha256", ""))), "artifact_missing_or_invalid_hash")
            require(digest(path) == item["sha256"], "local_artifact_hash_mismatch:" + role)
            if item.get("executable") or name.endswith(".so"):
                verify_arm64_elf(path)
            self.artifacts[role] = {**item, "bytes": path.stat().st_size}
            names.add(name)
        entries = self.plan.get("entries")
        require(isinstance(entries, list) and entries, "plan_has_no_entries")
        require(len({e.get("task_id") for e in entries}) == len(entries), "duplicate_task_identity")
        for entry in entries:
            require(entry.get("family") in p["families"] and entry.get("variant") in p["families"][entry["family"]]["variants"], "unmapped_planned_variant")
        families = set(p["families"])
        if self.phase == "formal":
            from paired_suite_v2 import pilot_eligibility, analyze_family
            from paired_analysis_v2 import load_journal
            path = self.pilot_journal_path
            require(path is not None and path.is_relative_to(self.root) and path.is_file(), "formal_requires_complete_pilot_journal")
            records, journal_errors = load_journal(path)
            require(not journal_errors, "pilot_journal_hash_chain_invalid")
            self.pilot_evidence_hashes[path] = digest(path)
            allowed, skipped, identities = set(), [], []
            for family in sorted(families):
                report = analyze_family(p, self.plan, records, "pilot", family, journal_errors)
                gate = pilot_eligibility(p, self.plan, report, family)
                if gate["formal_allowed"]:
                    allowed.add(family)
                else:
                    require(report.get("decision") == "STOP_FAMILY_PILOT_PRECISION_INSUFFICIENT" and
                            not report.get("functional_failures") and not report.get("incomplete_reasons") and
                            not report.get("resource_budget_failures") and not report.get("freeze_errors") and
                            report.get("phase") == "pilot" and report.get("family") == family and
                            report.get("protocol_canonical_sha256") == object_sha(p) and
                            report.get("plan_canonical_sha256") == object_sha(self.plan) and
                            report.get("attempted_tasks") == report.get("planned_tasks") == 64,
                            "pilot_failure_stops_entire_suite:" + family)
                    skipped.append({"family": family, "state": "SKIPPED_BY_PILOT_STOP", "reason": report["decision"]})
                identities.append({"family": family, "recomputed_report_canonical_sha256": object_sha(report), "gate": gate})
            require(self.enabled_families is None or self.enabled_families == allowed, "enabled_families_must_match_bound_pilot_gates")
            self.enabled_families = allowed
            require(allowed, "no_formal_family_admitted_by_pilot")
            self.receipt.update({"pilot_reports_recomputed": identities, "skipped_by_pilot_stop": skipped,
                                 "pilot_journal": {"path": path.relative_to(self.root).as_posix(), "sha256": self.pilot_evidence_hashes[path]}})
        else:
            require(self.enabled_families is None or self.enabled_families == families, "pilot_cannot_silently_skip_a_family")
            self.enabled_families = families
        self.receipt["enabled_families"] = sorted(self.enabled_families)
        self.receipt.update({"protocol_file_sha256": self.protocol_file_sha, "plan_file_sha256": self.plan_file_sha,
                             "protocol_canonical_sha256": object_sha(p), "plan_canonical_sha256": object_sha(self.plan),
                             "artifact_closure": list(self.artifacts.values())})

    def boot(self, label):
        row = self.checked(label, ["shell", "cat", "/proc/sys/kernel/random/boot_id"], hash_stdout=True)
        require(row.get("stdout_value_is_boot_uuid"), "invalid_device_boot_UUID")
        return row["stdout_value_sha256"]

    def profile_gate(self):
        profile = self.protocol["freeze"]["device_profile"]
        require(self.boot("boot-before") == profile["boot_id_sha256"], "device_boot_changed_since_freeze")
        query = self.checked("profile", ["shell", "getprop ro.product.cpu.abilist; getconf PAGESIZE; getprop ro.build.version.sdk; grep '^Cpus_allowed_list:' /proc/self/status"])
        lines = query["stdout"].splitlines()
        require(len(lines) == 4 and "arm64-v8a" in lines[0].split(",") and lines[1] == str(profile["page_bytes"])
                and lines[2] == str(profile["api"]), "device_profile_differs_from_frozen_profile")
        match = re.fullmatch(r"Cpus_allowed_list:\s*([0-9,-]+)", lines[3])
        require(match, "allowed_CPU_set_unavailable")
        allowed = set()
        for interval in match[1].split(","):
            ends = [int(x) for x in interval.split("-")]
            require(len(ends) in (1, 2) and 0 <= ends[0] <= ends[-1] <= 255, "invalid_allowed_CPU_set")
            allowed.update(range(ends[0], ends[-1] + 1))
        require(self.cpu in allowed, "selected_CPU_is_not_allowed")
        self.receipt["device_profile_observed"] = {"abi_list": lines[0], "page_bytes": int(lines[1]), "api": int(lines[2]), "allowed_cpus": sorted(allowed)}

    def stage(self):
        require(REMOTE.fullmatch(self.remote), "invalid_owned_directory")
        self.checked("create-owned-directory", ["shell", "mkdir", self.remote])
        self.receipt["device_directory_created"] = True
        for role, item in self.artifacts.items():
            target = self.remote + "/" + item["name"]
            self.checked("push-" + role, ["push", str(self.root / item["path"]), target])
            self.verify_remote(role)
            if item.get("executable"):
                self.checked("chmod-" + role, ["shell", "chmod", "700", target])
        deployment = {"schema_version": 2, "state": "STAGED", "device_serial_sha256": self.receipt["device_serial_sha256"],
                      "boot_id_sha256": self.protocol["freeze"]["device_profile"]["boot_id_sha256"], "device_directory": self.remote,
                      "protocol_canonical_sha256": object_sha(self.protocol), "artifacts": list(self.artifacts.values()), "created_at_utc": utc_now()}
        path = self.root_output / "deployment.json"
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(deployment, ensure_ascii=False, indent=2) + "\n")
        return deployment

    def verify_remote(self, role):
        item = self.artifacts[role]
        text = self.checked("hash-" + role, ["shell", "sha256sum", self.remote + "/" + item["name"]])["stdout"].split()
        require(text and text[0] == item["sha256"], "device_artifact_hash_mismatch:" + role)

    def deployment(self):
        require(self.deployment_path is not None, "stage_deployment_required")
        deployment = load(self.deployment_path)
        require(deployment.get("state") == "STAGED" and deployment.get("device_serial_sha256") == self.receipt["device_serial_sha256"]
                and deployment.get("boot_id_sha256") == self.protocol["freeze"]["device_profile"]["boot_id_sha256"]
                and deployment.get("protocol_canonical_sha256") == object_sha(self.protocol)
                and deployment.get("artifacts") == list(self.artifacts.values()), "deployment_identity_mismatch")
        require(REMOTE.fullmatch(str(deployment.get("device_directory", ""))), "invalid_staged_directory")
        self.remote = deployment["device_directory"]
        self.receipt["device_directory"] = self.remote
        self.receipt["deployment_file_sha256"] = digest(self.deployment_path)
        for role in self.artifacts:
            self.verify_remote(role)

    def environment(self, label, before):
        cadence = self.protocol["freeze"]["cadence"]
        window = cadence.get("background_window_seconds", .4) if before else None
        row = self.checked(label, ["shell", environment_script(self.cpu, window, cadence.get("battery_source", "sysfs"))],
                           capture_private_inventory=True)
        value = parse_environment(row["_inventory_stdout"], self.cpu, self.protocol["environment_limits"], cadence, before)
        self.publish_inventory(row, value)
        return value

    def command_for(self, entry):
        variant = self.protocol["families"][entry["family"]]["variants"][entry["variant"]]
        executable = self.artifacts[variant["executable_role"]]
        source = self.artifacts[variant["input_role"]]
        require(executable.get("executable") is True, "task_executable_role_not_executable")
        mapping = {"@input": self.remote + "/" + source["name"], "@output": self.remote + "/result.pgm"}
        args = [mapping.get(token, token) for token in variant["argv"]]
        require(all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_./:+,=-]+", x) for x in args), "unsafe_native_argument")
        return ["shell", "LD_LIBRARY_PATH=" + self.remote + " " + " ".join(shlex.quote(x) for x in [self.remote + "/" + executable["name"]] + args)]

    def one(self, entry):
        row = {**entry, "schema_version": 2, "experiment_id": self.protocol["experiment_id"],
               "attempt_id": self.protocol["attempt_id"], "estimand_id": self.protocol["estimand_id"],
               "plan_canonical_sha256": object_sha(self.plan), "host_clock_epoch_id": self.host_epoch,
               "success": False, "native": None, "resources": None, "environment": None, "environment_after": None,
               "command_result": None, "gate_reasons": [], "clocks": {}, "started_at_utc": utc_now()}
        try:
            freeze, cadence = self.protocol["freeze"], self.protocol["freeze"]["cadence"]
            attempts = []
            for attempt in range(freeze["maximum_environment_waits"]):
                begin = time.monotonic_ns()
                environment = self.environment("pre-" + str(self.task_journal.sequence) + "-" + str(attempt), True)
                end = time.monotonic_ns()
                attempts.append({"environment": environment, "host_begin_monotonic_ns": begin, "host_end_monotonic_ns": end})
                row["environment"], row["environment_attempts"] = environment, attempts
                row["clocks"].update({"observation_begin_monotonic_ns": begin, "observation_end_monotonic_ns": end})
                if environment["valid"]:
                    break
                if attempt + 1 < freeze["maximum_environment_waits"]:
                    self.sleep_bounded(cadence.get("cooldown_seconds", 1))
            require(row["environment"]["valid"], "environment_gate_failed_after_bounded_waits")
            launch = time.monotonic_ns()
            row["clocks"]["launch_monotonic_ns"] = launch
            result = self.command("native-" + str(self.task_journal.sequence), self.command_for(entry), freeze["single_task_timeout_seconds"])
            row["clocks"]["completion_monotonic_ns"] = time.monotonic_ns()
            row["command_result"] = result
            if result["timed_out"]:
                row["remote_process_state_after_timeout"] = "UNKNOWN_DO_NOT_ASSUME_STOPPED"
            self.require_command(result, "native_command_failed", functional_test=True)
            native = strict_json(result["stdout"])
            require(isinstance(native, dict), "native_output_not_an_object")
            row["native"] = native
            family = self.protocol["families"][entry["family"]]
            expected = {**family.get("semantic_identity", {}), **family.get("required_success", {}),
                        **family["variants"][entry["variant"]].get("native_match", {})}
            for field, value in expected.items():
                require(field in native and type(native.get(field)) is type(value) and native.get(field) == value, "native_contract_mismatch:" + field)
            require(all(native.get(field) == self.cpu and type(native.get(field)) is int for field in ("cpu_before", "cpu_after")), "native_affinity_contract_failed")
            require(uint(native.get("total_ns"), 1, freeze["single_task_timeout_seconds"] * 1_000_000_000), "invalid_native_elapsed")
            from paired_suite_v2 import resource_view, resource_failures
            row["resources"] = resource_view(self.protocol, entry["family"], native)
            require(not resource_failures(self.protocol, entry["family"], entry["variant"], native), "native_resource_budget_exceeded")
            after = self.environment("post-" + str(self.task_journal.sequence), False)
            row["environment_after"] = after
            require(after["valid"], "post_environment_gate_failed")
            require(after["thermal_observed_monotonic_ns"] >= row["environment"]["device_end_monotonic_ns"], "device_clock_reversed_during_task")
            gap = (launch - row["clocks"]["observation_end_monotonic_ns"]) / 1e9
            require(gap * 1e9 <= cadence["observation_to_launch_max_ns"], "observation_to_launch_gap_exceeded")
            row["success"] = True
        except (Blocked, Failed, ValueError, TypeError, OSError, RuntimeError, KeyboardInterrupt) as error:
            row["gate_reasons"].append(type(error).__name__ + ":" + str(error))
            if isinstance(error, KeyboardInterrupt):
                row["remote_process_state_after_interrupt"] = "UNKNOWN_DO_NOT_ASSUME_STOPPED"
        finally:
            row["completed_at_utc"] = utc_now()
            self.task_journal.append(self.redact(row))
            self.receipt["native_tasks_recorded"] += 1
            self.save(force=True)
        require(row["success"], "capture_stopped_after_preserved_task_failure")
        return row

    def sleep_bounded(self, seconds):
        require(isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 0 <= seconds <= 3600, "invalid_bounded_wait")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_immutable()
            delay = min(1, deadline-time.monotonic(), self.remaining())
            if delay > 0:
                time.sleep(delay)

    def run(self):
        error = None
        try:
            self.validate()
            self.occupancy("before", self.allowed)
            self.profile_gate()
            if self.phase == "stage":
                self.stage()
            else:
                self.deployment()
                entries = self.selected_entries()
                require(entries, "no_planned_entries_for_selection")
                current_window, session_start = None, None
                for entry in entries:
                    if current_window != entry["window_id"]:
                        current_window = entry["window_id"]
                        schedule = datetime.fromisoformat(entry["scheduled_window_start_utc"].replace("Z", "+00:00"))
                        require(schedule.tzinfo is not None, "window_schedule_requires_timezone")
                        delay = (schedule - datetime.now(timezone.utc)).total_seconds()
                        if delay > 0:
                            self.sleep_bounded(delay)
                        delay = (schedule - datetime.now(timezone.utc)).total_seconds()
                        require(delay >= -self.protocol["freeze"]["cadence"].get("window_start_lateness_max_seconds", 60), "missed_prescheduled_window")
                        session_start = time.monotonic()
                        self.occupancy("window-" + str(entry["window_id"]), self.allowed)
                    require(time.monotonic()-session_start < self.protocol["freeze"]["session_wall_cap_seconds"], "session_wall_cap_exceeded")
                    self.one(entry)
                self.occupancy("after", self.allowed)
                for role in self.artifacts:
                    self.verify_remote(role)
            require(self.boot("boot-after") == self.protocol["freeze"]["device_profile"]["boot_id_sha256"], "boot_changed_during_capture")
            self.check_immutable(include_source=True)
            self.receipt.update({"state": "PASS_CAPTURE_ONLY", "all_passed": True})
        except (Blocked, Failed, ValueError, TypeError, OSError, RuntimeError, KeyboardInterrupt) as failure:
            error = failure
            self.receipt.update({"state": "INTERRUPTED" if isinstance(failure, KeyboardInterrupt) else "FAILED_OR_BLOCKED",
                                 "all_passed": False, "reason": type(failure).__name__ + ":" + str(failure)})
        finally:
            self.receipt["completed_at_utc"] = utc_now()
            self.save(force=True)
            self.task_journal.close()
            self.command_journal.close()
        if error:
            raise error
        return self.receipt

    def selected_entries(self):
        """Keep frozen physical order; excluded family is recorded, never replaced."""
        return [entry for entry in self.plan["entries"] if entry["phase"] == self.phase and
                entry["family"] in self.enabled_families and
                (self.window_id is None or entry["window_id"] == self.window_id)]
