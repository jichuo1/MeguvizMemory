#!/usr/bin/env python3
"""Verify installed consumers with independent data/oracles; no performance claim.

Uses only Python's standard library. Commands are argument arrays (never a shell),
have a timeout, and retain stdout/stderr plus exit status even on failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


DIMENSIONS = [(1, 1), (1, 7), (7, 1), (17, 13), (320, 180)]


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rgb_fixture(width: int, height: int) -> bytes:
    payload = bytearray()
    for y in range(height):
        for x in range(width):
            payload.extend(((17*x + 29*y + x*y + 11) % 256,
                            (37*x + 13*y + 3*x*y + 53) % 256,
                            (7*x + 41*y + 5*x*y + 101) % 256))
    return bytes(payload)


def image_oracle(width: int, height: int, rgb: bytes) -> tuple[bytes, int]:
    # Separate scalar implementation, not a call into the consumer/library.
    luminance = [(77*rgb[i] + 150*rgb[i+1] + 29*rgb[i+2]) // 256
                 for i in range(0, len(rgb), 3)]
    blurred = bytearray()
    for y in range(height):
        for x in range(width):
            total = 0
            for yy in (max(0, y-1), y, min(height-1, y+1)):
                for xx in (max(0, x-1), x, min(width-1, x+1)):
                    total += luminance[yy*width + xx]
            blurred.append(total // 9)
    return f"P5\n{width} {height}\n255\n".encode("ascii") + blurred, sum(blurred)


def decoded(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else value.decode("utf-8", errors="replace")


def run_command(case_id: str, command: list[str], directory: Path, timeout: float,
                input_path: Path, output_path: Path | None = None) -> dict:
    started = time.monotonic()
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout,
                                   cwd=directory, check=False)
        code, stdout, stderr = completed.returncode, decoded(completed.stdout), decoded(completed.stderr)
        timed_out = False
        launch_error = None
    except subprocess.TimeoutExpired as failure:
        code, stdout, stderr = None, decoded(failure.stdout), decoded(failure.stderr)
        timed_out, launch_error = True, None
    except OSError as failure:
        code, stdout, stderr = None, "", ""
        timed_out, launch_error = False, str(failure)
    (directory / f"{case_id}.stdout.txt").write_bytes(stdout.encode("utf-8"))
    (directory / f"{case_id}.stderr.txt").write_bytes(stderr.encode("utf-8"))
    parsed = None
    parse_error = None
    if stdout.strip():
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as failure:
            parse_error = str(failure)
    return {
        "case_id": case_id, "command": command, "timeout_seconds": timeout,
        "exit_code": code, "timed_out": timed_out, "launch_error": launch_error,
        "stdout": stdout, "stderr": stderr, "parsed_json": parsed,
        "json_parse_error": parse_error,
        "diagnostic_runner_wall_seconds": time.monotonic() - started,
        "input_sha256": sha256(input_path),
        "output_sha256": sha256(output_path) if output_path is not None else None,
    }


def add_verdict(row: dict, checks: dict[str, bool]) -> None:
    row["checks"] = checks
    row["verdict"] = "PASS" if all(checks.values()) else "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-exe", type=Path, required=True)
    parser.add_argument("--dex-exe", type=Path)
    parser.add_argument("--dex-fixture", type=Path)
    parser.add_argument("--image-only", action="store_true",
                        help="Verify only the image consumer when no real parser SDK is available")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    if not 0 < args.timeout <= 300:
        parser.error("--timeout must be in (0, 300] seconds")
    if not args.image_only and (args.dex_exe is None or args.dex_fixture is None):
        parser.error("--dex-exe and --dex-fixture are required unless --image-only is selected")
    image_exe = args.image_exe.resolve()
    dex_exe = args.dex_exe.resolve() if args.dex_exe else None
    dex_fixture = args.dex_fixture.resolve() if args.dex_fixture else None
    required_files = [image_exe] + ([] if args.image_only else [dex_exe, dex_fixture])
    for path in required_files:
        if not path.is_file():
            parser.error(f"Required input is not a regular file: {path}")
    directory = args.out_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    fixtures = []

    for width, height in DIMENSIONS:
        name = f"image-{width}x{height}"
        rgb = rgb_fixture(width, height)
        ppm = directory / f"{name}.ppm"
        ppm.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + rgb)
        expected, expected_checksum = image_oracle(width, height, rgb)
        oracle = directory / f"{name}.oracle.pgm"
        oracle.write_bytes(expected)
        fixtures.append({"id": name, "width": width, "height": height,
                         "input_sha256": sha256(ppm), "oracle_sha256": sha256(oracle),
                         "oracle_checksum": expected_checksum})
        for mode in ("system", "arena"):
            case_id = f"{name}-{mode}"
            output = directory / f"{case_id}.pgm"
            command = [str(image_exe), str(ppm), str(output)]
            if mode == "system":
                command.append("--system")
            row = run_command(case_id, command, directory, args.timeout, ppm, output)
            payload = row["parsed_json"] if isinstance(row["parsed_json"], dict) else {}
            add_verdict(row, {
                "command_completed": row["exit_code"] == 0 and not row["timed_out"],
                "consumer_reported_success": payload.get("success") is True,
                "mode": payload.get("mode") == mode,
                "pixels": payload.get("pixels") == width*height,
                "algorithm_checksum": payload.get("checksum") == expected_checksum,
                "output_bytes_equal_python_oracle": output.is_file() and output.read_bytes() == expected,
                "closed_backing_is_zero": payload.get("closed_backing_bytes") == 0,
                "late_worker_close_safe": payload.get("late_worker_close_safe") is True,
            })
            rows.append(row)

    valid_rgb = rgb_fixture(17, 13)
    invalid_inputs = {
        "truncated": b"P6\n17 13\n255\n" + valid_rgb[:-1],
        "too_large": b"P6\n8388609 1\n255\n",
        "overflow": b"P6\n18446744073709551615 2\n255\n",
        "wrong_header": b"P3\n1 1\n255\n\x00\x00\x00",
        "trailing_bytes": b"P6\n1 1\n255\n\x00\x00\x00extra",
    }
    for name, contents in invalid_inputs.items():
        source = directory / f"invalid-{name}.ppm"
        source.write_bytes(contents)
        for mode in ("system", "arena"):
            case_id = f"invalid-{name}-{mode}"
            output = directory / f"{case_id}.pgm"
            command = [str(image_exe), str(source), str(output)]
            if mode == "system":
                command.append("--system")
            row = run_command(case_id, command, directory, args.timeout, source, output)
            add_verdict(row, {"rejected_with_nonzero_exit": row["exit_code"] is not None and row["exit_code"] != 0,
                              "did_not_time_out": not row["timed_out"],
                              "no_output_file_created": not output.exists()})
            rows.append(row)

    # A directory is an unwritable *file target* on Windows and POSIX. This does
    # not require changing ACLs, chmod, or any directory outside --out-dir.
    bad_output = directory / "output-is-a-directory"
    bad_output.mkdir(exist_ok=True)
    source = directory / "image-17x13.ppm"
    for mode in ("system", "arena"):
        command = [str(image_exe), str(source), str(bad_output)]
        if mode == "system":
            command.append("--system")
        row = run_command(f"unwritable-output-{mode}", command, directory, args.timeout, source)
        row["failure_contract"] = "Existing directory cannot be opened as an output file; no ACL claim is made."
        add_verdict(row, {"rejected_with_nonzero_exit": row["exit_code"] is not None and row["exit_code"] != 0,
                          "did_not_time_out": not row["timed_out"],
                          "directory_preserved": bad_output.is_dir()})
        rows.append(row)

    dex_rows = []
    for mode in (() if args.image_only else ("system", "arena")):
        command = [str(dex_exe), str(dex_fixture)]
        if mode == "arena":
            command.append("--arena")
        row = run_command(f"dex-{mode}", command, directory, args.timeout, dex_fixture)
        payload = row["parsed_json"] if isinstance(row["parsed_json"], dict) else {}
        add_verdict(row, {
            "command_completed": row["exit_code"] == 0 and not row["timed_out"],
            "consumer_reported_pass": payload.get("status") == "PASS",
            "mode": payload.get("mode") == mode,
            "input_hash": payload.get("source_sha256") == sha256(dex_fixture),
            "real_nonempty_query": isinstance(payload.get("method_count"), int) and payload["method_count"] > 0,
            "copied_oracle_equal": payload.get("copied_oracle_equal") is True,
            "closed_backing_is_zero": payload.get("closed_backing_bytes") == 0,
        })
        rows.append(row)
        dex_rows.append(row)
    dex_payloads = [row["parsed_json"] if isinstance(row["parsed_json"], dict) else {} for row in dex_rows]
    matching_keys = ("source_sha256", "input_bytes", "method_count", "result_checksum")
    dex_modes_equal = None if args.image_only else all(
        key in dex_payloads[0] and key in dex_payloads[1] and
        dex_payloads[0][key] == dex_payloads[1][key] for key in matching_keys)

    receipt = {
        "schema_version": 1,
        "protocol_id": "MM-003-host-functional-diagnostic-v1",
        "scope": "INSTALLED_CONSUMER_HOST_FUNCTIONAL_VERIFICATION",
        "python_version": sys.version,
        "selected_consumers": ["image"] if args.image_only else ["image", "DexMeguviz"],
        "dex_validation": "NOT_SELECTED_IMAGE_ONLY" if args.image_only else "EXECUTED",
        "executables": {"image": {"sha256": sha256(image_exe)},
                        "dex": None if args.image_only else {"sha256": sha256(dex_exe)}},
        "fixture_generator": "deterministic_rgb_integer_v1",
        "image_oracle": "Python scalar integer luma (77R+150G+29B)//256 then edge-clamped 3x3 sum//9",
        "fixtures": fixtures,
        "dex_system_arena_results_equal": dex_modes_equal,
        "commands": rows,
        "all_passed": all(row["verdict"] == "PASS" for row in rows) and dex_modes_equal is not False,
        "performance_validation": "NOT_STARTED",
        "mechanism_evidence": "NOT_APPLICABLE",
        "limitations": [
            "Runner wall times include launch, capture and verification; they are not performance samples.",
            "Each executable uses its installed package link contract; runtime-library identity must be paired with the build/install artifact lock.",
            "DEX copy-in is a semantic oracle, not a matched storage/performance control.",
            "Nonzero failure tests do not independently observe native cleanup; ownership/failure tests supply that evidence.",
            "No Android device, PMU/PFN, RSS/PSS or energy qualification is claimed."
        ],
    }
    (directory / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"all_passed": receipt["all_passed"], "command_count": len(rows),
                      "dex_system_arena_results_equal": dex_modes_equal,
                      "receipt": str(directory / "receipt.json")}, ensure_ascii=False))
    return 0 if receipt["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
