#!/usr/bin/env python3
"""Independent byte oracle and resource-model checks; never a timing qualification."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


DIMENSIONS = [(1, 1), (1, 2), (1, 7), (2, 1), (2, 2), (2, 7),
              (7, 1), (7, 2), (17, 13), (320, 180), (1920, 1080)]
VARIANTS = [(provider, layout) for provider in ("direct", "system", "arena")
            for layout in ("full", "ring")]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rgb_fixture(width: int, height: int) -> bytes:
    return bytes(channel for y in range(height) for x in range(width)
                 for channel in ((17*x + 29*y + x*y + 11) % 256,
                                 (37*x + 13*y + 3*x*y + 53) % 256,
                                 (7*x + 41*y + 5*x*y + 101) % 256))


def oracle(width: int, height: int, rgb: bytes) -> tuple[bytes, int]:
    # Full Python scalar image with independent list indexing and two distinct
    # integer divisions; it does not call the native ring or Memory library.
    luma = [(77*rgb[i] + 150*rgb[i+1] + 29*rgb[i+2]) // 256
            for i in range(0, len(rgb), 3)]
    result = bytearray()
    for y in range(height):
        ys = (max(0, y-1), y, min(height-1, y+1))
        for x in range(width):
            xs = (max(0, x-1), x, min(width-1, x+1))
            result.append(sum(luma[yy*width + xx] for yy in ys for xx in xs) // 9)
    return f"P5\n{width} {height}\n255\n".encode() + result, sum(result)


def model(width: int, height: int, layout: str, provider: str) -> dict:
    pixels = width*height
    sizes = [3*pixels, min(height, 3)*width if layout == "ring" else pixels, pixels]
    if provider == "arena":
        # Sequential first-fit, 64-byte alignment, retained chunk capacity. This
        # is a capacity model, not physical malloc usable size or RSS.
        blocks: list[list[int]] = []
        for size in sizes:
            for block in blocks:
                offset = (block[1] + 63) // 64 * 64
                if offset + size <= block[0]:
                    block[1] = offset + size
                    break
            else:
                blocks.append([max(65536, size), size])
        backing = sum(block[0] for block in blocks)
        count = len(blocks)
    else:
        backing, count = sum(sizes), 3
    return {"logical_payload_bytes": sum(sizes),
            "peak_unique_owned_backing_bytes": backing,
            "allocation_count": 3, "provider_allocation_count": count,
            "steady_state_provider_allocations": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-exe", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    executable = args.image_exe.resolve()
    if not executable.is_file():
        parser.error("--image-exe must be a regular file")
    directory = args.out_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    if args.runtime_dir:
        environment["PATH"] = str(args.runtime_dir.resolve()) + os.pathsep + environment.get("PATH", "")
    rows, models, fixtures = [], [], []

    def run(case_id: str, source: Path, target: Path, provider: str, layout: str,
            frames: int, extra: list[str], expected: bytes | None, checksum: int | None,
            width: int = 0, height: int = 0, expected_error: str | None = None) -> None:
        command = [str(executable), str(source), str(target), "--provider", provider,
                   "--layout", layout, "--frames", str(frames)] + extra
        try:
            result = subprocess.run(command, capture_output=True, env=environment,
                                    timeout=30, check=False)
            stdout, stderr = result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace")
            parsed = json.loads(stdout) if stdout.strip() else {}
            code, launch_error = result.returncode, None
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            code, stdout, stderr, parsed, launch_error = None, "", "", {}, type(error).__name__
        (directory / f"{case_id}.stdout.json").write_text(stdout, encoding="utf-8")
        (directory / f"{case_id}.stderr.txt").write_text(stderr, encoding="utf-8")
        checks = {"launched_and_parsed": code is not None and launch_error is None,
                  "closed": parsed.get("closed") is True,
                  "cleanup_observed_success": parsed.get("cleanup_ok") is True,
                  "closed_backing_zero": parsed.get("closed_backing_bytes") == 0,
                  "performance_not_qualified": parsed.get("performance_qualification") == "NOT_STARTED"}
        if expected_error:
            checks.update({"expected_failure": code is not None and code != 0 and parsed.get("success") is False,
                           "expected_error": expected_error in parsed.get("error", ""),
                           "no_output_created": not target.is_file()})
        else:
            predicted = model(width, height, layout, provider)
            checks.update({"command_success": code == 0 and parsed.get("success") is True,
                           "full_output_equal_scalar_oracle": target.is_file() and target.read_bytes() == expected,
                           "all_frames_consumed": parsed.get("completed_frames") == frames,
                           "checksum_equal": parsed.get("checksum") == checksum*frames,
                           "each_gray_pixel_converted_once_per_frame": parsed.get("gray_conversions") == width*height*frames,
                           "provider_and_layout": parsed.get("provider") == provider and parsed.get("layout") == layout})
            checks.update({key + "_model_equal": parsed.get(key) == value for key, value in predicted.items()})
        if provider != "direct" and (expected_error is None or expected_error in ("injected worker exception", "cannot open output file")):
            checks["whole_allocation_exclusivity"] = parsed.get("whole_allocation_exclusive") is True
            if "--close-race" in extra:
                checks["admitted_worker_close_while_leased"] = parsed.get("close_while_leased_verified") is True
        rows.append({"case_id": case_id, "provider": provider, "layout": layout,
                     "frames": frames, "extra": extra, "input_sha256": digest(source.read_bytes()),
                     "output_sha256": digest(target.read_bytes()) if target.is_file() else None,
                     "exit_code": code, "launch_error": launch_error,
                     "result": parsed, "checks": checks,
                     "verdict": "PASS" if all(checks.values()) else "FAIL"})

    for width, height in DIMENSIONS:
        name = f"{width}x{height}"
        rgb = rgb_fixture(width, height)
        source = directory / f"{name}.ppm"
        source.write_bytes(f"P6\n{width} {height}\n255\n".encode() + rgb)
        expected, checksum = oracle(width, height, rgb)
        fixtures.append({"dimensions": [width, height], "input_sha256": digest(source.read_bytes()),
                         "oracle_sha256": digest(expected), "oracle_checksum": checksum})
        for provider, layout in VARIANTS:
            models.append({"dimensions": [width, height], "provider": provider, "layout": layout,
                           **model(width, height, layout, provider)})
            for frames in ((1,) if width == 1920 else (1, 3)):
                case_id = f"{name}-{provider}-{layout}-batch{frames}"
                run(case_id, source, directory / f"{case_id}.pgm", provider, layout, frames,
                    ["--close-race"], expected, checksum, width, height)

    source = directory / "17x13.ppm"
    expected, checksum = oracle(17, 13, rgb_fixture(17, 13))
    for provider, layout in VARIANTS:
        run(f"standard-finish-{provider}-{layout}", source,
            directory / f"standard-finish-{provider}-{layout}.pgm",
            provider, layout, 3, [], expected, checksum, 17, 13)
    bad_target = directory / "output-is-a-directory"
    bad_target.mkdir(exist_ok=True)
    for provider, layout in VARIANTS:
        run(f"worker-failure-{provider}-{layout}", source, directory / f"failed-worker-{provider}-{layout}.pgm",
            provider, layout, 1, ["--close-race", "--fault-worker"], None, None,
            expected_error="injected worker exception")
        run(f"output-failure-{provider}-{layout}", source, bad_target,
            provider, layout, 1, ["--close-race"], None, None,
            expected_error="cannot open output file")
    invalid = {"truncated": b"P6\n2 2\n255\n\x00",
               "overflow": b"P6\n18446744073709551615 2\n255\n"}
    for kind, contents in invalid.items():
        source = directory / f"invalid-{kind}.ppm"
        source.write_bytes(contents)
        for provider, layout in VARIANTS:
            run(f"invalid-{kind}-{provider}-{layout}", source, directory / f"invalid-{kind}-{provider}-{layout}.pgm",
                provider, layout, 1, [], None, None,
                expected_error="truncated input" if kind == "truncated" else "size overflow")

    receipt = {"schema_version": 1, "task": "MM-014.R5", "scope": "HOST_FUNCTIONAL_RESOURCE_DIAGNOSTIC",
               "executable_sha256": digest(executable.read_bytes()),
               "verifier_sha256": digest(Path(__file__).read_bytes()), "python_version": sys.version.split()[0],
               "fixtures": fixtures, "resource_models": models, "commands": rows,
               "command_count": len(rows), "passed_count": sum(row["verdict"] == "PASS" for row in rows),
               "all_passed": all(row["verdict"] == "PASS" for row in rows),
               "performance_qualification": "NOT_STARTED", "device_execution": "NOT_STARTED",
               "limitations": ["Elapsed fields are diagnostics including worker launch, full output and observer checks; no performance comparison is qualified.",
                               "Repeated identical images form a controlled functional batch, not evidence of a production batch workload.",
                               "Source preload is common external capacity, reported separately and released within the task before its final clock.",
                               "Backing is requested provider capacity, not malloc usable size, RSS, PSS, energy or all metadata.",
                               "Memory close is deterministic after lease admission and before worker access; the owner path cannot destroy a live domain.",
                               "Direct System keeps owner storage until worker join; it does not expose the Memory close API.",
                               "Row layout savings belong to the ordinary consumer; no Memory-specific time benefit is claimed."]}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    model_check = {"schema_version": 1, "task": "MM-014.R5", "status": "PASS" if receipt["all_passed"] else "FAIL",
                   "scope": "REQUESTED_PAYLOAD_AND_PROVIDER_CAPACITY_ONLY",
                   "layout_benefit_owner": "ORDINARY_CONSUMER_NOT_MEMORY",
                   "all_normal_case_model_fields_match": all(
                       all(value for key, value in row["checks"].items() if key.endswith("_model_equal"))
                       for row in rows),
                   "1080p": [entry for entry in models if entry["dimensions"] == [1920, 1080]],
                   "all_models": models,
                   "rss_pss_time_energy": "UNMEASURED_OR_UNQUALIFIED"}
    args.receipt.with_name("workspace_model_check.json").write_text(
        json.dumps(model_check, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"all_passed": receipt["all_passed"], "passed": receipt["passed_count"],
                      "commands": len(rows), "receipt": str(args.receipt)}, ensure_ascii=False))
    return 0 if receipt["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
