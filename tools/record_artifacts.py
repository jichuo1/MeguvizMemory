#!/usr/bin/env python3
"""Record built/installed binaries and verify that SDK ZIP entries match installation.

Run from the repository root after building/installing. No downloads or shell
interpolation. --ndk and --msvc-bin identify already installed toolchains.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def entry(root, path):
    return {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
            "sha256": digest(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ndk", type=Path, required=True)
    parser.add_argument("--msvc-bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("ARTIFACTS.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["git", "-c", "safe.directory=" + root.as_posix(),
                             "-C", str(root), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True, timeout=10)
    profiles = {"host": "Windows-x64", "host-static": "Windows-x64-static",
                "android-x86_64": "Android-x86_64", "android-arm64-v8a": "Android-arm64-v8a",
                "android-armeabi-v7a": "Android-armeabi-v7a"}
    installed, packages = {}, []
    for profile, label in profiles.items():
        install = root / "install" / profile
        if not install.is_dir():
            raise RuntimeError("missing install: " + profile)
        files = sorted(path for path in install.rglob("*") if path.is_file())
        installed[profile] = [entry(root, path) for path in files]
        package = root / "dist" / ("MeguvizMemory-0.1.0-" + label + ".zip")
        with zipfile.ZipFile(package) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            actual = set()
            for member in members:
                name = member.filename.replace("\\", "/")
                if name.startswith("/") or ".." in Path(name).parts:
                    raise RuntimeError("unsafe ZIP entry")
                target = install / name
                if not target.is_file() or hashlib.sha256(archive.read(member)).hexdigest() != digest(target):
                    raise RuntimeError("ZIP mismatch: " + name)
                actual.add(name)
            expected = {path.relative_to(install).as_posix() for path in files}
            if actual != expected:
                raise RuntimeError("ZIP omits installed files: " + profile)
        record = entry(root, package)
        record.update({"profile": profile, "installed_entries_verified": len(actual)})
        packages.append(record)
    binaries = ["build/host-ninja/mgmem_core_tests.exe", "build/host-ninja/mgmem_c_consumer.exe",
                "build/host-ninja/mgmem_cpp_consumer.exe", "build/host-ninja/mgmem_runtime_tests.exe",
                "build/host-asan/mgmem.dll", "build/host-asan/mgmem_core_tests.exe",
                "build/host-asan/mgmem_cpp_consumer.exe", "build/dex/mgmem_dex_consumer.exe",
                "build/image/mgmem_image.exe", "build/image-static/mgmem_image.exe"]
    binary_records = [entry(root, root / name) for name in binaries]
    toolchains = {"msvc_cl": args.msvc_bin / "cl.exe",
                  "ndk_clang": args.ndk / "toolchains/llvm/prebuilt/windows-x86_64/bin/clang++.exe",
                  "ndk_source_properties": args.ndk / "source.properties"}
    toolchain_records = {name: {"bytes": path.stat().st_size, "sha256": digest(path)}
                         for name, path in toolchains.items()}
    loaded = (root / "artifacts/verification-20261030/android-loaded-library-sha256.txt").read_text(encoding="utf-8-sig").split()[0]
    if loaded != digest(root / "install/android-x86_64/lib/libmgmem.so"):
        raise RuntimeError("loaded Android library differs from final installed artifact")
    if digest(root / "build/host-ninja/mgmem.dll") != digest(root / "install/host/bin/mgmem.dll"):
        raise RuntimeError("host installed library mismatch")
    receipt = json.loads((root / "artifacts/verification-20261030/consumers/receipt.json").read_text())
    if receipt["executables"]["image"]["sha256"] != digest(root / "build/image/mgmem_image.exe") or receipt["executables"]["dex"]["sha256"] != digest(root / "build/dex/mgmem_dex_consumer.exe"):
        raise RuntimeError("consumer receipts do not match final executable")
    artifact = {"schema_version": 1, "version": "0.1.0", "qualification": "CPU_FUNCTIONAL_ENGINEERING_PREVIEW",
                "compiled_source_commit": result.stdout.strip(),
                "source_identity_rule": "Core and consumers match this code commit; SOURCE_LOCK pins the final source/document/evidence snapshot independently.",
                "installed_profiles": installed, "packages": packages,
                "built_binaries": binary_records, "toolchains": toolchain_records,
                "runtime_checks": {"host_install_matches_build": True, "consumer_receipts_match_executables": True,
                                   "android_emulator_loaded_library_matches_install": True,
                                   "android_emulator_base_page_bytes": 4096,
                                   "real_16kb_loader": "UNAVAILABLE", "product_performance": "NOT_STARTED"},
                "receipts": ["artifacts/verification-20261030/consumers/receipt.json",
                             "artifacts/verification-20261030/host-release-ctest.txt",
                             "artifacts/verification-20261030/host-asan-ctest.txt",
                             "artifacts/verification-20261030/final-android-api34-mgmem_core_tests.txt",
                             "artifacts/verification-20261030/final-android-api34-mgmem_cpp_consumer.txt"]}
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"profiles": len(installed), "packages_verified": len(packages), "output": str(args.output)}))


if __name__ == "__main__":
    main()
