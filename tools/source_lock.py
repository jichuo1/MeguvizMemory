#!/usr/bin/env python3
"""Hash scoped source inputs without copying file contents or environment values.

Paths in the manifest are relative to named source roots. External parser inputs
are a conservative source-tree superset; compiled subsets remain defined by the
corresponding CMake build receipts. No files are fetched or modified except output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


LOCAL_DIRECTORIES = ("include", "src", "tests", "consumers", "bench", "cmake", "tools", "docs", ".github", "third_party_notices")
EXTERNAL_DIRECTORIES = ("include", "src", "third_party", "cmake", "cli")
EXCLUDED_DIRECTORIES = {".git", ".venv", ".codex", ".agents", ".aws", "node_modules", "__pycache__",
                        "build", "install", "dist", "artifacts", ".cache", ".gradle", ".idea"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"}


def sha256_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def allowed(path: Path) -> bool:
    lower = path.name.lower()
    return (lower != ".env" and not lower.startswith(".env.") and
            path.suffix.lower() not in SECRET_SUFFIXES)


def scoped_files(root: Path, directories: tuple[str, ...], output: Path):
    for name in directories:
        directory = root / name
        if not directory.is_dir() or directory.is_symlink():
            continue
        for current, children, files in os.walk(directory, followlinks=False):
            children[:] = sorted(child for child in children
                                 if child.lower() not in EXCLUDED_DIRECTORIES and
                                 not (Path(current) / child).is_symlink())
            for name in sorted(files):
                path = Path(current) / name
                if path.relative_to(root).as_posix().startswith("docs/research/"):
                    continue
                if path.is_file() and not path.is_symlink() and allowed(path) and path.resolve() != output:
                    yield path
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if (path.is_file() and not path.is_symlink() and allowed(path) and path.resolve() != output and
            (path.suffix.lower() in {".md", ".json", ".cmake", ".txt"} or
             path.name in {"CMakeLists.txt", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES", ".gitignore", ".gitattributes"})):
            yield path


def git_identity(root: Path) -> dict:
    try:
        # Trust only the caller-selected, resolved root for this command. The
        # isolated Windows account may differ from the checkout owner; never
        # change the global safe.directory list or permit a repo fsmonitor hook.
        command = ["git", "-c", "safe.directory=" + root.as_posix(),
                   "-c", "core.fsmonitor=false", "-C", str(root)]
        top = subprocess.run(command + ["rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10, check=False)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root:
            return {"head": None, "dirty": None, "state": "FROZEN_INPUT_WITHOUT_INDEPENDENT_GIT_ROOT"}
        head = subprocess.run(command + ["rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10, check=False)
        if head.returncode != 0:
            return {"head": None, "dirty": None, "state": "NO_ACCESSIBLE_GIT_HEAD"}
        status = subprocess.run(command + ["status", "--porcelain", "--untracked-files=normal"],
                                capture_output=True, text=True, timeout=10, check=False)
        return {"head": head.stdout.strip(),
                "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
                "state": "READ_ONLY_LOCAL_CHECK"}
    except (OSError, subprocess.TimeoutExpired):
        return {"head": None, "dirty": None, "state": "GIT_UNAVAILABLE"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dex-source", type=Path,
                        help="Real read-only DexMeguviz checkout; defaults to a sibling if present")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--research-archive", type=Path,
                        help="Optionally record only the private archive's byte count and SHA256")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        parser.error("--root must be an existing directory")
    output = args.output.resolve()
    dex = args.dex_source.resolve() if args.dex_source else root.parent / "DexMeguviz"
    dex_present = (dex / "include" / "dexmeguviz" / "dexmeguviz.h").is_file()
    if args.dex_source is not None and not dex_present:
        parser.error("Set --dex-source to the real checkout containing include/dexmeguviz/dexmeguviz.h")
    roots = [("MeguvizMemory", root, LOCAL_DIRECTORIES)]
    if dex_present:
        roots.append(("DexMeguviz", dex, EXTERNAL_DIRECTORIES))
    records = []
    identities = {}
    for alias, source, directories in roots:
        identities[alias] = git_identity(source)
        for path in scoped_files(source, directories, output):
            records.append({"root": alias, "path": path.relative_to(source).as_posix(),
                            "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    records.sort(key=lambda item: (item["root"], item["path"]))
    canonical = json.dumps(records, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    result = {
        "schema_version": 1,
        "scope": "SCOPED_SOURCE_TREE_LOCK",
        "roots": identities,
        "external_parser_source": "LOCKED" if dex_present else "UNAVAILABLE_NOT_CONSUMED",
        "source_set_sha256": hashlib.sha256(canonical).hexdigest(),
        "file_count": len(records),
        "files": records,
        "rules": [
            "Paths are relative to named roots; no machine paths, remotes or environment values are stored.",
            "File contents are represented only by byte length and SHA256.",
            "Output excludes itself to avoid a recursive hash dependency.",
            "External include/src/third_party/cmake and root build/manifests are a conservative superset of build inputs.",
            "Build/install outputs, Git internals, caches, symlinks and common secret/env files are excluded.",
            "Private research attachments are excluded from the public source-file list; optional archive identity contains only size and SHA256.",
            "Private preparatory indexes under docs/research are excluded; reviewed public summaries are recorded from docs instead.",
            "A source-tree lock is not a clean-commit, build, install, runtime or release receipt."
        ],
    }
    if args.research_archive is not None:
        archive = args.research_archive.resolve()
        if not archive.is_file():
            parser.error("--research-archive must be an existing regular file")
        result["private_research_archive"] = {"bytes": archive.stat().st_size, "sha256": sha256_file(archive)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"file_count": len(records), "source_set_sha256": result["source_set_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
