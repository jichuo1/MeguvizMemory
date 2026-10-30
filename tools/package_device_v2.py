#!/usr/bin/env python3
"""Package reviewed public sources/evidence and local preview artifacts.

Requires terminal v2 receipts. Raw DEX/APK, admission authorization bodies,
private research and duplicate command sidecars are excluded. Existing archives
are never overwritten; the exact ZIP identity is saved beside the output.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parents[1]
BASE="artifacts/device-v2-20261030/"


def allowed(relative):
    return not (relative.startswith(("research/","docs/research/","artifacts/local/",".git/")) or
        (relative.startswith(BASE) and ("/commands/" in relative or
         ("/capture-" in "/"+relative and Path(relative).name in ("receipt.json","receipt.json.pending")) or
         Path(relative).name.startswith("admission-"))) or Path(relative).suffix.lower() in (".apk",".dex"))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    manifest=json.loads((ROOT/BASE/"ARTIFACTS.json").read_text(encoding="utf-8"))
    if (manifest.get("static_integrity_verified") is not True or
        any(c.get("state")=="IN_PROGRESS" for c in manifest["captures"].values())):
        parser.error("terminal verified evidence is required")
    output=args.output.resolve()
    if not output.is_relative_to(ROOT/"dist") or output.exists():
        parser.error("output must be a new archive within dist")
    result=subprocess.run(["git","-c","safe.directory="+ROOT.as_posix(),"ls-files","-z"],
                          capture_output=True,check=True)
    files={p for p in result.stdout.decode().split("\0") if p and allowed(p)}
    lock=json.loads((ROOT/"SOURCE_LOCK.json").read_text(encoding="utf-8"))
    files.update(r["path"] for r in lock["files"] if r["root"]=="MeguvizMemory" and allowed(r["path"]))
    files.update(r["path"] for r in manifest["files"] if allowed(r["path"]))
    files.update(p.relative_to(ROOT).as_posix() for p in (ROOT/BASE).rglob("*")
                 if p.is_file() and allowed(p.relative_to(ROOT).as_posix()))
    files.add(BASE+"ARTIFACTS.json")
    for relative in ("install/host/bin/mgmem.dll","install/android-arm64-v8a/lib/libmgmem.so",
        "build/dex-borrow-v2/mgmem_dex_paired_v2.exe","build/dex-borrow-v2/mgmem_dex_paired_v2_diagnostic.exe",
        "build/dex-borrow-v2-arm64/mgmem_dex_paired_v2","build/dex-borrow-v2-arm64/mgmem_dex_paired_v2_diagnostic",
        "build/image-perf-v2/mgmem_image_perf.exe","build/image-perf-v2-arm64/mgmem_image_perf",
        "build/image-v2/mgmem_image_paired.exe","build/image-v2-arm64/mgmem_image_paired"):
        files.add(relative)
    old=json.loads((ROOT/"ARTIFACTS.json").read_text(encoding="utf-8"))
    for item in old["packages"]+[old["external_parser_replay_snapshot"]]:
        raw=(ROOT/item["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item["sha256"]:raise ValueError("SDK archive identity drift")
        files.add(item["path"])
    for relative in files:
        path=ROOT/relative
        if not allowed(relative) or not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError("unsafe or missing package input")
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,"x",zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for relative in sorted(files):archive.write(ROOT/relative,relative)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:raise ValueError("ZIP integrity failure")
        for relative in files:
            if archive.read(relative)!=(ROOT/relative).read_bytes():raise ValueError("ZIP input changed while packaging")
    sha=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+".sha256").write_text(sha+"  "+output.name+"\n",encoding="ascii")
    print(json.dumps({"archive":output.name,"bytes":output.stat().st_size,"entries":len(files),"sha256":sha,
                      "formal_release_qualified":False,"raw_APK_DEX_and_private_authorization":"EXCLUDED"}))


if __name__=="__main__":main()
