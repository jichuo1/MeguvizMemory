"""Private-profile runner tests using fakes; never invokes a device or adb."""
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import freeze_device_suite_v2 as old_freezer
import validate_arm64_consumers_profile_v2 as runner_module
from run_arm64_validation import Blocked


ROOT = runner_module.ROOT
PS = "PID NAME ARGS\n1 init /init\n2 system_server system_server\n3 zygote zygote\n4 logd logd\n5 adbd adbd\n"
ACTIVITY = ("ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)\n"
            "ACTIVITY MANAGER RUNNING PROCESSES (dumpsys activity processes)\n"
            "mActiveInstrumentation=null\n")


class ProfileRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "build", prefix="profile-runner-fake-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.admission = self.directory / "admission.json"
        self.serial = "SYNTHETIC-DEVICE-DO-NOT-CONNECT"
        self.admission.write_text(json.dumps({"state": "RELEASED_FOR_MEMORY", "owner_evidence": "synthetic only",
            "device_serial_sha256": hashlib.sha256(self.serial.encode()).hexdigest(),
            "expires_at_utc": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()}), encoding="utf-8")

    def runner(self):
        output = self.directory / "output"
        output.mkdir()
        args = SimpleNamespace(adb=Path(sys.executable), serial=self.serial, admission=self.admission,
            dex_input=self.directory / "input.dex", overall_timeout=600, task_timeout=30, cpu=2,
            input_profile=self.directory / "private-profile.json", input_profile_sha256="a" * 64)
        return runner_module.ConsumerRunner(args, output)

    def selected_profile(self):
        return {"profile_id": "b" * 32, "snapshot": {"roles": [],
                "toolchain_entries": {"fake": {"bytes": 1, "sha256": "c" * 64}}}}

    def test_status_and_reference_contract_replays_and_rejects_matching_wrong_values(self):
        rows = json.loads((ROOT / "artifacts/research-20261030/dex/functional_receipt.json").read_text())["runs"]
        self.assertEqual(len(rows), 32)
        for row in rows:
            native, case, variant = row["native"], row["case"], row["variant"]
            def checks(value):
                return runner_module.dex_checks(value, variant, case, native["diagnostic"], strict_contract=True)
            self.assertTrue(all(checks(native).values()), case)
            for bad in (None, False, 1.0, 100):
                changed = {**native, "expected_status": bad, "observed_status": bad}
                self.assertFalse(checks(changed)["case_status_contract"], (case, bad))
            changed = {**native, "callback_retains": 4, "callback_releases_including_caller": 5}
            self.assertFalse(checks(changed)["exact_callback_accounting"])
            changed = {**native, "unique_owned_backing_peak_bytes": False}
            self.assertFalse(checks(changed)["resource_counters_nonnegative_integers"])
            changed = {**native, "reserved_final_bytes": 1}
            self.assertFalse(checks(changed)["final_reserved_zero"])
            changed = {**native, "reserved_final_bytes": False}
            self.assertFalse(checks(changed)["final_reserved_zero"])
            for field in ("input_bytes", "logical_requested_bytes", "input_budget_bytes", "parser_budget_bytes"):
                changed = {**native, field: native[field] + 1}
                self.assertFalse(checks(changed)["fixed_input_and_budget_contract"])

    def test_reserved_integer_zero_rejects_boolean_and_nonzero(self):
        self.assertTrue(runner_module.zero_integer(0))
        for value in (False, True, 1, None, 0.0):
            self.assertFalse(runner_module.zero_integer(value))

    def test_occupancy_fails_closed_for_truncation_permissions_and_unknown_markers(self):
        self.assertEqual(runner_module.parse_occupancy(PS, ACTIVITY, {})["result"], "PASS_OBSERVATIONAL_ONLY")
        for ps, activity in ((PS.rstrip(), ACTIVITY), ("PID NAME ARGS\n1 init /init\n", ACTIVITY),
                             (PS, ACTIVITY.replace("ACTIVITY MANAGER RUNNING PROCESSES", "MISSING")),
                             (PS, ACTIVITY + "Permission Denial\n")):
            with self.assertRaises(Blocked):
                runner_module.parse_occupancy(ps, activity, {})
        unknown = runner_module.parse_occupancy(PS, ACTIVITY + "mActiveInstrumentation=unrecognized\n", {})
        self.assertEqual(unknown["result"], "BLOCKED")
        busy = runner_module.parse_occupancy(PS + "6 mgmem_test mgmem_test\n", ACTIVITY, {})
        self.assertEqual(busy["result"], "BLOCKED")
        self.assertEqual(runner_module.parse_occupancy(PS + "6 mgmem_test mgmem_test\n", ACTIVITY,
                                                       {"mgmem_test": True})["result"], "PASS_OBSERVATIONAL_ONLY")

    def test_bounded_pipes_and_timeout_use_only_synthetic_processes(self):
        good = runner_module.bounded_adb([sys.executable, "-c", "import sys;sys.stdout.write('ok');sys.stderr.write('err')"], 5)
        self.assertEqual(good, (0, b"ok", b"err", False, False, False, "LOCAL_EXIT_CONFIRMED"))
        long_output = runner_module.bounded_adb([sys.executable, "-c",
            "import sys;sys.stdout.write('x' * (4*1024*1024+1));sys.stderr.write('y')"], 10)
        self.assertEqual(long_output[1:3], (b"", b""))
        self.assertTrue(long_output[4])
        timed = runner_module.bounded_adb([sys.executable, "-c", "import time;time.sleep(3)"], 0.2)
        self.assertTrue(timed[3])
        self.assertIsNone(timed[0])

    def test_continuous_flood_stops_transport_promptly_and_suppresses_output(self):
        program = "import sys\nwhile True:\n sys.stdout.buffer.write(b'x'*65536); sys.stdout.buffer.flush()"
        started = time.monotonic()
        result = runner_module.bounded_adb([sys.executable, "-c", program], 20)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(result[4])
        self.assertEqual(result[1:3], (b"", b""))
        self.assertEqual(result[6], "LOCAL_EXIT_CONFIRMED")

    def test_kill_failure_and_wait_timeout_are_bounded_and_unconfirmed(self):
        class StuckTransport:
            def __init__(self, fail_kill):
                self.stdout = io.BytesIO(b"x" * (runner_module.OUTPUT_LIMIT + 1))
                self.stderr = io.BytesIO(b"")
                self.returncode = None
                self.fail_kill = fail_kill
                self.wait_timeout = None
                self.kill_called = False
            def poll(self):
                return None
            def kill(self):
                self.kill_called = True
                if self.fail_kill:
                    raise OSError("private transport error")
            def wait(self, timeout):
                self.wait_timeout = timeout
                raise subprocess.TimeoutExpired("fake", timeout)
        for fail_kill in (False, True):
            process = StuckTransport(fail_kill)
            started = time.monotonic()
            with patch.object(runner_module.subprocess, "Popen", return_value=process):
                result = runner_module.bounded_adb(["synthetic-only"], 20)
            self.assertLess(time.monotonic() - started, 3)
            self.assertTrue(process.kill_called)
            self.assertEqual(process.wait_timeout, runner_module.TRANSPORT_STOP_WAIT_SECONDS)
            self.assertEqual(result[6], "LOCAL_EXIT_UNCONFIRMED")
            self.assertEqual(result[1:3], (b"", b""))

    def test_transport_deadline_covers_start_late_poll_and_pipe_completion(self):
        class ExitedTransport:
            def __init__(self):
                self.stdout = io.BytesIO(b"private output")
                self.stderr = io.BytesIO(b"")
                self.returncode = 0
                self.polled = 0
                self.killed = 0
            def poll(self):
                self.polled += 1
                return 0
            def kill(self):
                self.killed += 1
            def wait(self, timeout):
                return 0
        for clocks, expected_polls, expected_kills in (
                ((0.0, 1.1, 1.1), 0, 1),       # Startup crosses the deadline.
                ((0.0, 0.1, 1.1, 1.1), 1, 0),  # Exit was observed too late.
                ((0.0, 0.1, 0.2, 1.1), 1, 0)): # Pipe completion crossed it.
            process = ExitedTransport()
            times = iter(clocks)
            with patch.object(runner_module.subprocess, "Popen", return_value=process), \
                 patch.object(runner_module, "transport_clock", side_effect=lambda: next(times)):
                result = runner_module.bounded_adb(["synthetic-only"], 1)
            self.assertTrue(result[3])
            self.assertIsNone(result[0])
            self.assertEqual(result[1:3], (b"", b""))
            self.assertEqual(process.polled, expected_polls)
            self.assertEqual(process.killed, expected_kills)

    def test_unconfirmed_transport_is_blocked_and_marked_unknown(self):
        runner = self.runner()
        runner.admission_gate(first=True)
        response = (None, b"", b"", False, True, False, "LOCAL_EXIT_UNCONFIRMED")
        with patch.object(runner_module, "bounded_adb", return_value=response):
            with self.assertRaisesRegex(Blocked, "transport_unknown"):
                runner.command("fake-flood", ["shell", "true"])
        self.assertEqual(runner.receipt["remote_process_state_after_abort"], "UNKNOWN_DO_NOT_ASSUME_STOPPED")
        self.assertEqual(runner.receipt["commands"][-1]["local_transport_state"], "LOCAL_EXIT_UNCONFIRMED")

    def test_late_observed_success_cannot_pass_and_marks_remote_unknown(self):
        runner = self.runner()
        runner.admission_gate(first=True)
        late = (None, b"", b"", True, False, False, "LOCAL_EXIT_CONFIRMED")
        with patch.object(runner_module, "bounded_adb", return_value=late):
            with self.assertRaises(Blocked):
                runner.checked("fake-late", ["shell", "true"])
        self.assertEqual(runner.receipt["remote_process_state_after_abort"], "UNKNOWN_DO_NOT_ASSUME_STOPPED")
        self.assertIs(runner.receipt["all_passed"], False)

    def test_invalid_profile_blocks_before_any_fake_adb(self):
        runner = self.runner()
        with patch.object(runner_module.input_profile, "validate", side_effect=ValueError("private_path")), \
             patch.object(runner_module, "bounded_adb") as adb:
            with self.assertRaisesRegex(Blocked, "private_input_profile_validation_failed"):
                runner.run()
            adb.assert_not_called()

    def test_runtime_script_drift_is_rejected_at_checkpoint(self):
        runner = self.runner()
        selected = self.selected_profile()
        original_regular = runner_module.input_profile.regular
        changed = [False]
        def regular(path, boundary, *args, **kwargs):
            result = original_regular(path, boundary, *args, **kwargs)
            if changed[0] and Path(path) == ROOT / "tools" / "run_arm64_validation.py":
                return result[0], result[1] + b"drift"
            return result
        with patch.object(runner_module.input_profile, "validate", return_value=selected), \
             patch.object(runner_module.input_profile, "regular", side_effect=regular):
            runner.profile_checkpoint()
            changed[0] = True
            with self.assertRaisesRegex(Blocked, "profile_runtime_identity_changed"):
                runner.profile_checkpoint()

    def test_private_binding_persists_identities_and_rejects_forged_receipt(self):
        runner = self.runner()
        with patch.object(runner_module.input_profile, "validate", return_value=self.selected_profile()):
            runner.profile_checkpoint()
            binding = runner.receipt["input_binding"]
            self.assertEqual(binding["profile_sha256"], "a" * 64)
            self.assertEqual(binding["profile_id"], "b" * 32)
            expected_sources = {"tools/" + name for name in runner_module.PROFILE_RUNTIME_SOURCES} | set(runner_module.input_profile.HELPERS)
            self.assertEqual(set(binding["runtime_sources"]), expected_sources)
            manifest_path = runner.root_output / "private_runtime_tools.json"
            data = manifest_path.read_bytes()
            self.assertEqual(binding["runtime_tools_manifest_sha256"], hashlib.sha256(data).hexdigest())
            manifest = json.loads(data)
            self.assertEqual(set(manifest["tools"]), {"adb", "python"})
            for name, record in manifest["tools"].items():
                self.assertEqual({k: record[k] for k in ("bytes", "sha256")}, binding["runtime_tools"][name])
            self.assertNotIn(str(Path(sys.executable).resolve()), json.dumps(runner.redact(binding)))
            runner.receipt["input_binding"]["profile_sha256"] = "0" * 64
            with self.assertRaisesRegex(Blocked, "profile_runtime_identity_changed"):
                runner.profile_checkpoint()
            self.assertIs(runner.receipt["all_passed"], False)

    def test_missing_hash_and_modified_private_manifest_block(self):
        runner = self.runner()
        with patch.object(runner_module.input_profile, "validate", return_value=self.selected_profile()):
            runner.input_profile_sha256 = ""
            with self.assertRaises(Blocked):
                runner.profile_checkpoint()
        runner.input_profile_sha256 = "a" * 64
        # A failed first checkpoint left an incomplete private manifest; it cannot be reused.
        with patch.object(runner_module.input_profile, "validate", return_value=self.selected_profile()):
            with self.assertRaises(Blocked):
                runner.profile_checkpoint()
        runner2_dir = self.directory / "other"
        runner2_dir.mkdir()
        args = runner.args
        runner2 = runner_module.ConsumerRunner(args, runner2_dir)
        with patch.object(runner_module.input_profile, "validate", return_value=self.selected_profile()):
            runner2.profile_checkpoint()
            (runner2.root_output / "private_runtime_tools.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(Blocked, "private_tool_manifest_changed"):
                runner2.profile_checkpoint()
            self.assertIs(runner2.receipt["all_passed"], False)

    def test_missing_runtime_source_blocks_before_adb(self):
        runner = self.runner()
        original_regular = runner_module.input_profile.regular
        def regular(path, boundary, *args, **kwargs):
            if Path(path) == ROOT / "tools" / "run_arm64_validation.py":
                raise FileNotFoundError("private path")
            return original_regular(path, boundary, *args, **kwargs)
        with patch.object(runner_module.input_profile, "validate", return_value=self.selected_profile()), \
             patch.object(runner_module.input_profile, "regular", side_effect=regular), \
             patch.object(runner_module, "bounded_adb") as adb:
            with self.assertRaisesRegex(Blocked, "private_input_profile_validation_failed"):
                runner.run()
            adb.assert_not_called()
            self.assertIs(runner.receipt["all_passed"], False)

    def test_runtime_tool_drift_blocks(self):
        runner = self.runner()
        actual_external = runner_module.input_profile.external_file
        changed = [False]
        def external(path):
            value = actual_external(path)
            return {**value, "sha256": "0" * 64} if changed[0] else value
        with patch.object(runner_module.input_profile, "validate", return_value=self.selected_profile()), \
             patch.object(runner_module.input_profile, "external_file", side_effect=external):
            runner.profile_checkpoint()
            changed[0] = True
            with self.assertRaisesRegex(Blocked, "private_tool_manifest_changed"):
                runner.profile_checkpoint()

    def test_dex_source_read_failure_does_not_create_snapshot(self):
        runner = self.runner()
        with patch.object(runner_module.input_profile, "regular", side_effect=ValueError("unsafe source")):
            with self.assertRaises(ValueError):
                runner.snapshot_dex_input()
        self.assertFalse((runner.cache / "input.dex").exists())

    def test_dex_snapshot_is_private_exclusive_and_identity_checked(self):
        runner = self.runner()
        body = b"x" * 10540184
        source = self.directory / "source.dex"
        source.write_bytes(body)
        runner.args.dex_input = source
        with patch.object(runner_module, "INPUT_SHA256", hashlib.sha256(body).hexdigest()):
            snapshot = runner.snapshot_dex_input()
            self.assertTrue(snapshot.is_relative_to(ROOT / "build"))
            self.assertEqual(snapshot.read_bytes(), body)
            with self.assertRaises(FileExistsError):
                runner.snapshot_dex_input()
        snapshot.unlink()
        with self.assertRaisesRegex(Blocked, "real_DEX_input_identity_mismatch"):
            runner.snapshot_dex_input()
        self.assertFalse(snapshot.exists())

    def test_private_input_receipt_path_and_bytes_are_bound(self):
        runner = self.runner()
        asset = runner.cache / "input.dex"
        asset.write_bytes(b"dex")
        runner.assets = {"input.dex": asset}
        runner.binary_names = ()
        item = {"name": "input.dex", "private_path": asset.relative_to(ROOT).as_posix(),
                "bytes": 3, "sha256": hashlib.sha256(b"dex").hexdigest()}
        runner.check_asset_identity(item)
        with self.assertRaisesRegex(Blocked, "private_input_receipt_mismatch"):
            runner.check_asset_identity({**item, "private_path": "build/forged.dex"})
        asset.write_bytes(b"bad")
        with self.assertRaisesRegex(Blocked, "profile_asset_drift"):
            runner.check_asset_identity(item)

    def test_stage_rejects_asset_changed_during_fake_push(self):
        runner = self.runner()
        asset = self.directory / "mgmem_core_tests"
        asset.write_bytes(b"original")
        identity = {"bytes": 8, "sha256": hashlib.sha256(b"original").hexdigest()}
        runner.assets = {asset.name: asset}
        runner.binary_names = (asset.name,)
        runner.profile_role_identities = {asset.name: identity}
        runner.receipt["deployment_manifest"] = [{"name": asset.name, "role": "core_safety", **identity}]
        names = []
        def checked(name, *_args, **_kwargs):
            names.append(name)
            if name.startswith("push-"):
                asset.write_bytes(b"modified")
            return {"stdout": identity["sha256"]}
        with patch.object(runner, "profile_checkpoint"), patch.object(runner, "checked", side_effect=checked):
            with self.assertRaisesRegex(Blocked, "profile_role_drift"):
                runner.stage()
        self.assertNotIn("set-owned-executable-bits", names)

    def test_stage_rejects_remote_hash_mismatch(self):
        runner = self.runner()
        asset = self.directory / "mgmem_core_tests"
        asset.write_bytes(b"original")
        identity = {"bytes": 8, "sha256": hashlib.sha256(b"original").hexdigest()}
        runner.assets = {asset.name: asset}
        runner.binary_names = (asset.name,)
        runner.profile_role_identities = {asset.name: identity}
        runner.receipt["deployment_manifest"] = [{"name": asset.name, "role": "core_safety", **identity}]
        with patch.object(runner, "profile_checkpoint"), \
             patch.object(runner, "checked", return_value={"stdout": "0" * 64}):
            with self.assertRaisesRegex(runner_module.Failed, "remote_stage_hash_mismatch"):
                runner.stage()

    def test_profile_scope_rejected_by_legacy_performance_freezer(self):
        runner = self.runner()
        receipt = self.directory / "functional.json"
        receipt.write_text(json.dumps({"state": "PASS", "all_passed": True, "scope": runner.receipt["scope"]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "wrong_functional_scope"):
            old_freezer.freeze(receipt, self.admission, self.directory / "not-created")
        self.assertFalse((self.directory / "not-created").exists())
        self.assertEqual(runner.receipt["qualification"]["performance"], "NOT_STARTED")

    def test_fake_adb_output_limit_blocks_before_followup(self):
        runner = self.runner()
        runner.admission_gate(first=True)
        with patch.object(runner_module, "bounded_adb", return_value=(None, b"", b"", False, True, False, "LOCAL_EXIT_CONFIRMED")) as adb:
            with self.assertRaisesRegex(Blocked, "adb_output_exceeded_limit_or_pipe_failed"):
                runner.command("fake-inventory", ["shell", "ps"])
            self.assertEqual(adb.call_count, 1)

    def test_remote_digest_mismatch_blocks_at_checkpoint(self):
        runner = self.runner()
        runner.receipt["deployment_manifest"] = [{"name": "mgmem_core_tests", "sha256": "1" * 64}]
        with patch.object(runner, "checked", return_value={"stdout": "2" * 64 + " remote"}) as adb:
            with self.assertRaisesRegex(Blocked, "remote_asset_drift"):
                runner.device_asset_checkpoint()
            self.assertEqual(adb.call_count, 1)

    def test_image_replay_rejects_boolean_counter_and_1080_cases_share_input(self):
        rows = json.loads((ROOT / "artifacts/research-20261030/image/image_oracle_receipt.json").read_text())["commands"]
        for row in rows:
            native = row["result"]
            spec = {"provider": row["provider"], "layout": row["layout"], "frames": row["frames"],
                    "extra": row["extra"], "width": native["width"], "height": native["height"],
                    "checksum": native["checksum"] // row["frames"]}
            if not native["success"]:
                spec["expected_error"] = native["error"]
            self.assertTrue(all(runner_module.image_checks(native, spec).values()), row["case_id"])
        row = next(row for row in rows if row["result"]["success"])
        native = {**row["result"], "completed_frames": True}
        spec = {"provider": row["provider"], "layout": row["layout"], "frames": row["frames"],
                "extra": row["extra"], "width": native["width"], "height": native["height"],
                "checksum": native["checksum"] // row["frames"]}
        self.assertFalse(runner_module.image_checks(native, spec)["resource_counters_nonnegative_integers"])
        perf = [spec for spec in runner_module.make_specs() if spec["group"] == "image_perf"]
        self.assertEqual(len(perf), 8)
        self.assertEqual({spec["input"] for spec in perf}, {"image-1920x1080.ppm"})

    def test_fake_run_checkpoints_and_publication_failure_never_mark_pass(self):
        runner = self.runner()
        runner.input_binding = {"synthetic": True}
        runner.receipt["input_binding"] = {"synthetic": True}
        events = []
        def local_assets():
            events.append("local")
            runner.receipt["deployment_manifest"] = []
        def occupancy(phase, allowed):
            events.append("occupancy-" + phase)
            runner.command("fake-adb-" + phase, ["shell", "true"])
        def set_profile():
            events.append("device-profile")
            runner.receipt["device"] = {"page_bytes": 4096}
        def one(spec):
            events.append("case-" + spec["group"])
            runner.receipt["completed_cases"] = dict(runner_module.EXPECTED_CASES)
        specs = [{"group": "dex"}, {"group": "image_workspace"}, {"group": "image_perf"}]
        with patch.object(runner, "local_assets", side_effect=local_assets), \
             patch.object(runner, "save"), \
             patch.object(runner, "occupancy", side_effect=occupancy), patch.object(runner, "profile", side_effect=set_profile), \
             patch.object(runner, "stage", side_effect=lambda: events.append("stage")), \
             patch.object(runner, "core_safety", side_effect=lambda: events.append("core")), \
             patch.object(runner, "device_asset_checkpoint", side_effect=lambda: events.append("remote-hash")), \
             patch.object(runner, "profile_checkpoint", side_effect=lambda: events.append("local-hash")), \
             patch.object(runner, "boot_gate"), patch.object(runner, "one", side_effect=one), \
             patch.object(runner_module, "make_specs", return_value=specs), \
             patch.object(runner_module, "bounded_adb", return_value=(0, b"", b"", False, False, False, "LOCAL_EXIT_CONFIRMED")) as adb, \
             patch.object(runner_module, "write_json", side_effect=OSError("synthetic publication failure")):
            with self.assertRaises(OSError):
                runner.run()
            self.assertGreater(adb.call_count, 0)
        self.assertLess(events.index("stage"), events.index("core"))
        self.assertGreaterEqual(events.count("remote-hash"), 4)
        self.assertGreaterEqual(events.count("local-hash"), 4)
        self.assertIsNot(runner.receipt["all_passed"], True)
        self.assertNotEqual(runner.receipt["qualification"]["functional"], "PASS_RECORDED_DEVICE_BOOT_PAGE_ARTIFACTS_ONLY")

    def test_fake_run_success_requires_all_checkpoint_groups(self):
        runner = self.runner()
        runner.input_binding = {"synthetic": True}
        runner.receipt["input_binding"] = {"synthetic": True}
        events = []
        def local_assets():
            runner.receipt["deployment_manifest"] = []
        def one(spec):
            events.append(spec["group"])
            runner.receipt["completed_cases"] = dict(runner_module.EXPECTED_CASES)
        specs = [{"group": "dex"}, {"group": "image_workspace"}, {"group": "image_perf"}]
        with patch.object(runner, "local_assets", side_effect=local_assets), patch.object(runner, "save"), \
             patch.object(runner, "occupancy"), \
             patch.object(runner, "profile", side_effect=lambda: runner.receipt.update({"device": {"page_bytes": 4096}})), \
             patch.object(runner, "stage"), patch.object(runner, "core_safety"), \
             patch.object(runner, "device_asset_checkpoint", side_effect=lambda: events.append("remote-check")), \
             patch.object(runner, "profile_checkpoint", side_effect=lambda: events.append("local-check")), \
             patch.object(runner, "boot_gate"), patch.object(runner, "one", side_effect=one), \
             patch.object(runner_module, "make_specs", return_value=specs):
            runner.run()
        self.assertTrue(runner.receipt["all_passed"])
        self.assertEqual(runner.receipt["qualification"]["performance"], "NOT_STARTED")
        self.assertEqual(events.count("remote-check"), 4)
        self.assertEqual(events.count("local-check"), 5)
        deployment = json.loads((runner.root_output / "deployment.json").read_text(encoding="utf-8"))
        self.assertEqual(deployment["state"], "FUNCTIONAL_STAGED_IDENTITY_ONLY")

    def test_final_receipt_write_failure_clears_success(self):
        instance = SimpleNamespace(receipt={"state": "PASS", "all_passed": True,
            "qualification": {"functional": "PASS_RECORDED_DEVICE_BOOT_PAGE_ARTIFACTS_ONLY", "16KB": "NOT_OBSERVED"}})
        instance.run = lambda: None
        instance.save = lambda **_: (_ for _ in ()).throw(OSError("private path"))
        output = self.directory / "new-output"
        args = ["profile-runner", "--adb", sys.executable, "--serial", self.serial,
                "--admission", str(self.admission), "--out-dir", str(output),
                "--input-profile", str(self.directory / "input.json"), "--input-profile-sha256", "a" * 64]
        captured = io.StringIO()
        with patch.object(sys, "argv", args), patch.object(runner_module, "ConsumerRunner", return_value=instance), \
             patch.object(sys, "stdout", captured):
            self.assertEqual(runner_module.main(), 1)
        self.assertIs(instance.receipt["all_passed"], False)
        self.assertEqual(instance.receipt["qualification"]["functional"], "INCOMPLETE")
        self.assertNotIn("private path", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
