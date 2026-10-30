#!/usr/bin/env python3
"""Deterministic fake-ADB tests; never connects to a device or measures product work."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from device_capture_v2 import CaptureV2, Journal, environment_script, parse_environment
from paired_analysis_v2 import digest, load_journal
from paired_suite_v2 import default_protocol, make_plan, native_errors, resource_view
from test_paired_suite_v2 import records_fixture


ROOT = Path(__file__).resolve().parents[1]
SERIAL = "FAKE_ONLY_PRIVATE_SERIAL"
BOOT = "12345678-1234-1234-1234-123456789abc"
BOOT_SHA = hashlib.sha256(BOOT.encode()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def environmental_text(start=100, busy=False, before=True, temperature="300"):
    first, second = ("100 0 0 900 0 0 0 0", "140 0 0 900 0 0 0 0") if busy else (
        "100 0 0 900 0 0 0 0", "101 0 0 939 0 0 0 0")
    lines = []
    if before:
        lines += ["MGMEM_V2_STAT1_BEGIN", f"{start:.2f} 0.00", "cpu2 " + first, f"{start:.2f} 0.00",
                  "MGMEM_V2_STAT2_BEGIN", f"{start+.40:.2f} 0.00", "cpu2 " + second, f"{start+.40:.2f} 0.00"]
    lines += ["MGMEM_V2_BATTERY", temperature, "MGMEM_V2_THERMAL", "Thermal Status: 0",
              "MGMEM_V2_THERMAL_CLOCK", f"{start+.42:.2f} 0.00", "MGMEM_V2_FREQUENCY", "UNAVAILABLE",
              "MGMEM_V2_END", f"{start+.43:.2f} 0.00"]
    return "\n".join(lines) + "\n"


class FakeADB:
    def __init__(self, protocol, native_failure=None, busy=False):
        self.protocol, self.native_failure, self.busy = protocol, native_failure, busy
        self.calls, self.native_calls, self.device_clock = [], 0, 100.

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        arguments, stdout = argv[3:], ""
        if arguments[:2] == ["shell", "ps"]:
            stdout = "PID NAME ARGS\n1 init init\n22 com.private.app com.private.app\n"
        elif arguments[:3] == ["shell", "dumpsys", "activity"]:
            stdout = "ACTIVITY MANAGER\n  mActiveInstrumentation=[]\n"
        elif arguments == ["shell", "cat", "/proc/sys/kernel/random/boot_id"]:
            stdout = BOOT + "\n"
        elif arguments and arguments[0] == "shell" and arguments[-1].startswith("getprop "):
            stdout = "arm64-v8a,armeabi-v7a\n4096\n37\nCpus_allowed_list:\t0-7\n"
        elif arguments[:2] == ["shell", "sha256sum"]:
            name = arguments[-1].rsplit("/", 1)[1]
            item = next(a for a in self.protocol["freeze"]["artifact_closure"] if a["name"] == name)
            stdout = item["sha256"] + "  " + arguments[-1] + "\n"
        elif arguments and arguments[0] == "shell" and "MGMEM_V2_BATTERY" in arguments[-1]:
            self.device_clock += 1
            stdout = environmental_text(self.device_clock, self.busy, "MGMEM_V2_STAT1_BEGIN" in arguments[-1],
                                         "291" if "dumpsys battery |" in arguments[-1] else "300")
        elif arguments and arguments[0] == "shell" and arguments[-1].startswith("LD_LIBRARY_PATH="):
            self.native_calls += 1
            if self.native_failure == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"partial-native-output", stderr=b"")
            tokens = shlex.split(arguments[-1])
            family = "dex" if "--variant" in tokens else "image"
            if family == "dex":
                name = tokens[tokens.index("--variant") + 1]
            else:
                provider, layout = tokens[tokens.index("--provider") + 1], tokens[tokens.index("--layout") + 1]
                name = next(k for k,v in self.protocol["families"][family]["variants"].items()
                            if v["native_match"]["provider"] == provider and v["native_match"]["layout"] == layout)
            contract = self.protocol["families"][family]
            native = {**contract["semantic_identity"], **contract["required_success"],
                      **contract["variants"][name]["native_match"], "total_ns": 1000000,
                      "selected_cpu": 2, "cpu_before": 2, "cpu_after": 2}
            for field in contract["resource_fields"].values():
                native.setdefault(field, None if field in ("memory_metadata_bytes", "rss_pss_bytes", "energy") else 0)
            if self.native_failure == "checksum":
                native["result_checksum"] += 1
            stdout = json.dumps(native) + "\n"
        elif arguments[0] == "push" or arguments[:2] in (["shell", "mkdir"], ["shell", "chmod"]):
            pass
        else:
            raise AssertionError("unexpected fake ADB call: " + repr(arguments))
        return subprocess.CompletedProcess(argv, 0, stdout.encode(), b"")


class CaptureFixtures(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="capture-v2-fake-", dir=ROOT / "build")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.admission, self.protocol_path, self.plan_path = [self.base / name for name in ("admission.json", "protocol.json", "plan.json")]
        save(self.admission, {"state": "RELEASED_FOR_MEMORY", "owner_evidence": "FAKE TEST ONLY",
                              "device_serial_sha256": hashlib.sha256(SERIAL.encode()).hexdigest(),
                              "expires_at_utc": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
        self.protocol = default_protocol()
        self.protocol.update({"state": "FROZEN_BEFORE_PILOT", "device_execution": "AUTHORIZED_CURRENT_BATCH", "attempt_id": "FAKE_TEST_ONLY"})
        image = self.protocol["families"]["image"]["semantic_identity"]
        image.update({"source_sha256": "1"*64, "input_bytes": 32, "width": 1, "height": 1,
                      "result_checksum": 123, "output_hash_fnv64": 123, "gray_conversions": 3})
        for family in self.protocol["families"].values():
            family["resource_limits"] = {name:{"unique_owned_backing_peak_bytes":2**32} for name in family["variants"]}
        binary = b"\x7fELF\x02\x01" + bytes(12) + b"\xb7\x00" + bytes(44)
        artifacts = []
        for role, name, executable in (("dex_formal","dex",True),("image_formal","image",True),
                                       ("runtime","libmgmem.so",False),("dex_input","input.dex",False),
                                       ("image_input","input.ppm",False)):
            file = self.base / name
            file.write_bytes(binary if executable or name.endswith(".so") else b"FAKE INPUT NOT REAL DEX")
            artifacts.append({"role":role,"path":file.relative_to(ROOT).as_posix(),"name":name,
                              "sha256":hashlib.sha256(file.read_bytes()).hexdigest(),"executable":executable})
        start = datetime.now(timezone.utc) - timedelta(seconds=1)
        windows = [{"phase":"pilot" if i<4 else "formal", "window_id":"w%02d"%i,
                    "start_utc":(start+timedelta(seconds=120*i)).isoformat(),
                    "day_id":"FAKE_DAY", "environment_id":"FAKE_ENVIRONMENT"} for i in range(16)]
        self.protocol["freeze"].update({"device_profile":{"selected_cpu":2,"abi":"arm64-v8a","api":37,"page_bytes":4096,"boot_id_sha256":BOOT_SHA},
            "artifact_closure":artifacts,"functional_receipt_sha256":"2"*64,"scheduled_windows":windows,
            "single_task_timeout_seconds":5,"session_wall_cap_seconds":60,"overall_wall_cap_seconds":3600,
            "maximum_environment_waits":2,"cadence":{"busy_interval_min_ns":300000000,"busy_interval_max_ns":600000000,
                "observation_to_launch_max_ns":2000000000,"cooldown_seconds":0,"window_start_lateness_max_seconds":60},
            "resource_budgets":{"status":"FAKE_TEST_ONLY"}})
        self.plan = make_plan(self.protocol)
        qualification = self.base / "functional.json"
        save(qualification, {"all_passed": True, "scope": "SYNTHETIC_ONLY"})
        self.protocol["freeze"]["functional_receipts"] = [{"path": qualification.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(qualification.read_bytes()).hexdigest(), "expected": {"all_passed": True, "scope": "SYNTHETIC_ONLY"}}]
        self.plan = make_plan(self.protocol)
        save(self.protocol_path, self.protocol); save(self.plan_path, self.plan)

    def runner(self, name, phase="stage", deployment=None):
        return CaptureV2(ROOT, self.base / "fake-adb.exe", SERIAL, self.admission,
                         self.protocol_path, self.plan_path, self.base / name, phase,
                         "w00" if phase != "stage" else None, deployment)

    def staged(self, fake):
        with patch("run_arm64_validation.subprocess.run", fake):
            self.runner("stage").run()
        return self.base / "stage/deployment.json"

    def test_admission_rejected_before_first_adb(self):
        save(self.admission, {"state":"BUSY"})
        fake = FakeADB(self.protocol)
        with patch("run_arm64_validation.subprocess.run", fake), self.assertRaises(RuntimeError):
            self.runner("blocked").run()
        self.assertEqual(fake.calls, [])

    def test_unfrozen_protocol_rejected_before_first_adb(self):
        self.protocol["state"] = "DRAFT_NOT_FROZEN"; save(self.protocol_path,self.protocol)
        fake = FakeADB(self.protocol)
        with patch("run_arm64_validation.subprocess.run", fake), self.assertRaises(RuntimeError):
            self.runner("draft").run()
        self.assertEqual(fake.calls, [])

    def test_local_artifact_drift_rejected_before_first_adb(self):
        (self.base / "dex").write_bytes(b"CHANGED")
        fake = FakeADB(self.protocol)
        with patch("run_arm64_validation.subprocess.run", fake), self.assertRaises(RuntimeError):
            self.runner("drift").run()
        self.assertEqual(fake.calls, [])

    def test_functional_receipt_drift_rejected_before_first_adb(self):
        save(self.base / "functional.json", {"all_passed": False})
        fake = FakeADB(self.protocol)
        with patch("run_arm64_validation.subprocess.run", fake), self.assertRaises(RuntimeError):
            self.runner("functional-drift").run()
        self.assertEqual(fake.calls, [])

    def test_expired_admission_is_rejected_before_first_adb(self):
        admission = json.loads(self.admission.read_text())
        admission["expires_at_utc"] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        save(self.admission, admission)
        fake = FakeADB(self.protocol)
        with patch("run_arm64_validation.subprocess.run", fake), self.assertRaises(RuntimeError):
            self.runner("expired").run()
        self.assertEqual(fake.calls, [])

    def test_mid_capture_admission_expiry_blocks_next_adb(self):
        fake = FakeADB(self.protocol)
        runner = self.runner("expiry-mid")
        try:
            runner.validate()
            runner.admission_deadline = datetime.now(timezone.utc)-timedelta(seconds=1)
            with patch("run_arm64_validation.subprocess.run", fake), self.assertRaisesRegex(RuntimeError,"expired_during_capture"):
                runner.profile_gate()
            self.assertEqual(fake.calls, [])
        finally:
            runner.task_journal.close(); runner.command_journal.close()

    def test_formal_without_completed_pilot_rejected_before_adb(self):
        fake=FakeADB(self.protocol)
        with patch("run_arm64_validation.subprocess.run",fake),self.assertRaisesRegex(RuntimeError,"requires_complete_pilot_journal"):
            self.runner("no-pilot","formal").run()
        self.assertEqual(fake.calls,[])

    def test_precision_stop_skips_only_affected_family_without_plan_changes(self):
        rows=[r for r in records_fixture(self.protocol,self.plan) if r["phase"]=="pilot"]
        for row in rows:
            if row["variant"]=="D-Z0":
                row["native"]["total_ns"] *= 1+int(row["window_id"][1:])
                row["command_result"]["stdout"]=json.dumps(row["native"])
        path=self.base/"pilot.jsonl";journal=Journal(path)
        for row in rows:journal.append(row)
        journal.close()
        original=digest(self.plan)
        runner=CaptureV2(ROOT,self.base/"fake-adb.exe",SERIAL,self.admission,self.protocol_path,self.plan_path,
                         self.base/"formal-selection","formal",pilot_journal_path=path)
        try:
            runner.validate()
            self.assertEqual(len(runner.selected_entries()),192)
            self.assertTrue(all(row["family"]=="image" for row in runner.selected_entries()))
            self.assertEqual(digest(runner.plan),original)
            self.assertEqual(runner.receipt["skipped_by_pilot_stop"][0]["family"],"dex")
        finally:
            runner.task_journal.close();runner.command_journal.close()

    def test_partial_pilot_journal_never_admits_formal(self):
        path=self.base/"partial-pilot.jsonl";journal=Journal(path)
        for row in records_fixture(self.protocol,self.plan)[:32]:journal.append(row)
        journal.close();fake=FakeADB(self.protocol)
        runner=CaptureV2(ROOT,self.base/"fake-adb.exe",SERIAL,self.admission,self.protocol_path,self.plan_path,
                         self.base/"partial-pilot-capture","formal",pilot_journal_path=path)
        with patch("run_arm64_validation.subprocess.run",fake),self.assertRaisesRegex(RuntimeError,"pilot_failure_stops_entire_suite"):
            runner.run()
        self.assertEqual(fake.calls,[])

    def test_source_closure_drift_rejected_before_adb_and_at_final_check(self):
        path=self.base/"source.cc";path.write_text("original source")
        self.protocol["freeze"]["source_closure"]=[{"path":path.relative_to(ROOT).as_posix(),
            "sha256":hashlib.sha256(path.read_bytes()).hexdigest()}]
        self.plan=make_plan(self.protocol);save(self.protocol_path,self.protocol);save(self.plan_path,self.plan)
        runner=self.runner("source-closure")
        try:
            runner.validate();path.write_text("changed source")
            with self.assertRaisesRegex(RuntimeError,"source_closure_changed_during_capture"):
                runner.check_immutable(include_source=True)
        finally:
            runner.task_journal.close();runner.command_journal.close()
        fake=FakeADB(self.protocol)
        with patch("run_arm64_validation.subprocess.run",fake),self.assertRaisesRegex(RuntimeError,"source_closure_identity_mismatch"):
            self.runner("source-drift").run()
        self.assertEqual(fake.calls,[])

    def test_stage_and_complete_window_hash_chain_and_privacy(self):
        fake = FakeADB(self.protocol)
        deployment = self.staged(fake)
        with patch("run_arm64_validation.subprocess.run", fake):
            receipt = self.runner("capture", "pilot", deployment).run()
        rows, errors = load_journal(self.base / "capture/tasks.jsonl")
        self.assertFalse(errors)
        self.assertEqual(len(rows),32)
        self.assertEqual(fake.native_calls,32)
        self.assertTrue(receipt["all_passed"])
        for row in rows:
            self.assertEqual(json.loads(row["command_result"]["stdout"]),row["native"])
            self.assertFalse(native_errors(self.protocol,row,row["native"]))
            self.assertEqual(row["resources"],resource_view(self.protocol,row["family"],row["native"]))
            self.assertEqual(row["environment"]["clock_domain"],"DEVICE_BOOTTIME_PROC_UPTIME_NS")
        durable = "\n".join(p.read_text(encoding="utf-8") for folder in (self.base / "capture",self.base / "stage") for p in folder.rglob("*") if p.is_file())
        self.assertNotIn(SERIAL,durable)
        self.assertNotIn("com.private.app",durable)
        self.assertNotIn(str(ROOT),durable)
        self.assertFalse(any("kill" in " ".join(a) or "rm " in " ".join(a) for a in fake.calls))

    def test_wrong_checksum_is_preserved_then_stops_without_replacement(self):
        fake=FakeADB(self.protocol); deployment=self.staged(fake); fake.native_failure="checksum"
        with patch("run_arm64_validation.subprocess.run",fake),self.assertRaises(RuntimeError):
            self.runner("failed","pilot",deployment).run()
        rows,errors=load_journal(self.base/"failed/tasks.jsonl")
        self.assertFalse(errors);self.assertEqual(len(rows),1);self.assertFalse(rows[0]["success"])
        self.assertEqual(fake.native_calls,1)

    def test_timeout_preserves_partial_stdout_and_unknown_remote_state(self):
        fake=FakeADB(self.protocol); deployment=self.staged(fake); fake.native_failure="timeout"
        with patch("run_arm64_validation.subprocess.run",fake),self.assertRaises(RuntimeError):
            self.runner("timeout","pilot",deployment).run()
        rows,errors=load_journal(self.base/"timeout/tasks.jsonl")
        self.assertFalse(errors);self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["command_result"]["stdout"],"partial-native-output")
        self.assertEqual(rows[0]["remote_process_state_after_timeout"],"UNKNOWN_DO_NOT_ASSUME_STOPPED")
        self.assertEqual(fake.native_calls,1)

    def test_environment_cooldown_exhaustion_never_launches_native(self):
        fake=FakeADB(self.protocol); deployment=self.staged(fake); fake.busy=True
        with patch("run_arm64_validation.subprocess.run",fake),self.assertRaises(RuntimeError):
            self.runner("busy","pilot",deployment).run()
        rows,errors=load_journal(self.base/"busy/tasks.jsonl")
        self.assertFalse(errors);self.assertEqual(len(rows),1);self.assertEqual(fake.native_calls,0)
        self.assertEqual(len(rows[0]["environment_attempts"]),2)

    def test_existing_evidence_cannot_be_replaced(self):
        runner=self.runner("exists")
        runner.task_journal.close();runner.command_journal.close()
        with self.assertRaises(FileExistsError):
            self.runner("exists")

    def test_missing_thermal_not_zero(self):
        with self.assertRaises(RuntimeError):
            parse_environment(environmental_text().replace("Thermal Status: 0","NO STATUS"),2,
                              self.protocol["environment_limits"],self.protocol["freeze"]["cadence"],True)

    def test_battery_source_is_explicit_and_never_falls_back(self):
        sysfs=environment_script(2,.4)
        service=environment_script(2,.4,"dumpsys_battery")
        self.assertIn("cat /sys/class/power_supply/battery/temp",sysfs)
        self.assertNotIn("dumpsys battery",sysfs)
        self.assertIn("dumpsys battery | sed -n",service)
        self.assertNotIn("/sys/class/power_supply/battery/temp",service)
        with self.assertRaisesRegex(RuntimeError,"invalid_frozen_battery_source"):
            environment_script(2,.4,"automatic_fallback")

    def test_dumpsys_temperature_291_and_sysfs_300_are_labeled_distinctly(self):
        cadence=self.protocol["freeze"]["cadence"]
        service=parse_environment(environmental_text(temperature="291"),2,self.protocol["environment_limits"],
                                  {**cadence,"battery_source":"dumpsys_battery"},True)
        sysfs=parse_environment(environmental_text(),2,self.protocol["environment_limits"],cadence,True)
        self.assertEqual((service["battery_deci_c"],service["battery_source"]),(291,"dumpsys_battery"))
        self.assertEqual((sysfs["battery_deci_c"],sysfs["battery_source"]),(300,"sysfs"))
        self.assertFalse(service["battery_sensor_is_CPU_temperature"])

    def test_service_temperature_missing_duplicate_or_noninteger_is_rejected(self):
        cadence={**self.protocol["freeze"]["cadence"],"battery_source":"dumpsys_battery"}
        for value in ("", "291\n292", "291 invalid", "temperature: 291"):
            with self.subTest(value=value),self.assertRaises(RuntimeError):
                parse_environment(environmental_text(temperature=value),2,self.protocol["environment_limits"],cadence,True)

    def test_service_source_complete_fake_window_does_not_grant_qualification(self):
        self.protocol["freeze"]["cadence"]["battery_source"]="dumpsys_battery"
        self.plan=make_plan(self.protocol);save(self.protocol_path,self.protocol);save(self.plan_path,self.plan)
        fake=FakeADB(self.protocol);deployment=self.staged(fake)
        with patch("run_arm64_validation.subprocess.run",fake):
            receipt=self.runner("service-window","pilot",deployment).run()
        rows,errors=load_journal(self.base/"service-window/tasks.jsonl")
        self.assertFalse(errors);self.assertEqual(len(rows),32)
        self.assertTrue(all(r["environment"]["battery_source"]==r["environment_after"]["battery_source"]=="dumpsys_battery" for r in rows))
        self.assertTrue(all(r["environment"]["battery_deci_c"]==291 for r in rows))
        self.assertFalse(receipt["performance_qualified"])
        self.assertFalse(any("/sys/class/power_supply/battery/temp" in " ".join(call) for call in fake.calls))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(CaptureFixtures)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        save(args.output,{"schema_version":2,"scope":"FAKE_ADB_ONLY_NO_DEVICE_OR_PRODUCT_MEASUREMENTS",
                          "status":"PASS" if result.wasSuccessful() else "FAIL","tests":result.testsRun,
                          "failures":len(result.failures),"errors":len(result.errors),"actual_device_operations":0,
                          "collector_sha256":hashlib.sha256((ROOT/"tools/device_capture_v2.py").read_bytes()).hexdigest(),
                          "test_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    return 0 if result.wasSuccessful() else 1


if __name__=="__main__":
    raise SystemExit(main())
