#!/usr/bin/env python3
"""Run the existing arm64 safety tests only after a recorded owner release.

This runner does not acquire a shared device lock. It requires a current admission
JSON with state RELEASED_FOR_MEMORY and nonempty owner_evidence, then checks for
other diagnostic/test processes before and after the bounded run. Optional
explicitly_idle_processes entries must contain an exact name and owner evidence.
No process is killed, unfrozen or restarted; no package/settings/global policy is
changed. A local ADB timeout does not prove that its remote child has stopped.

Example (run only after the original owner has released the device):
  python tools/run_arm64_validation.py --adb <adb> --serial <serial> \
    --admission <released.json> --build-dir build/android-arm64-v8a \
    --out-dir artifacts/device-20261030/arm64

The output directory must not already exist. Device serials are redacted from
public records; the device directory and every pushed binary are recorded.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import struct
import subprocess
import time


BINARIES = ("libmgmem.so", "mgmem_core_tests", "mgmem_c_consumer", "mgmem_cpp_consumer")
CORE_CASES = {
    "abi_and_checked_arithmetic", "system_budget_layout_and_rollback",
    "provider_oom_and_exact_pairing", "allocation_reservation_close_race",
    "borrow_access_and_handle_identity", "late_cross_thread_lease_after_close",
    "arena_retained_reset_and_trim", "growth_coexistence_peak",
    "drain_completion_and_timeout", "drain_poll_during_blocked_collection",
    "concurrent_system_stress",
}
BUSY_PROCESS = re.compile(
    r"gpudiagnostic|androidx\.test|uiautomator|mgmem_|"
    r"(?:^|[ /._-])(?:ncnn|rife|mgv|mgc)[^ ]*(?:bench|test)|"
    r"meguviz[^ ]*(?:benchmark|diagnostic)", re.IGNORECASE)
REMOTE_DIRECTORY = re.compile(r"^/data/local/tmp/meguvizmemory-arm64-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")


class Blocked(RuntimeError):
    pass


class Failed(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def nonempty_evidence(value) -> bool:
    return isinstance(value, (str, list, dict)) and bool(value)


def parse_last_json(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.lstrip().startswith("{"):
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                pass
    raise Failed("test_output_has_no_final_JSON_object")


def verify_arm64_elf(path: Path) -> None:
    with path.open("rb") as stream:
        header = stream.read(64)
    if (len(header) < 64 or header[:4] != b"\x7fELF" or header[4:6] != b"\x02\x01" or
            struct.unpack_from("<H", header, 18)[0] != 183):
        raise Blocked("build_input_is_not_little_endian_AArch64_ELF: " + path.name)


class Runner:
    def __init__(self, args, output: Path):
        self.args = args
        self.output = output
        self.counter = 0
        self.remote = "/data/local/tmp/meguvizmemory-arm64-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(6)
        if not REMOTE_DIRECTORY.fullmatch(self.remote):
            raise RuntimeError("invalid_generated_device_directory")
        self.receipt = {
            "schema_version": 1, "scope": "ARM64_PHYSICAL_CPU_FUNCTIONAL_ONLY",
            "started_at_utc": utc_now(), "state": "NOT_RUN", "all_passed": False,
            "device_serial_sha256": hashlib.sha256(args.serial.encode("utf-8")).hexdigest(),
            "device_directory": self.remote, "device_directory_created": False,
            "shared_device_lock_acquired": False,
            "admission_rule": "Original owner release plus observational process gates; no future-exclusion guarantee.",
            "commands": [], "binary_manifest": [], "tests": {},
            "qualification": {
                "arm64_functional": "NOT_RUN", "product_performance": "NOT_STARTED",
                "actual_16kb_loader": "UNAVAILABLE_UNTIL_OBSERVED",
                "mte": "UNAVAILABLE_NO_PROCESS_MODE_OR_CONTROLLED_FAULT_TEST",
                "pfn_pmu_energy": "UNAVAILABLE",
            },
            "cleanup": "Dedicated device directory is retained; no automatic deletion or process termination.",
        }

    def redact(self, value):
        if isinstance(value, str):
            return value.replace(self.args.serial, "<redacted-device-serial>")
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        return value

    def save(self) -> None:
        data = (json.dumps(self.redact(self.receipt), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        pending = self.output / "receipt.pending.json"
        pending.write_bytes(data)
        pending.replace(self.output / "receipt.json")

    def command(self, name: str, arguments: list[str], timeout: float = 20,
                hash_stdout: bool = False, capture_private_inventory: bool = False) -> dict:
        self.counter += 1
        command = [str(self.args.adb), "-s", self.args.serial] + arguments
        started = time.monotonic()
        timed_out, launch_error = False, None
        try:
            done = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
            code = done.returncode
            stdout_bytes, stderr_bytes = done.stdout, done.stderr
        except subprocess.TimeoutExpired as error:
            code, timed_out = None, True
            stdout_bytes, stderr_bytes = error.stdout or b"", error.stderr or b""
            self.receipt["remote_process_state_after_timeout"] = "UNKNOWN_DO_NOT_ASSUME_STOPPED"
        except OSError as error:
            code, stdout_bytes, stderr_bytes, launch_error = None, b"", b"", str(error)
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        stdout_hash = hashlib.sha256(stdout.strip().encode("utf-8")).hexdigest() if hash_stdout else None
        public_stdout = "sha256:" + stdout_hash + "\n" if hash_stdout else self.redact(stdout)
        public_stderr = self.redact(stderr)
        if capture_private_inventory:
            # Full process/activity inventories can reveal unrelated user apps.
            # Only hashes enter the initial persisted row. The raw text is returned
            # in a separate in-memory copy and never appended to the receipt.
            public_stdout = "private_inventory_sha256:" + hashlib.sha256(stdout_bytes).hexdigest() + "\n"
            public_stderr = "private_stderr_sha256:" + hashlib.sha256(stderr_bytes).hexdigest() + "\n"
        row = {
            "record_id": self.counter, "name": name, "command": self.redact(command), "timeout_seconds": timeout,
            "exit_code": code, "timed_out": timed_out, "launch_error": self.redact(launch_error),
            "stdout": public_stdout, "stderr": public_stderr,
            "diagnostic_runner_elapsed_seconds": time.monotonic() - started,
        }
        if capture_private_inventory:
            row.update({"capture_policy": "RAW_IN_MEMORY_ONLY_FILTERED_PUBLIC_OUTPUT",
                        "raw_stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
                        "raw_stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest()})
        if hash_stdout:
            row["stdout_value_sha256"] = stdout_hash
            row["stdout_value_is_boot_uuid"] = bool(re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", stdout.strip()))
        prefix = f"{self.counter:03d}-{name}"
        row["stdout_file"] = prefix + ".stdout.txt"
        row["stderr_file"] = prefix + ".stderr.txt"
        (self.output / row["stdout_file"]).write_bytes(public_stdout.encode("utf-8"))
        (self.output / row["stderr_file"]).write_bytes(public_stderr.encode("utf-8"))
        self.receipt["commands"].append(row)
        self.save()
        if capture_private_inventory:
            private_row = dict(row)
            private_row["_inventory_stdout"] = stdout
            private_row["_inventory_stderr"] = stderr
            return private_row
        return row

    def publish_inventory(self, row: dict, selected: dict) -> None:
        """Persist only the already filtered inventory, never its full raw text."""
        public = next(item for item in self.receipt["commands"] if item["record_id"] == row["record_id"])
        public["selected_inventory"] = self.redact(selected)
        public["stdout"] = ("private_inventory_sha256:" + public["raw_stdout_sha256"] + "\n" +
                            json.dumps(self.redact(selected), ensure_ascii=False, indent=2) + "\n")
        (self.output / public["stdout_file"]).write_bytes(public["stdout"].encode("utf-8"))
        self.save()

    @staticmethod
    def require_command(row: dict, failure: str, functional_test: bool = False) -> None:
        if row["exit_code"] != 0 or row["timed_out"] or row["launch_error"]:
            raise (Failed if functional_test else Blocked)(failure)
        stdout = row.get("_inventory_stdout", row["stdout"])
        stderr = row.get("_inventory_stderr", row["stderr"])
        if "Permission Denial" in stdout or "Permission denied" in stderr:
            raise Blocked(failure + "_permission_denied")
        if re.search(r"(?im)^\s*(?:Unknown command|Can't find service|Error dumping service|Error:)", stdout + "\n" + stderr):
            raise Blocked(failure + "_unsupported_or_error_output")

    def admission(self) -> dict[str, str]:
        path = self.args.admission.resolve()
        if not path.is_file():
            raise Blocked("owner_release_admission_file_missing")
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise Blocked("owner_release_admission_invalid_JSON") from error
        self.receipt["admission"] = {"file_name": path.name, "sha256": digest(path)}
        if not isinstance(value, dict) or value.get("state") != "RELEASED_FOR_MEMORY":
            raise Blocked("owner_has_not_released_device_for_Memory")
        if not nonempty_evidence(value.get("owner_evidence")):
            raise Blocked("owner_release_has_no_evidence")
        serial_hash = value.get("device_serial_sha256")
        if serial_hash and serial_hash != self.receipt["device_serial_sha256"]:
            raise Blocked("admission_is_for_a_different_device")
        if value.get("expires_at_utc"):
            try:
                deadline = datetime.fromisoformat(value["expires_at_utc"].replace("Z", "+00:00"))
                if deadline.tzinfo is None or datetime.now(timezone.utc) >= deadline:
                    raise Blocked("owner_release_admission_expired")
            except (ValueError, TypeError) as error:
                raise Blocked("invalid_admission_expiry") from error
        allowed = {}
        idle = value.get("explicitly_idle_processes", [])
        if not isinstance(idle, list):
            raise Blocked("explicitly_idle_processes_must_be_an_array")
        for item in idle:
            if (not isinstance(item, dict) or not isinstance(item.get("name"), str) or
                    not item["name"] or not nonempty_evidence(item.get("evidence"))):
                raise Blocked("idle_process_needs_an_exact_name_and_owner_evidence")
            allowed[item["name"]] = item["evidence"]
        self.receipt["admission"].update({
            "state": value["state"], "owner_evidence": self.redact(value["owner_evidence"]),
            "explicitly_idle_processes": self.redact(idle),
        })
        self.save()
        return allowed

    def occupancy(self, phase: str, allowed: dict) -> None:
        processes = self.command(phase + "-processes", ["shell", "ps", "-A", "-o", "PID,NAME,ARGS"],
                                 capture_private_inventory=True)
        self.require_command(processes, phase + "_process_inventory_unavailable")
        parsed, busy, ignored = [], [], []
        for line in processes["_inventory_stdout"].splitlines():
            fields = line.split(None, 2)
            if len(fields) < 2 or not fields[0].isdigit():
                continue
            row = {"pid": int(fields[0]), "name": fields[1], "args": fields[2] if len(fields) == 3 else fields[1]}
            parsed.append(row)
            if BUSY_PROCESS.search(row["name"] + " " + row["args"]):
                (ignored if row["name"] in allowed or row["args"] in allowed else busy).append(row)
        if not parsed:
            raise Blocked(phase + "_process_inventory_could_not_be_parsed")
        self.publish_inventory(processes, {
            "scope": "All ps rows parsed in memory; only matching diagnostic/test rows are public.",
            "process_rows_parsed": len(parsed), "busy_processes": busy,
            "explicitly_idle_processes_observed": ignored,
        })
        instrument = self.command(phase + "-instrumentation", ["shell", "dumpsys", "activity"],
                                  capture_private_inventory=True)
        self.require_command(instrument, phase + "_instrumentation_inventory_unavailable")
        lines = instrument["_inventory_stdout"].splitlines()
        sections = []
        unknown_markers = []
        for index, line in enumerate(lines):
            if not re.search(r"\bmActiveInstrumentation\b|\bActiveInstrumentation\b", line):
                continue
            section = [line.rstrip()]
            marker = re.search(r"\bmActiveInstrumentation\s*[:=]\s*(.*)$", line)
            value = marker.group(1).strip() if marker else None
            empty = value in ("null", "None", "[]", "{}", "0")
            if not empty:
                indentation = len(line) - len(line.lstrip())
                for following in lines[index + 1:]:
                    if (not following.strip() or
                            len(following) - len(following.lstrip()) <= indentation):
                        break
                    section.append(following.rstrip())
            sections.append("\n".join(section))
            if marker and value and not empty and "ActiveInstrumentation{" not in value:
                unknown_markers.append(line.strip())
        active = sorted(set(line.strip() for section in sections for line in section.splitlines()
                            if "ActiveInstrumentation{" in line))
        # An explicit exact component may be declared idle by its original owner;
        # unknown active-instrumentation formats always fail closed.
        blocked_instrumentation = list(unknown_markers)
        for line in active:
            components = re.findall(r"[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+", line)
            if not components or not all(component in allowed for component in components):
                blocked_instrumentation.append(line)
        self.publish_inventory(instrument, {
            "scope": "Successful dumpsys activity; only mActiveInstrumentation/ActiveInstrumentation sections retained.",
            "instrumentation_sections": sections,
            "observation": ("NO_INSTRUMENTATION_MARKERS_IN_SUCCESSFUL_ACTIVITY_DUMP" if not sections else
                            "INSTRUMENTATION_MARKERS_EVALUATED"),
            "active_instrumentation": active, "blocked_instrumentation": blocked_instrumentation,
        })
        gate = {"checked_at_utc": utc_now(), "busy_processes": busy,
                "explicitly_idle_processes_observed": ignored,
                "active_instrumentation": active, "blocked_instrumentation": blocked_instrumentation,
                "instrumentation_scope": "Only mActiveInstrumentation/ActiveInstrumentation in a successful activity dump; no shared lock guarantee.",
                "result": "PASS_OBSERVATIONAL_ONLY" if not busy and not blocked_instrumentation else "BLOCKED"}
        self.receipt[phase + "_occupancy"] = gate
        self.save()
        if busy or blocked_instrumentation:
            raise Blocked(phase + "_other_test_or_diagnostic_owner_still_present")

    def run(self) -> None:
        allowed = self.admission()  # Strictly before the first ADB command.
        if not self.args.adb.is_file():
            raise Blocked("ADB_executable_missing")
        build = self.args.build_dir.resolve()
        for name in BINARIES:
            path = build / name
            if not path.is_file():
                raise Blocked("required_build_input_missing: " + name)
            verify_arm64_elf(path)
            self.receipt["binary_manifest"].append({"name": name, "bytes": path.stat().st_size, "sha256": digest(path)})
        self.save()
        self.occupancy("before", allowed)
        metadata = {}
        queries = {
            "model": ["shell", "getprop", "ro.product.model"],
            "api": ["shell", "getprop", "ro.build.version.sdk"],
            "abi_list": ["shell", "getprop", "ro.product.cpu.abilist"],
            "kernel": ["shell", "uname", "-r"],
            "page_bytes": ["shell", "getconf", "PAGESIZE"],
        }
        for key, command in queries.items():
            row = self.command("metadata-" + key.replace("_", "-"), command)
            self.require_command(row, "required_device_metadata_unavailable: " + key)
            metadata[key] = row["stdout"].strip()
        if "arm64-v8a" not in metadata["abi_list"].split(","):
            raise Blocked("target_does_not_advertise_arm64_v8a")
        if not metadata["api"].isdigit() or not metadata["page_bytes"].isdigit():
            raise Blocked("invalid_API_or_page_size_metadata")
        metadata["api"] = int(metadata["api"])
        metadata["page_bytes"] = int(metadata["page_bytes"])
        boot = self.command("boot-before", ["shell", "cat", "/proc/sys/kernel/random/boot_id"], hash_stdout=True)
        self.require_command(boot, "boot_identity_unavailable")
        if not boot["stdout_value_is_boot_uuid"]:
            raise Blocked("boot_identity_format_invalid")
        metadata["boot_id_sha256"] = boot["stdout_value_sha256"]
        self.receipt["device"] = metadata
        mkdir = self.command("create-owned-directory", ["shell", "mkdir", self.remote])
        self.require_command(mkdir, "exclusive_device_directory_creation_failed")
        self.receipt["device_directory_created"] = True
        self.save()
        for binary in self.receipt["binary_manifest"]:
            name = binary["name"]
            destination = self.remote + "/" + name
            push = self.command("push-" + name.replace(".", "-"), ["push", str(build / name), destination])
            self.require_command(push, "push_failed: " + name)
            checksum = self.command("hash-" + name.replace(".", "-"), ["shell", "sha256sum", destination])
            self.require_command(checksum, "device_hash_unavailable: " + name)
            actual = checksum["stdout"].split()[0] if checksum["stdout"].split() else ""
            binary["device_sha256"] = actual
            if actual != binary["sha256"]:
                raise Failed("pushed_binary_hash_mismatch: " + name)
        chmod = self.command("set-owned-test-execute-bits", ["shell", "chmod", "700"] +
                             [self.remote + "/" + name for name in BINARIES[1:]])
        self.require_command(chmod, "test_execute_bit_setup_failed")
        for name in BINARIES[1:]:
            # Both environment path and executable are generated constants with
            # a checked [A-Za-z0-9/_-] directory. No caller-provided shell text.
            row = self.command("test-" + name, ["shell", "LD_LIBRARY_PATH=" + self.remote,
                                               self.remote + "/" + name], timeout=60)
            self.require_command(row, "functional_test_failed: " + name, functional_test=True)
            result = parse_last_json(row["stdout"])
            if name == "mgmem_core_tests":
                cases = {line[5:].strip() for line in row["stdout"].splitlines() if line.startswith("PASS ")}
                valid = (result == {"suite": "mgmem-core-v1", "tests": 11, "checks": 145033, "status": "PASS"}
                         and cases == CORE_CASES)
            elif name == "mgmem_c_consumer":
                valid = (result.get("consumer") == "pure_c_abi" and result.get("success") is True and
                         result.get("backing_after_close") == 0)
            else:
                valid = result == {"suite": "mgmem-cpp-raii", "cases": 5, "status": "PASS"}
            self.receipt["tests"][name] = {"result": result, "verdict": "PASS" if valid else "FAIL"}
            self.save()
            if not valid:
                raise Failed("functional_result_contract_mismatch: " + name)
        self.occupancy("after", allowed)
        after_boot = self.command("boot-after", ["shell", "cat", "/proc/sys/kernel/random/boot_id"], hash_stdout=True)
        self.require_command(after_boot, "final_boot_identity_unavailable")
        if not after_boot["stdout_value_is_boot_uuid"] or after_boot["stdout_value_sha256"] != metadata["boot_id_sha256"]:
            raise Failed("device_rebooted_during_validation")
        self.receipt["qualification"]["arm64_functional"] = "PASS_FOR_RECORDED_API_PAGE_AND_ARTIFACTS"
        self.receipt["qualification"]["actual_16kb_loader"] = (
            "PASS_THIS_NATIVE_LIBRARY_AND_FUNCTIONAL_TESTS_ONLY" if metadata["page_bytes"] == 16384
            else "UNAVAILABLE_ACTUAL_DEVICE_PAGE_SIZE_IS_" + str(metadata["page_bytes"]))
        self.receipt["state"] = "PASS"
        self.receipt["all_passed"] = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, default=Path("build/android-arm64-v8a"))
    parser.add_argument("--out-dir", type=Path, default=Path("artifacts/device-20261030/arm64"))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", args.serial):
        parser.error("--serial must be a bounded device identifier")
    root = Path(__file__).resolve().parents[1]
    output = args.out_dir.resolve()
    if not output.is_relative_to(root):
        parser.error("--out-dir must remain inside this workspace")
    if output.exists():
        parser.error("--out-dir already exists; retain previous evidence and select a new directory")
    output.mkdir(parents=True, exist_ok=False)
    args.adb = args.adb.resolve()
    runner = Runner(args, output)
    code = 1
    try:
        runner.run()
        code = 0
    except Blocked as error:
        runner.receipt.update({"state": "BLOCKED", "reason": str(error)})
        code = 2
    except (Failed, OSError, ValueError, RuntimeError) as error:
        runner.receipt.update({"state": "FAIL", "reason": str(error)})
    finally:
        runner.receipt["completed_at_utc"] = utc_now()
        runner.save()
    print(json.dumps({"state": runner.receipt["state"], "all_passed": runner.receipt["all_passed"],
                      "receipt": str(output / "receipt.json")}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
