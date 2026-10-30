"""Verify scoped closeout evidence and create a new local bundle; never run tests/devices."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "artifacts/continuation-20261030"

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def load(path): return json.loads(path.read_text(encoding="utf-8-sig"))
def identity(path): return {"path": path.relative_to(ROOT).as_posix(), "bytes": path.stat().st_size, "sha256": sha(path)}
def require(value, why):
    if not value: raise RuntimeError(why)
def checked(item):
    path = (ROOT / item["path"]).resolve()
    require(path.is_relative_to(ROOT) and path.is_file(), "invalid artifact: " + item["path"])
    require(sha(path) == item["sha256"], "identity drift: " + item["path"])
    return path

def plans():
    aliases = {v["original_path"]: v for v in load(BASE / "frozen-first-stability/source-aliases.json")["aliases"]}
    results = []
    for name in ("stability-plan.json", "stability-plan-android-closefix.json"):
        plan = load(BASE / name); verified = []
        for item in plan["files"]:
            current = ROOT / item["path"]
            actual = current if current.is_file() and sha(current) == item["sha256"] else None
            if actual is None and name == "stability-plan.json":
                alias = aliases.get(item["path"])
                require(alias and alias["executed_sha256"] == item["sha256"], "unexplained first-plan drift: " + item["path"])
                actual = ROOT / alias["exact_archived_source"]
                require(actual.is_relative_to(BASE / "frozen-first-stability") and sha(actual) == item["sha256"], "bad executed-source alias")
            require(actual is not None and sha(actual) == item["sha256"], "frozen drift: " + item["path"])
            verified.append({"frozen_path": item["path"], "verified_bytes": identity(actual)})
        if name == "stability-plan-android-closefix.json": require(len(verified) == 31, "closefix plan closure")
        results.append({"plan": identity(BASE / name), "verified_files": verified})
    return results

def safe_public(name):
    path = Path(name); parts = set(path.parts)
    return (not parts.intersection({"commands", ".git", ".codex", ".agents", "__pycache__"})
            and not path.name.startswith(("admission", ".env"))
            and path.suffix.lower() not in {".key", ".pem", ".jks", ".keystore", ".p12", ".pfx", ".apk", ".dex"}
            and not ("device-" in name and name.endswith((".stdout.txt", ".stderr.txt"))))

def zip_tree(prefix, target):
    files = {p.relative_to(prefix).as_posix(): p for p in prefix.rglob("*") if p.is_file()}
    if not target.exists():
        with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as archive:
            for name, path in sorted(files.items()): archive.write(path, name)
    with zipfile.ZipFile(target) as archive:
        require(set(archive.namelist()) == set(files) and archive.testzip() is None, "SDK ZIP closure")
        for name, path in files.items(): require(archive.read(name) == path.read_bytes(), "SDK ZIP bytes")

def copy_new(source, target):
    if target.exists(): require(sha(source) == sha(target), "refuse artifact overwrite: " + target.name)
    else: shutil.copyfile(source, target)
    return target

def main():
    frozen = plans()
    task = load(ROOT / "TASKS.json")["continuation_20261030"]
    require(task["state"] == "STAGE_CLOSED_SCOPED_DELIVERY_ANDROID_LONG_NOT_QUALIFIED", "closeout state")
    require((ROOT / "docs/CONSUMER_CLOSEOUT_20261030.md").is_file(), "closeout report")
    names = ["host-stability-final-15m/receipt.json", "device-arm64-final-15m/receipt.json", "device-api34-final-15m/receipt.json",
             "device-arm64-closefix-smoke/receipt.json", "device-api34-closefix-smoke/receipt.json",
             "device-arm64-closefix-15m/receipt.json", "device-api34-closefix-15m/receipt.json",
             "device-api34-guard-injection/receipt.json", "dex/host-postreview-receipt.json", "image-host-postreview/receipt.json",
             "asan/summary.json", "jni-close-fix/summary.json"]
    host, arm, old34, smoke_arm, smoke34, long_arm, long34, guard, dex, image, asan, jni = [load(BASE / n) for n in names]
    first_hash = sha(BASE / "stability-plan.json")
    require(host["state"] == "PASS" and host["stability_plan_sha256"] == first_hash, "host first window")
    require(len(host["results"]) == 3, "three host profiles")
    for row in host["results"].values():
        native = row["checked"]
        require(row["state"] == "PASS" and native.get("actual_elapsed_ms", native.get("active_loop_duration_ns", 0)/1000000) >= 900000, "host actual work")
    require(arm["state"] != "PASS" and not arm["all_passed"], "arm parent failure must remain")
    require(arm["stability_plan_sha256"] == first_hash and arm["results"]["dex"]["state"] == "PASS", "arm native identity")
    require(arm["results"]["dex"]["actual_elapsed_ms"] >= 900000 and arm["results"]["image"]["success"] is True
            and arm["results"]["image"]["active_loop_duration_ns"] >= 900000000000, "arm native bounded work")
    require(old34["state"] == "PASS" and old34["results"]["android"]["actual_duration_ms"] >= 900000, "historical API34 only")
    for row in (smoke_arm, smoke34): require(row["state"] == "PASS" and row["all_passed"] and row["results"]["android"]["status"] == "PASS", "closefix smoke")
    for row in (long_arm, long34):
        require(row["state"] != "PASS" and not row["all_passed"], "never upgrade incomplete Android long round")
        require(row["stability_plan_sha256"] == sha(BASE / "stability-plan-android-closefix.json"), "closefix round identity")
    require(guard["state"] == "PASS_EXPECTED_DIAGNOSTIC_FAIL_STOP" and guard["all_passed"], "guard isolation")
    require(dex["functional_cases"] == 12 and dex["state"].startswith("PASS") and image["all_passed"], "consumer short tests")
    require(asan["state"] == jni["state"] == "PASS" and all(p["CTest_count"] == 18 for p in jni["profiles"]), "Host ASan/JNI regressions")
    lock = BASE / "SOURCE_LOCK.json"; source = load(lock)
    for item in source["files"]:
        prefix = ROOT if item["root"] == "MeguvizMemory" else ROOT / "build/locked-inputs/DexMeguviz"
        require(item["root"] in ("MeguvizMemory", "DexMeguviz"), "unknown source root")
        path = (prefix / item["path"]).resolve()
        require(path.is_relative_to(prefix) and sha(path) == item["sha256"], "source lock drift: " + item["path"])
    builds = [load(BASE / "android-build/r8-arm64-closefix-build.json"), load(BASE / "android-build/r8-x86_64-closefix-build.json")]
    for built in builds:
        require(built["status"] == "PASS", "closefix R8 build")
        for item in built["sources"] + built["artifacts"]: checked(item)
        checked(built["input_aar"])
    notice = load(BASE / "android-build/pressure-closefix-notice.json")
    arm_aar = checked(notice["notice_augmented_aar"]); x86_aar = checked(builds[1]["input_aar"])
    for path in (arm_aar, x86_aar):
        with zipfile.ZipFile(path) as archive:
            require(archive.testzip() is None and any("NOTICE" in n.upper() for n in archive.namelist()), "AAR notice/integrity")
    dist = ROOT / "dist"; dist.mkdir(exist_ok=True)
    bundle = dist / "MeguvizMemory-0.2.0-Consumer-Closeout-20261030-CI1.zip"
    require(not bundle.exists(), "refuse to overwrite closeout bundle")
    x86_sdk = dist / "MeguvizMemory-0.2.0-Android-x86_64-Closeout-20261030.zip"
    zip_tree(ROOT / "install/continuation-x86_64", x86_sdk)
    packages = [ROOT / "dist/MeguvizMemory-0.2.0-Windows-x64.zip", ROOT / "dist/MeguvizMemory-0.2.0-Android-arm64-v8a.zip", x86_sdk,
                copy_new(arm_aar, dist / "MeguvizMemory-Android-Pressure-0.2.0-arm64-closefix1.aar"),
                copy_new(x86_aar, dist / "MeguvizMemory-Android-Pressure-0.2.0-x86_64-closefix1.aar")]
    consumers = {
        "Windows-x64": [ROOT / "build/continuation-20261030/dex-host/mgmem_dex_import_consumer.exe", ROOT / "build/continuation-image-host/mgmem_image_pressure_stability.exe", ROOT / "install/features-host/bin/mgmem.dll"],
        "Android-arm64-v8a": [ROOT / "build/continuation-dex-arm64/mgmem_dex_import_consumer", ROOT / "build/continuation-image-arm64/mgmem_image_pressure_stability", ROOT / "install/features-arm64/lib/libmgmem.so"]}
    apks = [(built["abi"], checked(next(v for v in built["artifacts"] if v["path"].endswith(".apk")))) for built in builds]
    result = {"schema_version": 2, "version": "0.2.0", "native_abi": "1.1", "pressure_revision": "closefix1",
        "state": "STAGE_CLOSED_SCOPED_DELIVERY_ANDROID_LONG_NOT_QUALIFIED", "task_state": task,
        "source_identity": {"kind": "SOURCE_HASH_BUILD_NOT_DELIVERY_COMMIT_BUILD", "source_lock": identity(lock), "source_set_sha256": source["source_set_sha256"]},
        "frozen_plans_verified": frozen, "receipts": [identity(BASE / n) for n in names],
        "host_900_second_profiles": {n:r["checked"] for n,r in host["results"].items()},
        "arm64_native_first_window": {"parent_state_preserved": arm["state"], "dex": arm["results"]["dex"], "image": arm["results"]["image"], "Android": "FAILED_OR_INCOMPLETE_NOT_UPGRADED"},
        "old_API34_module_only": {"receipt": identity(BASE / names[2]), "iterations": old34["results"]["android"]["iterations_completed"], "actual_duration_ms": old34["results"]["android"]["actual_duration_ms"], "transfers_to_closefix": False},
        "closefix_Android": {"dual_ABI_smoke": "PASS", "API33_long": "INCOMPLETE_REPEATED_STALL_ROOT_CAUSE_UNKNOWN", "API34_long": "INCOMPLETE_USER_REQUESTED_EARLY_STOP", "long_stability_qualified": False},
        "guard": "EXPECTED_DIAGNOSTIC_FAIL_STOP_NOT_NORMAL_CLOSED_ZERO", "Host_JNI": "RELEASE18_ASAN18_PASS", "consumer_ASan": "SHORT_PASS",
        "packages": [identity(p) for p in packages], "R8_APKs": [dict(identity(p), abi=abi) for abi,p in apks],
        "native_consumers": {label:[identity(p) for p in paths] for label,paths in consumers.items()},
        "attribution": {"ordinary_borrow": "BOTH_DIRECT_AND_IMPORT_ZERO_COPY_NO_EXTRA_BACKING", "Memory_import": "SOURCE_CAPACITY_SUBSET_ADDS_BUDGET_LIFETIME_NO_NEW_CAPACITY_SAVING", "image_ring_and_existing_scratch": "ORDINARY_OPTIMIZATION_NOT_MEMORY_INCREMENT"},
        "BIL": "NOT_MODIFIED_NOT_INTEGRATED", "performance": "NOT_MEASURED_NO_NEW_QUALIFICATION", "energy": "UNAVAILABLE", "formal_release": "NOT_QUALIFIED",
        "limitations": ["Android closefix long qualification incomplete; physical hang cause unknown", "No actual global OOM, real16KB, MTE/HWASan, family bridge, BIL host or formal Release qualification", "15-minute repeated create-close is not one long-lived domain,24h or unbounded leak proof", "Own process RSS/PSS is not domain attribution or measured net savings"],
        "bundle": {"path": bundle.relative_to(ROOT).as_posix(), "sha256_sidecar": bundle.name + ".sha256", "hash_location": "SIDECAR_AVOIDS_SELF_REFERENCE"}}
    manifest = BASE / "ARTIFACTS.json"
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    tracked = subprocess.check_output(["git", "-c", "safe.directory=" + ROOT.as_posix(), "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
    entries = {}
    for name in sorted(set(tracked)):
        if not name or not safe_public(name) or name.startswith(("research/", "docs/research/", "build/", "install/", "dist/")): continue
        if name.startswith("artifacts/") and not name.startswith("artifacts/continuation-20261030/"): continue
        path = ROOT / name
        if path.is_file(): entries["source/" + name] = path
    entries["source/artifacts/continuation-20261030/ARTIFACTS.json"] = manifest
    for path in packages: entries["packages/" + path.name] = path
    for abi,path in apks: entries["diagnostic/" + abi + "/" + path.name] = path
    for label,paths in consumers.items():
        for path in paths: entries["consumers/" + label + "/" + path.name] = path
    with zipfile.ZipFile(bundle, "x", zipfile.ZIP_DEFLATED) as archive:
        for name,path in sorted(entries.items()): archive.write(path,name)
    with zipfile.ZipFile(bundle) as archive:
        require(archive.testzip() is None and set(archive.namelist()) == set(entries), "bundle integrity/closure")
        for name,path in entries.items(): require(archive.read(name) == path.read_bytes(), "bundle bytes: " + name)
    (dist / (bundle.name + ".sha256")).write_text(sha(bundle) + "  " + bundle.name + "\n", encoding="ascii")
    print(json.dumps({"state": result["state"], "bundle": identity(bundle), "entries": len(entries), "manifest": identity(manifest)}))

if __name__ == "__main__": main()
