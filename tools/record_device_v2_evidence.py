#!/usr/bin/env python3
"""Record this device-v2 scope; no ADB, raw-input publication or old-manifest rewrite."""
import hashlib
import json
import argparse
import subprocess
from pathlib import Path
from paired_analysis_v2 import digest, strict_json
from paired_suite_v2 import analyze_family, freeze_errors

ROOT = Path(__file__).resolve().parents[1]
BASE = Path("artifacts/device-v2-20261030")


def record(retry1=False):
    if not __debug__:
        raise RuntimeError("evidence_checks_require_assertions_enabled")
    files = {}
    suffix = "-retry1" if retry1 else ""
    def blob(relative, expected=None, include=True):
        path = (ROOT / relative).resolve()
        assert path.is_relative_to(ROOT) and path.is_file(), "missing or unsafe scoped evidence"
        raw = path.read_bytes(); sha = hashlib.sha256(raw).hexdigest()
        assert expected is None or expected == sha, "scoped identity mismatch: " + str(relative)
        if include:
            assert "admission" not in str(relative) and path.suffix.lower() not in (".apk", ".dex")
            files[path.relative_to(ROOT).as_posix()] = {"path": path.relative_to(ROOT).as_posix(), "bytes": len(raw), "sha256": sha}
        return raw
    def read(relative):
        return strict_json(blob(relative).decode("utf-8-sig"))
    def private_receipt(relative):
        raw = blob(relative, include=False)
        value = strict_json(raw.decode("utf-8-sig"))
        if "completed_at_utc" in value:
            summary = {key:value.get(key) for key in ("schema_version","scope","state","all_passed",
                "started_at_utc","completed_at_utc","native_tasks_recorded","device_serial_sha256",
                "device_directory","reason","enabled_families","skipped_by_pilot_stop")}
            summary.update({"scope":"PUBLIC_CAPTURE_SUMMARY_PRIVATE_AUTHORIZATION_BODY_EXCLUDED",
                "private_receipt_file_sha256":hashlib.sha256(raw).hexdigest(),
                "commands_recorded":len(value.get("commands",[])),"performance_qualified":False})
            target = Path(relative).with_name("capture-summary.json")
            (ROOT/target).write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="\n")
            blob(target)
        return value
    def chain(relative):
        rows, errors, previous = [], [], "0" * 64
        for number, line in enumerate(blob(relative).decode("utf-8").splitlines(), 1):
            try:
                event = strict_json(line); payload = {k: v for k, v in event.items() if k != "event_sha256"}
                assert type(event["sequence"]) is int and event["sequence"] == number and event["previous_sha256"] == previous
                assert digest(event["record"]) == event["record_sha256"] and digest(payload) == event["event_sha256"]
                rows.append(event["record"]); previous = event["event_sha256"]
            except (AssertionError, KeyError, ValueError, TypeError):
                errors.append({"line": number, "reason": "INVALID_OR_PARTIAL_HASH_CHAIN"}); break
        return rows, errors
    functional = read(BASE / "new-device-functional/functional-receipt.json")
    assert functional["state"] == "PASS" and functional["all_passed"] is True
    assert functional["completed_cases"] == {"dex": 32, "image_workspace": 156, "image_perf": 8}
    assert functional["core_safety"]["status"] == "PASS" and functional["core_safety"]["tests"] == 3
    native_rows = [strict_json(line) for line in blob(BASE / "new-device-functional/functional.jsonl").decode().splitlines()]
    assert len(native_rows) == 199 and all(r["success"] is True and all(v is True for v in r["checks"].values()) for r in native_rows)
    protocol, plan = read(BASE / ("frozen-suite" + suffix) / "protocol.json"), read(BASE / ("frozen-suite" + suffix) / "plan.json")
    assert not freeze_errors(protocol, plan)
    for item in protocol["freeze"]["source_closure"]: blob(item["path"], item["sha256"])
    retry = None
    if retry1:
        prereg = read(BASE / "engineering-retry1-preregistration.json")
        assert prereg["one_engineering_retry_predeclared"] is True and prereg["pool_old_attempt"] is False and prereg["additional_retry_after_retry1"] is False
        assert protocol["engineering_retry_preregistration_file_sha256"] == hashlib.sha256(blob(BASE / "engineering-retry1-preregistration.json")).hexdigest()
        old = read(BASE / "frozen-suite/protocol.json"); read(BASE / "frozen-suite/plan.json"); read(BASE / "initial-attempt-ARTIFACTS.json")
        blob(BASE / "frozen-suite/protocol.json", prereg["original_protocol_sha256"])
        blob(BASE / "capture-pilot/tasks.jsonl", prereg["original_pilot_journal_sha256"])
        failed = private_receipt(BASE / "capture-pilot/receipt.json"); old_rows, errors = chain(BASE / "capture-pilot/tasks.jsonl")
        assert not errors and failed["state"] == "FAILED_OR_BLOCKED" and failed["reason"] == "Blocked:session_wall_cap_exceeded" and len(old_rows) == failed["native_tasks_recorded"] == 28 and all(r["success"] is True for r in old_rows)
        _, errors = chain(BASE / "capture-pilot/command-events.jsonl"); assert not errors
        for item in old["freeze"]["source_closure"]:
            path = ROOT / item["path"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                snapshot = BASE / "frozen-helper-initial.txt"
                if item["path"] == "tools/freeze_device_suite_v2.py" and (ROOT / snapshot).is_file():
                    raw = blob(snapshot, item["sha256"])
                else:
                    raw = subprocess.run(["git", "-c", "safe.directory=" + ROOT.as_posix(), "show", "5a7611a:" + item["path"]], capture_output=True, check=True).stdout
                assert hashlib.sha256(raw).hexdigest() == item["sha256"], "old frozen source differs from fixed Git blob"
        retry = {"number": 1, "old_tasks_preserved_separately": 28, "old_native_tasks_successful": True, "old_source_fallback_commit": "5a7611a", "old_attempt_pooling": False, "further_retry_allowed": False}
    artifacts = []
    for item in protocol["freeze"]["artifact_closure"]:
        raw = blob(item["path"], item["sha256"], include=False)
        assert any(q["sha256"] == item["sha256"] == q["device_sha256"] for q in functional["deployment_manifest"])
        artifacts.append({"role": item["role"], "name": item["name"], "bytes": len(raw), "sha256": item["sha256"]})
    stage, deployed = private_receipt(BASE / ("capture-stage" + suffix) / "receipt.json"), read(BASE / ("capture-stage" + suffix) / "deployment.json")
    assert stage["all_passed"] is True and stage["state"] == "PASS_CAPTURE_ONLY" and deployed["state"] == "STAGED"
    assert deployed["protocol_canonical_sha256"] == digest(protocol) and deployed["boot_id_sha256"] == functional["device"]["boot_id_sha256"]
    assert {a["sha256"] for a in deployed["artifacts"]} == {a["sha256"] for a in artifacts}
    image, cross = read(BASE / "image-host-functional.json"), read(BASE / "image-arm64-cross-build.json")
    assert image["status"] == "PASS" and image["cases"] == 162 and cross["status"] == "PASS"
    blob(image["executable"]["path"], image["executable"]["sha256"])
    for item in cross["sources"] + [cross["binary"], cross["memory_installed_runtime"]]: blob(item["path"], item["sha256"])
    suite, collector = read(BASE / "suite-offline-tests.json"), read(BASE / "collector-offline-tests.json")
    assert suite["status"] == "PASS" and suite["test_count"] == 40 and suite["device_operations"] == 0
    for item in suite["sources"]: blob(item["path"], item["sha256"])
    assert collector["status"] == "PASS" and collector["tests"] == 20 and collector["actual_device_operations"] == 0
    blob("tools/device_capture_v2.py", collector["collector_sha256"]); blob("tools/test_device_capture_v2.py", collector["test_sha256"])
    captures, reports = {}, {}
    for phase in ("pilot", "formal"):
        folder = BASE / ("capture-" + phase + suffix)
        if not (ROOT / folder / "receipt.json").exists(): captures[phase] = {"state": "NOT_STARTED"}; continue
        receipt = private_receipt(folder / "receipt.json"); rows, errors = chain(folder / "tasks.jsonl")
        terminal = "completed_at_utc" in receipt
        if terminal: assert not errors and len(rows) == receipt["native_tasks_recorded"]
        commands, command_errors = chain(folder / "command-events.jsonl")
        if terminal: assert not command_errors
        source = protocol["freeze"]["cadence"].get("battery_source", "sysfs")
        assert all(row.get("environment", {}).get("battery_source") == source and row.get("environment_after", {}).get("battery_source") == source for row in rows if row.get("success") is True)
        captures[phase] = {"state": receipt["state"] if terminal else "IN_PROGRESS", "tasks_recorded": len(rows), "hash_chain_errors": errors, "command_events": len(commands), "command_chain_errors": command_errors, "all_passed": receipt["all_passed"] if terminal else False,
            "enabled_families":receipt.get("enabled_families"),"skipped_by_pilot_stop":receipt.get("skipped_by_pilot_stop",[])}
        for family in ("dex", "image"):
            name = BASE / (phase + "-" + family + ".json")
            if (ROOT / name).is_file():
                report = read(name); assert report["protocol_canonical_sha256"] == digest(protocol) and report["plan_canonical_sha256"] == digest(plan) and report["performance_qualified"] is False
                recomputed = analyze_family(protocol, plan, rows, phase, family, errors)
                assert all(report.get(key) == value for key, value in recomputed.items()), "report differs from immutable raw-journal analysis"
                reports[phase + "-" + family] = {"decision": report["decision"], "attempted_tasks": report["attempted_tasks"], "performance_qualified": False}
    historical = read(Path("artifacts/research-20261030/evidence_manifest.json"))
    for item in historical["historical_files"]: blob(item["path"], item["sha256"])
    result = {"schema_version": 2, "scope": "DEVICE_V2_CURRENT_FREEZE_ONLY", "retry1_selected": retry1, "engineering_retry": retry, "static_integrity_verified": True, "functional_cases": 196, "core_safety_tests": 3, "artifacts": artifacts, "captures": captures, "reports": reports, "files": sorted(files.values(), key=lambda x: x["path"]), "performance_qualification": "INCONCLUSIVE_OUTER_INDEPENDENCE_NOT_ESTABLISHED", "BIL_integration": "NOT_SELECTED", "history": "5ac9b4f research manifest remains applicable to its original snapshot; not revalidated against changed current CMake", "privacy": "No raw inputs, admission body, absolute paths, duplicate command sidecars or file bodies included; active captures are explicitly IN_PROGRESS."}
    (ROOT / BASE / "ARTIFACTS.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"integrity": True, "captures": captures, "performance_qualified": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry1", action="store_true")
    record(parser.parse_args().retry1)
