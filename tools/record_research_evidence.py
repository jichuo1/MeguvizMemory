#!/usr/bin/env python3
"""Verify the finite research batch and write its provenance-only manifest.

No builds, product execution, adb, network, kit-code imports, archive extraction
or private input reads. Run after final source-lock/receipt generation. Binary
and SDK archive bodies stay local; this output contains their identities only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


BASE = PurePosixPath("artifacts/research-20261030")
RECEIPTS = (
    "evidence_manifest.json", "independent_recompute.json", "historical-source-lock.json",
    "dex/borrow_contract_gate.json", "dex/functional_receipt.json",
    "image/image_oracle_receipt.json", "image/workspace_model_check.json",
    "offline_validator_receipt.json", "arm64_cross_build.json", "scoped_status_diff.md",
)
SDK_COMMIT = "58ed07f54199a1217ddef47735d4d297357654f4"
SDK_PROFILES = {"host", "host-static", "android-arm64-v8a", "android-armeabi-v7a", "android-x86_64"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha_file(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


class Recorder:
    def __init__(self, root):
        self.root = root.resolve()
        self.verified = {}

    def path(self, relative):
        require(isinstance(relative, str) and relative and "\\" not in relative and ":" not in relative,
                "expected portable repository-relative path")
        part = PurePosixPath(relative)
        require(not part.is_absolute() and ".." not in part.parts, "path escapes repository")
        require(part.suffix.lower() not in (".apk", ".dex") and not
                (len(part.parts) >= 2 and part.parts[:2] == ("artifacts", "local")),
                "private/raw input must not enter this manifest")
        path = (self.root / Path(*part.parts)).resolve()
        require(path.is_relative_to(self.root), "resolved path escapes repository")
        return path

    def check(self, relative, expected=None, size=None, role="reference"):
        path = self.path(relative)
        require(path.is_file(), "missing file: " + relative)
        actual = sha_file(path)
        require(expected is None or actual == expected, "SHA256 mismatch: " + relative)
        actual_size = path.stat().st_size
        require(size is None or actual_size == size, "size mismatch: " + relative)
        row = self.verified.setdefault(relative, {"path": relative, "bytes": actual_size,
                                                   "sha256": actual, "roles": []})
        require(row["sha256"] == actual and row["bytes"] == actual_size, "file changed during recording: " + relative)
        if role not in row["roles"]:
            row["roles"].append(role)
        return row

    def read(self, relative, role="receipt"):
        self.check(relative, role=role)
        return json.loads(self.path(relative).read_text(encoding="utf-8-sig"))

    def source_lock(self, value, role):
        mapping = {"MeguvizMemory": "", "DexMeguviz": "build/locked-inputs/DexMeguviz/"}
        require(len(value["files"]) == value["file_count"], "source lock count mismatch")
        for row in value["files"]:
            require(row["root"] in mapping, "unrecognized source-lock root")
            self.check(mapping[row["root"]] + row["path"], row["sha256"], row.get("bytes"), role)

    def finish_checks(self):
        # Detect concurrent source/receipt changes between checking and writing.
        for relative, row in self.verified.items():
            path = self.path(relative)
            require(path.is_file() and path.stat().st_size == row["bytes"] and sha_file(path) == row["sha256"],
                    "file changed before manifest write: " + relative)


def build_manifest(root):
    recorder = Recorder(root)
    names = {name: (BASE / name).as_posix() for name in RECEIPTS}
    data = {name: recorder.read(path) for name, path in names.items() if name.endswith(".json")}
    recorder.check(names["scoped_status_diff.md"], role="scoped_state_explanation")
    old = recorder.read("ARTIFACTS.json", "original_sdk_manifest")
    require(old["compiled_source_commit"] == SDK_COMMIT, "original SDK compilation identity was changed")
    current_lock = recorder.read("SOURCE_LOCK.json", "current_scoped_source_lock")
    recorder.source_lock(current_lock, "current_source_lock_entry")

    audit = data["evidence_manifest.json"]
    recompute = data["independent_recompute.json"]
    require(audit["old_results_rewritten"] is False and audit["device_used"] is False, "historical audit scope changed")
    for row in audit["historical_files"]:
        recorder.check(row["path"], row["sha256"], row["bytes"], "protected_historical_evidence")
    require(recompute["status"] == "PASS" and recompute["blocks"] == 32 and recompute["tasks"] == 128 and
            recompute["historical_decision"] == "INCONCLUSIVE", "historical result must remain inconclusive")
    require(recompute["device_execution"] == "NOT_PERFORMED", "offline recompute is not a device run")
    for row in audit["kit"]["repository_evidence_matches"]:
        recorder.check(row["path"], row["repository_sha256"], role="research_kit_comparison")
        require(row["semantic_equal"] is True, "returned kit has different historical values")
        if not row["path"].endswith("/report.json"):
            require(row["byte_equal"] is True, "historical raw data/plan bytes changed")

    gate = data["dex/borrow_contract_gate.json"]
    historical_lock_path = names["historical-source-lock.json"]
    require(gate["state"] == "PASS_FOR_SCOPED_FROZEN_SDK" and gate["source_lock_sha256"] ==
            recorder.check(historical_lock_path)["sha256"], "borrow gate must refer to the historical source lock")
    historical_lock = data["historical-source-lock.json"]
    require(gate["source_lock_source_set_sha256"] == historical_lock["source_set_sha256"], "SDK source-set identity differs")
    historical_sdk = {row["path"]: row for row in historical_lock["files"] if row["root"] == "DexMeguviz"}
    require(gate["frozen_file_count"] == len(gate["verified_files"]) == len(historical_sdk) == 65, "incomplete frozen SDK closure")
    require({row["path"] for row in gate["verified_files"]} == set(historical_sdk), "SDK gate paths differ")
    require(sha_bytes(canonical(gate["verified_files"])) == gate["verified_closure_receipt_sha256"], "SDK gate closure digest differs")
    for row in gate["verified_files"]:
        require(row["expected_sha256"] == row["actual_sha256"] == historical_sdk[row["path"]]["sha256"], "SDK gate identity differs")
        recorder.check("build/locked-inputs/DexMeguviz/" + row["path"], row["actual_sha256"], row["bytes"], "frozen_sdk_source")

    dex = data["dex/functional_receipt.json"]
    require(dex["state"] == "PASS_HOST_FUNCTIONAL_ONLY" and dex["device_executed"] is False and
            dex["checks_passed"] == dex["checks_total"] == len(dex["runs"]) == 32 and
            dex["performance_qualification"] == "NOT_STARTED_NEW_PROTOCOL_NOT_FROZEN" and
            dex["old_experiment_modified"] is False, "DEX functional scope/count mismatch")
    require(dex["contract_gate_sha256"] == recorder.check(names["dex/borrow_contract_gate.json"])["sha256"], "DEX gate changed")
    for row in dex["source_identity"]:
        recorder.check(row["path"], row["sha256"], role="dex_build_source")
    binaries = []
    for row in dex["build"]["executables"]:
        require(row["name"] in ("mgmem_dex_paired_v2.exe", "mgmem_dex_paired_v2_diagnostic.exe"), "unexpected DEX host artifact")
        binaries.append(recorder.check("build/dex-borrow-v2/" + row["name"], row["sha256"], role="host_consumer_binary"))
    require(len(binaries) == 2 and len({row["path"] for row in binaries}) == 2, "missing distinct DEX builds")
    runtime = recorder.check("install/host/bin/mgmem.dll", dex["build"]["memory_installed_library_sha256"], role="installed_runtime")
    original_runtime = next(row for row in old["installed_profiles"]["host"] if row["path"] == runtime["path"])
    require(original_runtime["sha256"] == runtime["sha256"], "host runtime differs from original SDK")
    for row in dex["runs"]:
        native = row["native"]
        require(all(value is True for value in row["checks"].values()) and native["success"] is True and
                native["source_final_released"] is True and native["lease_final_correct"] is True and
                type(native["closed_backing_bytes"]) is int and native["closed_backing_bytes"] == 0 and
                native["final_resource_state"] == "CLOSED_ZERO", "DEX case not qualified")

    image = data["image/image_oracle_receipt.json"]
    require(image["command_count"] == image["passed_count"] == len(image["commands"]) == 156 and image["all_passed"] is True and
            image["performance_qualification"] == "NOT_STARTED" and image["device_execution"] == "NOT_STARTED", "image scope/count mismatch")
    binaries.append(recorder.check("build/image-v2/mgmem_image_paired.exe", image["executable_sha256"], role="host_consumer_binary"))
    recorder.check("tools/verify_image_workspace_v2.py", image["verifier_sha256"], role="image_verifier_source")
    require(all(row["verdict"] == "PASS" and all(value is True for value in row["checks"].values())
                for row in image["commands"]), "image case not qualified")
    model = data["image/workspace_model_check.json"]
    require(model["status"] == "PASS" and model["all_normal_case_model_fields_match"] is True and
            model["layout_benefit_owner"] == "ORDINARY_CONSUMER_NOT_MEMORY", "image model failed or attribution changed")

    offline = data["offline_validator_receipt.json"]
    require(offline["status"] == "PASS" and offline["cases"] == len(offline["results"]) == 36 and
            all(row["status"] == "PASS" for row in offline["results"]) and offline["device_operations"] == 0 and
            offline["performance_qualified"] is False, "offline validator scope/count mismatch")
    protocol = recorder.read("bench/paired_protocol_v2_draft.json", "draft_protocol")
    recorder.check("bench/paired_protocol_v2_draft.json", offline["protocol_file_sha256"], role="offline_test_protocol")
    require(sha_bytes(canonical(protocol)) == offline["protocol_canonical_sha256"] and
            protocol["state"] == "PROPOSED_NOT_FROZEN_NOT_EXECUTED", "draft protocol identity/state changed")
    for name, key in (("tools/paired_analysis_v2.py", "analysis_source_sha256"),
                      ("tools/run_paired_device_v2.py", "entrypoint_source_sha256"),
                      ("tools/test_paired_analysis_v2.py", "test_source_sha256")):
        recorder.check(name, offline[key], role="offline_validator_source")
    smoke = offline["real_native_adapter_smoke"]
    require(smoke["source_receipt"] == names["dex/functional_receipt.json"] and
            smoke["not_new_device_or_performance_evidence"] is True, "adapter scope changed")
    recorder.check(smoke["source_receipt"], smoke["source_receipt_sha256"], role="real_host_payload_adapter_source")
    normal = {row["variant"]: row["native"] for row in dex["runs"] if row["case"] == "normal" and not row["native"]["diagnostic"]}
    require(len(smoke["native_payloads"]) == 4, "missing real payload adapter cases")
    for row in smoke["native_payloads"]:
        require(row["variant"] in normal and sha_bytes(canonical(normal[row["variant"]])) == row["native_sha256"], "adapter payload identity differs")

    arm64 = data["arm64_cross_build.json"]
    require(arm64["status"] == "PASS" and arm64["device_executed"] is False and
            arm64["performance_qualified"] is False and arm64["actual_16KB_loader"] == "NOT_EXECUTED" and
            arm64["MTE_HWASAN"] == "NOT_EXECUTED", "cross-build evidence was upgraded")
    for row in arm64["files"]:
        require(row["path"].startswith(("build/", "install/")), "unexpected cross-build file")
        checked = recorder.check(row["path"], row["sha256"], row["bytes"], "arm64_cross_build_binary")
        binaries.append(checked)
        if row["path"].startswith("install/"):
            original = next(item for item in old["installed_profiles"]["android-arm64-v8a"] if item["path"] == row["path"])
            require(original["sha256"] == row["sha256"], "arm64 runtime differs from original SDK")
    require(len(arm64["files"]) == 4, "incomplete cross-build manifest")
    for row in arm64["consumer_sources"]:
        recorder.check(row["path"], row["sha256"], role="arm64_consumer_build_source")

    archive_identities = []
    packages = old["packages"]
    require(len(packages) == 5 and {row["profile"] for row in packages} == SDK_PROFILES, "original SDK archive set differs")
    for row in packages + [old["external_parser_replay_snapshot"]]:
        require(row["path"].startswith("dist/") and row["path"].endswith(".zip"), "unexpected SDK archive path")
        present = recorder.path(row["path"]).is_file()
        if present:
            checked = recorder.check(row["path"], row["sha256"], row["bytes"], "local_archive_identity_only")
            archive_identities.append({**checked, "available_locally": True, "body_copied_or_published": False})
        else:
            archive_identities.append({"path": row["path"], "bytes": row["bytes"], "sha256": row["sha256"],
                                       "available_locally": False, "identity_source": "original ARTIFACTS.json",
                                       "body_copied_or_published": False})

    recorder.finish_checks()
    files = [recorder.verified[path] for path in names.values()]
    sources = [row for row in recorder.verified.values() if any("source" in role or role == "draft_protocol" for role in row["roles"])]
    return {
        "schema_version": 1, "scope": "RESEARCH_NEXT_BATCH_HOST_FUNCTIONAL_AND_ARM64_CROSS_BUILD_PROVENANCE",
        "integrity_checks_passed": True, "integrity_scope": "IDENTITY_AND_RECEIPT_CONSISTENCY_NOT_PERFORMANCE_QUALIFICATION",
        "evidence_counts": {"historical_blocks": 32, "historical_tasks": 128, "dex_host_cases": 32,
                            "image_host_cases": 156, "offline_validator_cases": 36},
        "new_consumer_provenance": {"identity": "DIRTY_SCOPED_SOURCE_HASH_BUILD_BEFORE_DELIVERY_COMMIT",
                                    "compiled_from_report_commit": False,
                                    "rule": "Receipt source/executable hashes and current scoped source lock are checked separately; no report or delivery commit is asserted as the compiler input.",
                                    "host_image_source_limit": "Host image receipt embeds executable/verifier hashes; current native/CMake source hashes are additionally locked by the arm64 cross-build receipt, not embedded in the host executable.",
                                    "host_image_runtime_limit": "Image receipt does not embed a loaded-library hash; the current installed DLL matches the original SDK and DEX receipt. This is not retroactive image-loader proof."},
        "original_sdk": {"compiled_source_commit": SDK_COMMIT, "identity_preserved": True,
                         "manifest": recorder.verified["ARTIFACTS.json"], "local_archive_identities": archive_identities},
        "current_source_lock": recorder.verified["SOURCE_LOCK.json"],
        "historical_source_lock": recorder.verified[names["historical-source-lock.json"]],
        "files": sorted(files, key=lambda row: row["path"]),
        "binary_artifacts": sorted(binaries + [runtime], key=lambda row: row["path"]),
        "source_and_protocol_files": sorted(sources, key=lambda row: row["path"]),
        "historical_files_verified_unchanged": audit["historical_files"],
        "qualification": {"new_host_functional": "PASS_SCOPED_CASES_ONLY", "new_arm64_cross_build": "PASS_BUILD_ONLY",
                          "new_device_execution": "NOT_PERFORMED", "historical_performance": "INCONCLUSIVE",
                          "new_performance": "NOT_STARTED_PROTOCOL_NOT_FROZEN", "BIL_integration": "NOT_SELECTED",
                          "actual_16KB_loader": "NOT_EXECUTED", "MTE_HWASAN": "NOT_EXECUTED",
                          "RSS_PSS_energy_PMU_PFN": "UNQUALIFIED"},
        "privacy_and_recursion": "Explicit public receipt allowlist only. No APK/DEX or private artifacts/local, prompts, machine paths, binary/archive bodies, this manifest's own hash, or combined-bundle identity is included.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = build_manifest(args.root)
    target = Recorder(args.root).path((BASE / "ARTIFACTS.json").as_posix())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                      encoding="utf-8", newline="\n")
    print(json.dumps({"integrity_checks_passed": True, "receipts": len(result["files"]),
                      "binary_artifacts": len(result["binary_artifacts"]),
                      "new_device_execution": "NOT_PERFORMED", "new_performance": "NOT_STARTED"}))


if __name__ == "__main__":
    main()
