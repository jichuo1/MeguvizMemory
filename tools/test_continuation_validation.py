#!/usr/bin/env python3
"""Offline corruption regressions using captured real success outputs.

No device commands, fabricated success records, or new runtime evidence. Public
hash-locked fixtures are preferred and require no ignored/private inputs. Running
this test never upgrades a failed/incomplete device run to a successful one.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import unittest

from run_continuation_device import android_checks, native_checks
from run_arm64_validation import Failed, parse_last_json


ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "artifacts/continuation-20261030"
PUBLIC = ROOT / "tests/fixtures/continuation-validation"
REPORT = re.compile(r"(?m)^INSTRUMENTATION_RESULT: report_json=(\{.*\})\r?$")


def read_capture(path):
    """Read exact saved output bytes; provenance is printed by main()."""
    raw = path.read_bytes()
    PROVENANCE.append({"path": path.relative_to(ROOT).as_posix(),
                       "sha256": hashlib.sha256(raw).hexdigest()})
    return raw.decode("utf-8")


PROVENANCE = []


def public_fixture(kind, provider=None):
    path = PUBLIC / "provenance.json"
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if (manifest.get("schema_version") != 1 or manifest.get("synthetic_success_records") is not False
            or manifest.get("new_device_execution") is not False):
        raise ValueError("invalid_public_golden_provenance")
    matches = [entry for entry in manifest["fixtures"]
               if entry["kind"] == kind and entry.get("provider") == provider]
    if len(matches) != 1:
        raise ValueError("one_public_golden_required")
    entry = matches[0]
    name = entry["file"]
    if Path(name).name != name:
        raise ValueError("public_golden_must_be_a_local_leaf")
    target = PUBLIC / name
    value = target.read_bytes()
    if len(value) != entry["bytes"] or hashlib.sha256(value).hexdigest() != entry["sha256"]:
        raise ValueError("public_golden_bytes_drift")
    if entry["representation"] not in ("EXACT_CAPTURE_BYTES", "DERIVED_FROM_ACTUAL_POSTREVIEW_RECEIPT"):
        raise ValueError("public_golden_representation_unknown")
    if entry["representation"] == "EXACT_CAPTURE_BYTES" and entry["original_output_sha256"] != entry["sha256"]:
        raise ValueError("raw_golden_not_exact_capture")
    PROVENANCE.append({"path": target.relative_to(ROOT).as_posix(), "sha256": entry["sha256"],
                       "representation": entry["representation"],
                       "original_receipt_path": entry["original_receipt_path"],
                       "original_receipt_sha256": entry["original_receipt_sha256"],
                       "provenance_manifest_sha256": hashlib.sha256(raw).hexdigest()})
    return value.decode("utf-8"), entry


class NativeValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if PUBLIC.is_dir():
            cls.dex = parse_last_json(public_fixture("dex")[0])
            cls.images = [json.loads(public_fixture("image", mode)[0]) for mode in ("system", "arena")]
            return
        commands = CAPTURE / "host-stability-smoke/commands"
        cls.dex = parse_last_json(read_capture(commands / "dex.stdout.txt"))
        cls.images = [parse_last_json(read_capture(commands / ("image-" + mode + ".stdout.txt")))
                      for mode in ("system", "arena")]

    def test_actual_native_successes_accepted(self):
        self.assertEqual(native_checks("dex", self.dex, 0)["state"], "PASS")
        for image in self.images:
            with self.subTest(provider=image["provider"]):
                self.assertIs(native_checks("image", image, 0), image)

    def test_native_incomplete_duration_rejected(self):
        dex = copy.deepcopy(self.dex); dex["actual_elapsed_ms"] = -1
        with self.assertRaisesRegex(Failed, "dex_minimum_duration"):
            native_checks("dex", dex, 0)
        image = copy.deepcopy(self.images[0]); image["active_loop_duration_ns"] = -1
        with self.assertRaisesRegex(Failed, "image_minimum_duration"):
            native_checks("image", image, 0)

    def test_dex_wrong_checksum_rejected(self):
        value = copy.deepcopy(self.dex); value["iterations"][-1]["result_checksum"] ^= 1
        with self.assertRaisesRegex(Failed, "dex_complete_work"):
            native_checks("dex", value, 0)

    def test_dex_missing_iteration_rejected(self):
        value = copy.deepcopy(self.dex); value["iterations"].pop()
        with self.assertRaisesRegex(Failed, "dex_iteration_count"):
            native_checks("dex", value, 0)

    def test_dex_close_and_callback_imbalance_rejected(self):
        for key, replacement, reason in (("memory_domain", "CLOSING", "dex_final_lifetime"),
                                         ("source_freed", False, "dex_final_lifetime"),
                                         ("origin_releases_including_caller", 0, "dex_callback_balance")):
            with self.subTest(key=key):
                value = copy.deepcopy(self.dex); value["iterations"][0][key] = replacement
                with self.assertRaisesRegex(Failed, reason):
                    native_checks("dex", value, 0)

    def test_dex_import_double_charge_rejected(self):
        value = copy.deepcopy(self.dex)
        value["iterations"][0]["source_plus_input_unique_capacity_bytes"] *= 2
        with self.assertRaisesRegex(Failed, "dex_unique_capacity"):
            native_checks("dex", value, 0)

    def test_image_checksum_or_work_mismatch_rejected(self):
        for key in ("checksum", "gray_conversions"):
            with self.subTest(key=key):
                value = copy.deepcopy(self.images[0]); value[key] += 1
                with self.assertRaisesRegex(Failed, "image_complete_checksum_and_work"):
                    native_checks("image", value, 0)

    def test_image_count_or_closed_zero_mismatch_rejected(self):
        for key in ("frames_completed", "normal_cycles_closed_zero"):
            with self.subTest(key=key):
                value = copy.deepcopy(self.images[1]); value[key] -= 1
                with self.assertRaisesRegex(Failed, "image_final_resources"):
                    native_checks("image", value, 0)

    def test_image_budget_and_timeout_mismatch_rejected(self):
        for key in ("zero_budget_denials", "explicit_restore_admissions", "held_close_timeouts"):
            with self.subTest(key=key):
                value = copy.deepcopy(self.images[1]); value[key] -= 1
                with self.assertRaisesRegex(Failed, "image_budget_and_close"):
                    native_checks("image", value, 0)


class AndroidValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if PUBLIC.is_dir():
            text, entry = public_fixture("android")
            matches = REPORT.findall(text)
            if len(matches) != 1:
                raise ValueError("one_actual_public_android_report_required")
            value = json.loads(matches[0]); expected = entry["expected"]
            android_checks(text, 0, expected["api"], expected["marker_after"])
            cls.stdout, cls.value, cls.api, cls.marker = text, value, expected["api"], expected["marker_after"]
            PROVENANCE[-1]["original_build_receipt_sha256"] = expected["original_build_receipt_sha256"]
            return
        candidates = sorted(CAPTURE.glob("device-api34-smoke*/commands/android.stdout.txt"), reverse=True)
        candidates += sorted(CAPTURE.glob("device-arm64-smoke*/commands/android.stdout.txt"), reverse=True)
        for path in candidates:
            raw = path.read_bytes(); text = raw.decode("utf-8")
            matches = REPORT.findall(text)
            if len(matches) != 1:
                continue
            value = json.loads(matches[0])
            if value.get("status") != "PASS":
                continue
            # No mutation produces our golden success; it must be present on
            # disk and pass the actual current consumer validator unchanged.
            capture_receipt = json.loads((path.parent.parent / "receipt.json").read_text(encoding="utf-8"))
            build_hash = capture_receipt["APK"]["build_receipt_sha256"]
            builds = [candidate for candidate in (CAPTURE / "android-build").glob("*.json")
                      if hashlib.sha256(candidate.read_bytes()).hexdigest() == build_hash]
            if not builds:
                raise unittest.SkipTest("Android PASS output has no matching real build receipt available")
            build = json.loads(builds[0].read_text(encoding="utf-8"))
            marker = build["r8_evidence"]["marker_after"]
            android_checks(text, 0, value["api"], marker)
            cls.stdout, cls.value, cls.api, cls.marker = text, value, value["api"], marker
            PROVENANCE.append({"path": path.relative_to(ROOT).as_posix(),
                               "sha256": hashlib.sha256(raw).hexdigest()})
            PROVENANCE.append({"path": builds[0].relative_to(ROOT).as_posix(), "sha256": build_hash})
            return
        raise unittest.SkipTest("No captured real Android PASS smoke yet; no success fixture fabricated")

    def changed(self, key, replacement):
        value = copy.deepcopy(self.value); value[key] = replacement
        return REPORT.sub(lambda match: "INSTRUMENTATION_RESULT: report_json=" + json.dumps(value), self.stdout)

    def test_actual_android_success_accepted(self):
        self.assertEqual(android_checks(self.stdout, 0, self.api, self.marker)["status"], "PASS")

    def test_android_incomplete_duration_rejected(self):
        with self.assertRaisesRegex(Failed, "android_duration_identity"):
            android_checks(self.changed("actual_duration_ms", -1), 0, self.api, self.marker)

    def test_android_nonzero_closed_backing_rejected(self):
        with self.assertRaisesRegex(Failed, "android_qualification_scope"):
            android_checks(self.changed("final_backing_bytes_each_domain", 1), 0, self.api, self.marker)

    def test_android_incomplete_cycle_count_rejected(self):
        with self.assertRaisesRegex(Failed, "android_duration_identity"):
            android_checks(self.changed("iterations_completed", 2), 0, self.api, self.marker)

    def test_android_wrong_finish_code_rejected(self):
        value = re.sub(r"(?m)^INSTRUMENTATION_CODE: 0\s*$", "INSTRUMENTATION_CODE: 1", self.stdout)
        with self.assertRaisesRegex(Failed, "instrumentation_not_finished_OK"):
            android_checks(value, 0, self.api, self.marker)

    def test_android_unrenamed_marker_rejected(self):
        value = self.changed("r8_marker_runtime_name", "io.github.jichuo1.meguviz.memory.android.R8ConsumerMarker")
        with self.assertRaisesRegex(Failed, "r8_not_renamed_as_built"):
            android_checks(value, 0, self.api, self.marker)

    def test_android_other_renamed_marker_rejected(self):
        value = self.changed("r8_marker_runtime_name", self.marker + "WrongBuild")
        with self.assertRaisesRegex(Failed, "r8_not_renamed_as_built"):
            android_checks(value, 0, self.api, self.marker)

    def test_android_duplicate_report_rejected(self):
        duplicate = "INSTRUMENTATION_RESULT: report_json=" + json.dumps(self.value) + "\n" + self.stdout
        with self.assertRaisesRegex(Failed, "one_instrumentation_report_required"):
            android_checks(duplicate, 0, self.api, self.marker)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({"scope": "OFFLINE_VALIDATOR_CORRUPTION_REGRESSIONS_NOT_NEW_DEVICE_EVIDENCE",
                      "golden_selection": "PUBLIC_HASH_LOCKED_FIXTURES" if PUBLIC.is_dir() else "LOCAL_CAPTURE_FALLBACK",
                      "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                      "skips": len(result.skipped), "actual_captured_goldens": PROVENANCE,
                      "all_available_regressions_passed": result.wasSuccessful()}, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
