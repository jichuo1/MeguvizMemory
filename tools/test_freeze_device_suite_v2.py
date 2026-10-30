#!/usr/bin/env python3
"""FAKE/offline freeze-contract regression: no ADB, SDK or private inputs."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import freeze_device_suite_v2 as target

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 30, 8, 30, tzinfo=timezone.utc)
class FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None): return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class FreezeTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "build").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="FAKE-freeze-", dir=ROOT / "build")
        self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall); patch.object(target, "ROOT", self.root).start(); patch.object(target, "datetime", FakeDatetime).start()
        self.paths = ["install/android-arm64-v8a/lib/libmgmem.so", "build/dex-borrow-v2-arm64/mgmem_dex_paired_v2",
                      "build/image-perf-v2-arm64/mgmem_image_perf", "artifacts/local/perf-inputs/bilibili-9.17.0-classes.dex", "build/image-perf-v2-checks/1920x1080.ppm"]
        sources = ["device_capture_v2", "paired_suite_v2", "paired_analysis_v2", "run_arm64_validation", "run_paired_device", "run_device_suite_v2", "freeze_device_suite_v2", "validate_arm64_consumers_v2"]
        for name in self.paths + ["tools/" + s + ".py" for s in sources] + ["consumers/image/perf.cpp", "consumers/image/CMakeLists.txt", "consumers/dex/paired_v2.cpp", "consumers/dex/CMakeLists.txt", "bench/paired_protocol_v2_draft.json"]:
            p = self.root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(("FAKE-NOT-AN-EXECUTABLE:" + name).encode())
        self.write("bench/image_perf_v2.json", {"input": {"source_sha256": "c" * 64, "input_bytes": 123, "width": 7, "height": 5}, "completion": {"frames": 3, "completed_frames": 3, "result_checksum": 1234, "output_hash_fnv64": 5678, "gray_conversions": 105}})
        self.admission = {"state": "RELEASED_FOR_MEMORY", "device_serial_sha256": "a" * 64, "expires_at_utc": (NOW + timedelta(hours=3)).isoformat(), "scope": "FAKE_OFFLINE_ONLY"}
        self.functional = {"state": "PASS", "all_passed": True, "scope": "ARM64_V2_CONSUMERS_FUNCTIONAL_ONLY_NOT_PERFORMANCE", "device_serial_sha256": "a" * 64, "completed_at_utc": (NOW - timedelta(seconds=1)).isoformat(), "completed_cases": {"dex": 32, "image_workspace": 156, "image_perf": 8}, "core_safety": {"status": "PASS", "tests": 3}, "admission": {}, "device": {"abi_list": "arm64-v8a,armeabi-v7a", "api": 33, "page_bytes": 4096, "requested_cpu": 2, "boot_id_sha256": "b" * 64}, "deployment_manifest": [{"sha256": target.file_sha(self.root / p)} for p in self.paths]}
        self.prereg = {"state": "FIRST_ATTEMPT_INCOMPLETE_ENGINEERING_WALL_CAP", "one_engineering_retry_predeclared": True, "additional_retry_after_retry1": False, "gain_estimates_examined": False, "original_protocol_sha256": "d" * 64}
        self.sync()
    def write(self, name, value):
        p = self.root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(value), encoding="utf-8"); return p
    def sync(self):
        self.ap = self.write("FAKE/admission.json", self.admission)
        self.functional["admission"]["sha256"] = target.file_sha(self.ap)
        self.fp = self.write("FAKE/functional.json", self.functional); self.rp = self.write("FAKE/prereg.json", self.prereg)
    def freeze(self, **kwargs):
        out = self.root / "FAKE-output"; result = target.freeze(self.fp, self.ap, out, **kwargs)
        return result, json.loads((out / "protocol.json").read_text()), json.loads((out / "plan.json").read_text())
    def retry(self, **kwargs):
        return self.freeze(**{"window_gap": 240, "session_cap": 210, "overall_cap": 4500, "engineering_retry": self.rp, **kwargs})

    def test_default_limits_fit_and_plan_is_finite(self):
        result, protocol, plan = self.freeze()
        self.assertEqual((protocol["freeze"]["session_wall_cap_seconds"], protocol["sampling"]["minimum_window_start_gap_seconds"]), (110, 120))
        self.assertEqual(result["planned_tasks"], 512); self.assertEqual(len({x["task_id"] for x in plan["entries"]}), 512)
        self.assertFalse(result["performance_qualified"])
    def test_retry_has_240_second_schedule_and_bound_identities(self):
        _, p, plan = self.retry(); starts = [datetime.fromisoformat(x["start_utc"]) for x in p["freeze"]["scheduled_windows"]]
        self.assertEqual(len(starts), 16); self.assertTrue(all((b-a).total_seconds() == 240 for a,b in zip(starts,starts[1:])))
        self.assertEqual((p["freeze"]["session_wall_cap_seconds"], p["freeze"]["overall_wall_cap_seconds"]), (210,4500))
        self.assertEqual(p["engineering_retry_preregistration_file_sha256"], target.file_sha(self.rp)); self.assertFalse(p["old_attempt_pooling"])
        self.assertTrue(p["attempt_id"].endswith("-retry1")); self.assertEqual(len(plan["entries"]), 512)
        for item in p["freeze"]["source_closure"] + p["freeze"]["artifact_closure"]: self.assertEqual(item["sha256"], target.file_sha(self.root/item["path"]))
    def test_input_and_resource_contracts_are_preserved(self):
        _, p, _ = self.retry(); self.assertEqual({x["role"] for x in p["freeze"]["artifact_closure"]}, {"library","dex_formal","image_formal","dex_input","image_input"})
        self.assertEqual(p["families"]["image"]["semantic_identity"]["input_bytes"], 123)
        dex = p["families"]["dex"]["resource_limits"]; image = p["families"]["image"]["resource_limits"]
        self.assertEqual([dex[x]["unique_owned_backing_peak_bytes"] for x in ("D-C0","D-Z0","D-C1","D-C2")], [10540184,0,10540184,10540184])
        self.assertTrue(all(x["external_source_capacity_bytes"] == 21080368 and x["parser_peak_owned_bytes"] == 268435456 and x["reserved_final_bytes"] == 0 for x in dex.values()))
        self.assertEqual([image[x]["unique_owned_backing_peak_bytes"] for x in ("I-DF","I-DR","I-SR","I-AR")], [10368000,8300160,8300160,8359936])
        self.assertTrue(all(x["external_source_capacity_bytes"] == 12441634 and x["steady_state_provider_allocations"] == x["reserved_final_bytes"] == 0 for x in image.values()))
    def test_incomplete_consumers_or_core_are_rejected(self):
        for field in ("dex","image_workspace","image_perf"):
            self.functional["completed_cases"][field] -= 1; self.sync()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError,"incomplete_consumer_cases"): self.freeze()
            self.functional["completed_cases"][field] += 1
        self.functional["core_safety"]["tests"] = 2; self.sync()
        with self.assertRaisesRegex(ValueError,"fresh_core_safety_required"): self.freeze()
    def test_wrong_device_is_rejected(self):
        self.functional["device_serial_sha256"] = "f" * 64; self.sync()
        with self.assertRaisesRegex(ValueError,"admission_device_mismatch"): self.freeze()
    def test_changed_admission_is_rejected(self):
        self.admission["extra_fake_field"] = 1; self.write("FAKE/admission.json",self.admission)
        with self.assertRaisesRegex(ValueError,"admission_changed_after_functional"): self.freeze()
    def test_retry_requires_one_no_extra_no_examined_gain(self):
        for key,value in (("one_engineering_retry_predeclared",False),("additional_retry_after_retry1",True),("gain_estimates_examined",True),("state","OTHER")):
            original=self.prereg[key];self.prereg[key]=value;self.sync()
            with self.subTest(field=key), self.assertRaisesRegex(ValueError,"bounded_predeclared_engineering_retry"): self.retry()
            self.prereg[key]=original
    def test_only_preregistered_retry_caps_are_accepted(self):
        for overrides in ({"window_gap":241},{"session_cap":209},{"overall_cap":4501}):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError,"retry_limits_differ"): self.retry(**overrides)
    def test_custom_caps_without_preregistration_are_rejected(self):
        with self.assertRaisesRegex(ValueError,"nondefault_limits_require"): self.freeze(window_gap=240,session_cap=210,overall_cap=4500)
    def test_session_cap_cannot_reach_window_gap(self):
        with self.assertRaisesRegex(ValueError,"session_cap_must_fit"): self.freeze(session_cap=120)
    def test_expiry_must_cover_entire_retry(self):
        self.admission["expires_at_utc"] = (NOW+timedelta(seconds=240+15*240+210)).isoformat();self.sync()
        with self.assertRaisesRegex(ValueError,"admission_does_not_cover_full_schedule"): self.retry()
    def test_stale_or_future_functional_receipt_is_rejected(self):
        for delta in (-1801,1):
            self.functional["completed_at_utc"]=(NOW+timedelta(seconds=delta)).isoformat();self.sync()
            with self.subTest(delta=delta), self.assertRaisesRegex(ValueError,"functional_qualification_too_old"): self.freeze()


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--output",type=Path); args=parser.parse_args()
    tests=unittest.defaultTestLoader.loadTestsFromTestCase(FreezeTests); names=[test.id().split(".")[-1] for test in tests]
    result=unittest.TextTestRunner(verbosity=1).run(tests)
    if args.output:
        receipt={"schema_version":2,"scope":"FAKE_OFFLINE_ONLY_SYNTHETIC_TEMPORARY_INPUTS_NO_DEVICE", "status":"PASS" if result.wasSuccessful() else "FAIL", "tests":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),"actual_device_operations":0,"test_names":names,"freeze_helper_sha256":target.file_sha(ROOT/"tools/freeze_device_suite_v2.py"),"test_sha256":target.file_sha(Path(__file__)),"performance_qualified":False}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open("x",encoding="utf-8",newline="\n") as stream: stream.write(json.dumps(receipt,ensure_ascii=False,indent=2)+"\n")
        print(json.dumps({"status":receipt["status"],"tests":result.testsRun,"actual_device_operations":0}))
    raise SystemExit(0 if result.wasSuccessful() else 1)
