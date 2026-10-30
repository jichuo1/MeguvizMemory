#!/usr/bin/env python3
"""Offline-only gates and contract replay; never calls an actual ADB binary."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from run_arm64_validation import Blocked
from run_paired_device import load_command_journal
from validate_arm64_consumers_v2 import ROOT, ConsumerRunner, core_checks, current_release, dex_checks, image_checks, make_specs, EXPECTED_CASES


class OfflineFunctionalGates(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "build", prefix="functional-v2-offline-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.admission = self.directory / "admission.json"
        self.serial = "SYNTHETIC-OFFLINE-ONLY"
        self.value = {"state": "RELEASED_FOR_MEMORY", "device_serial_sha256": hashlib.sha256(self.serial.encode()).hexdigest(),
                      "owner_evidence": "offline fixture, no real device", "explicitly_idle_processes": [],
                      "expires_at_utc": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()}
        self.save()

    def save(self):
        self.admission.write_text(json.dumps(self.value), encoding="utf-8")

    def runner(self):
        output = self.directory / "output"
        output.mkdir()
        args = SimpleNamespace(adb=Path(sys.executable), serial=self.serial, admission=self.admission,
                               dex_input=ROOT / "artifacts/local/perf-inputs/bilibili-9.17.0-classes.dex",
                               overall_timeout=600, task_timeout=30, cpu=2)
        return ConsumerRunner(args, output)

    def test_finite_unique_plan(self):
        specs = make_specs()
        self.assertEqual(len(specs), 196)
        self.assertEqual(len({s["id"] for s in specs}), 196)
        self.assertEqual({k: sum(s["group"] == k for s in specs) for k in EXPECTED_CASES}, EXPECTED_CASES)

    def test_fresh_core_contracts_replay_recorded_stdout_and_reject_corruption(self):
        folder = ROOT / "artifacts/arm64-20261030/safety"
        for program, name in (("mgmem_core_tests", "019-test-mgmem_core_tests.stdout.txt"),
                              ("mgmem_c_consumer", "020-test-mgmem_c_consumer.stdout.txt"),
                              ("mgmem_cpp_consumer", "021-test-mgmem_cpp_consumer.stdout.txt")):
            stdout = (folder / name).read_text()
            self.assertTrue(all(core_checks(program, stdout)[1].values()), program)
            broken = stdout.replace('"status":"PASS"', '"status":"FAIL"').replace('"success":true', '"success":false')
            self.assertNotEqual(broken, stdout)
            self.assertFalse(all(core_checks(program, broken)[1].values()), program)

    def test_current_release_accepted_without_device_query(self):
        self.assertEqual(current_release(self.admission, self.serial)["state"], "RELEASED_FOR_MEMORY")

    def test_nonreleased_is_blocked(self):
        self.value["state"] = "WAITING_FOR_OWNER"
        self.save()
        with self.assertRaises(Blocked):
            current_release(self.admission, self.serial)

    def test_expiry_is_mandatory(self):
        self.value.pop("expires_at_utc")
        self.save()
        with self.assertRaises(Blocked):
            current_release(self.admission, self.serial)

    def test_expired_release_is_blocked(self):
        self.value["expires_at_utc"] = "2020-01-01T00:00:00Z"
        self.save()
        with self.assertRaises(Blocked):
            current_release(self.admission, self.serial)

    def test_wrong_device_is_blocked(self):
        with self.assertRaises(Blocked):
            current_release(self.admission, "DIFFERENT-SYNTHETIC-ID")

    def test_no_commands_on_missing_release(self):
        runner = self.runner()
        self.value["state"] = "OCCUPIED"
        self.save()
        with patch("validate_arm64_consumers_v2.subprocess.run") as process:
            with self.assertRaises(Blocked):
                runner.run()
            process.assert_not_called()

    def test_changed_release_stops_before_next_command(self):
        runner = self.runner()
        runner.admission_gate(first=True)
        self.value["owner_evidence"] = "changed during hypothetical execution"
        self.save()
        with patch("validate_arm64_consumers_v2.subprocess.run") as process:
            with self.assertRaises(Blocked):
                runner.command("not-launched", ["shell", "true"])
            process.assert_not_called()

    def test_timeout_is_unknown_and_is_durable(self):
        runner = self.runner()
        runner.admission_gate(first=True)
        with patch("validate_arm64_consumers_v2.subprocess.run", side_effect=subprocess.TimeoutExpired(["fake-adb"], 30, output=b"partial")) as process:
            row = runner.command("synthetic-timeout", ["shell", "true"])
            self.assertEqual(process.call_count, 1)
        self.assertTrue(row["timed_out"])
        self.assertIsNone(row["exit_code"])
        self.assertEqual(runner.receipt["remote_process_state_after_timeout"], "UNKNOWN_DO_NOT_ASSUME_STOPPED")
        recovered = load_command_journal(runner.root_output / "functional-commands.jsonl")
        self.assertTrue(recovered[1]["timed_out"])

    def test_private_inventory_body_not_persisted(self):
        runner = self.runner()
        runner.admission_gate(first=True)
        secret = b"Unrelated.Private.App.Name"
        with patch("validate_arm64_consumers_v2.subprocess.run", return_value=subprocess.CompletedProcess([], 0, secret, b"")):
            row = runner.command("synthetic-inventory", ["shell", "ps"], capture_private_inventory=True)
        self.assertIn(secret.decode(), row["_inventory_stdout"])
        self.assertNotIn(secret.decode(), json.dumps(runner.receipt))
        for path in runner.root_output.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret, path.read_bytes())

    def test_dex_contract_replays_32_host_cases_and_rejects_bad_checksum(self):
        receipt = json.loads((ROOT / "artifacts/research-20261030/dex/functional_receipt.json").read_text())
        self.assertEqual(len(receipt["runs"]), 32)
        for row in receipt["runs"]:
            self.assertTrue(all(dex_checks(row["native"], row["variant"], row["case"], row["native"]["diagnostic"]).values()))
        row = receipt["runs"][0]
        native = {**row["native"], "result_checksum": row["native"]["result_checksum"] + 1}
        self.assertFalse(all(dex_checks(native, row["variant"], row["case"], native["diagnostic"]).values()))

    def test_image_contract_replays_156_host_cases_and_rejects_bad_close(self):
        receipt = json.loads((ROOT / "artifacts/research-20261030/image/image_oracle_receipt.json").read_text())
        self.assertEqual(len(receipt["commands"]), 156)
        for row in receipt["commands"]:
            native = row["result"]
            spec = {"provider": row["provider"], "layout": row["layout"], "frames": row["frames"], "extra": row["extra"],
                    "width": native["width"], "height": native["height"], "checksum": native["checksum"] // row["frames"]}
            if not native["success"]:
                spec["expected_error"] = native["error"]
            self.assertTrue(all(image_checks(native, spec).values()), row["case_id"])
        native = {**receipt["commands"][0]["result"], "closed_backing_bytes": 1}
        spec = {"provider": "direct", "layout": "full", "frames": 1, "extra": [], "width": 1, "height": 1,
                "checksum": native["checksum"]}
        self.assertFalse(all(image_checks(native, spec).values()))


if __name__ == "__main__":
    unittest.main()
