#!/usr/bin/env python3
"""Bounded real P6 image/pressure/close composition with an independent scalar oracle."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(width: int, height: int) -> tuple[bytes, bytes, int]:
    rgb = bytes(channel for y in range(height) for x in range(width)
                for channel in ((17*x+29*y+x*y+11) % 256,
                                (37*x+13*y+3*x*y+53) % 256,
                                (7*x+41*y+5*x*y+101) % 256))
    # Full scalar luma plane, independent of the native three-row ring.
    luma = [(77*rgb[i]+150*rgb[i+1]+29*rgb[i+2]) // 256 for i in range(0, len(rgb), 3)]
    output = bytearray()
    for y in range(height):
        for x in range(width):
            output.append(sum(luma[min(height-1,max(0,y+dy))*width + min(width-1,max(0,x+dx))]
                              for dy in (-1,0,1) for dx in (-1,0,1)) // 9)
    return (f"P6\n{width} {height}\n255\n".encode()+rgb,
            f"P5\n{width} {height}\n255\n".encode()+output, sum(output))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-exe", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--hold-ms", type=int, default=0)
    parser.add_argument("--wall-ms", type=int, default=300000)
    parser.add_argument("--dimensions", default="1x1,1x7,17x13,320x180")
    parser.add_argument("--stability-min-seconds", type=int, default=0)
    args = parser.parse_args()
    if (not 1 <= args.cycles <= 1000000 or not 0 <= args.hold_ms <= 1000 or not 100 <= args.wall_ms <= 3600000
            or not 0 <= args.stability_min_seconds <= 900 or args.stability_min_seconds*1000 >= args.wall_ms):
        parser.error("cycles/hold-ms/wall-ms exceed bounded profile")
    dimensions = []
    try:
        dimensions = [tuple(int(value) for value in part.split("x")) for part in args.dimensions.split(",")]
        if not 1 <= len(dimensions) <= 8 or any(len(d) != 2 or not all(1 <= v <= 4096 for v in d)
                                               or d[0]*d[1] > 8*1024*1024 for d in dimensions):
            raise ValueError()
    except ValueError:
        parser.error("dimensions must contain 1..8 bounded WIDTHxHEIGHT pairs")
    executable = args.image_exe.resolve()
    if not executable.is_file():
        parser.error("image executable must be a file")
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    runtime = None
    if args.runtime_dir:
        runtime = args.runtime_dir.resolve()
        environment["PATH"] = str(runtime) + os.pathsep + environment.get("PATH", "")
    inputs, fixtures, checksum_per_cycle, gray_per_cycle = [], [], 0, 0
    for index, (width,height) in enumerate(dimensions):
        source, oracle, checksum = fixture(width,height)
        source_path, oracle_path = out / f"input-{index}.ppm", out / f"oracle-{index}.pgm"
        source_path.write_bytes(source); oracle_path.write_bytes(oracle)
        inputs += ["--input",str(source_path),"--oracle",str(oracle_path)]
        fixtures.append({"index":index,"width":width,"height":height,"input_sha256":digest(source_path),
                         "oracle_sha256":digest(oracle_path),"checksum":checksum})
        checksum_per_cycle += checksum
        gray_per_cycle += width*height
    rows = []
    for provider in ("system","arena"):
        target = out / provider
        target.mkdir(exist_ok=True)
        command = [str(executable),"--provider",provider,"--out-dir",str(target),"--cycles",str(args.cycles),
                   "--hold-ms",str(args.hold_ms),"--wall-ms",str(args.wall_ms),
                   "--min-duration-ms",str(args.stability_min_seconds*1000)] + inputs
        parsed, launch_error, code, stdout, stderr = {}, None, None, "", ""
        try:
            result = subprocess.run(command,capture_output=True,env=environment,
                                    timeout=args.wall_ms/1000+15,check=False)
            code = result.returncode
            stdout,stderr = result.stdout.decode("utf-8","replace"),result.stderr.decode("utf-8","replace")
            parsed = json.loads(stdout)
        except (OSError,subprocess.TimeoutExpired,json.JSONDecodeError) as error:
            launch_error = type(error).__name__
        (target/"stdout.json").write_text(stdout,encoding="utf-8")
        (target/"stderr.txt").write_text(stderr,encoding="utf-8")
        actual_cycles = parsed.get("cycles_completed", 0)
        if not isinstance(actual_cycles, int) or isinstance(actual_cycles, bool):
            actual_cycles = 0
        frames = actual_cycles*len(dimensions)
        checks = {"success":code == 0 and launch_error is None and parsed.get("success") is True,
                  "all_cycles_completed":actual_cycles >= args.cycles and (args.stability_min_seconds > 0 or actual_cycles == args.cycles),
                  "all_frames_completed":parsed.get("frames_completed") == frames,
                  "all_frame_scalar_bytes_verified_native":parsed.get("frames_completed") == frames and parsed.get("success") is True,
                  "all_checksums":parsed.get("checksum") == checksum_per_cycle*actual_cycles,
                  "each_pixel_converted_once":parsed.get("gray_conversions") == gray_per_cycle*actual_cycles,
                  "every_frame_zero_budget_denial":parsed.get("zero_budget_denials") == frames,
                  "every_frame_explicit_restore_admission":parsed.get("explicit_restore_admissions") == frames,
                  "busy_observed_each_frame":parsed.get("deferred_busy_total",0) >= frames,
                  "quiet_trim_between_sizes":parsed.get("quiet_trim_successes_total",0) >= actual_cycles*(len(dimensions)-1),
                  "each_cycle_poll_and_bounded_timeout":parsed.get("held_close_timeouts") == 2*actual_cycles,
                  "each_cycle_closed_zero":parsed.get("normal_cycles_closed_zero") == actual_cycles,
                  "minimum_active_duration_met":parsed.get("active_loop_duration_ns",0) >= args.stability_min_seconds*1000000000,
                  "duration_within_wall":0 < parsed.get("diagnostic_duration_ns",0) < args.wall_ms*1000000,
                  "no_performance_qualification":parsed.get("performance_qualification") == "NOT_STARTED",
                  "rss_and_energy_not_invented":parsed.get("rss_pss_bytes") is None and parsed.get("energy") is None}
        for entry in fixtures:
            output = target/f"frame-{entry['index']}.pgm"
            checks[f"final_output_{entry['index']}_scalar_equal"] = output.is_file() and digest(output) == entry["oracle_sha256"]
        rows.append({"provider":provider,"exit_code":code,"launch_error":launch_error,"result":parsed,
                     "checks":checks,"verdict":"PASS" if all(checks.values()) else "FAIL"})
    source = Path(__file__).resolve().parent.parent / "consumers/image/pressure_stability.cpp"
    receipt = {"schema_version":1,"suite":"image_pressure_stability_v1","scope":"HOST_BOUNDED_FUNCTIONAL_STABILITY",
               "executable_sha256":digest(executable),"consumer_source_sha256":digest(source),
               "verifier_sha256":digest(Path(__file__)),"python_version":sys.version.split()[0],
               "memory_runtime_sha256":digest(runtime/"mgmem.dll") if runtime and (runtime/"mgmem.dll").is_file() else None,
               "profile":{"minimum_cycles_per_provider":args.cycles,"minimum_active_seconds_per_provider":args.stability_min_seconds,
                          "hold_ms":args.hold_ms,"wall_limit_ms_per_provider":args.wall_ms},
               "fixtures":fixtures,"commands":rows,"passed":sum(row["verdict"] == "PASS" for row in rows),
               "all_passed":all(row["verdict"] == "PASS" for row in rows),"device_execution":"NOT_STARTED",
               "performance_qualification":"NOT_STARTED","limitations":[
                   "Repeated controlled file fixtures are a bounded stability session, not a production workload distribution or an unbounded leak proof.",
                   "The existing ordinary three-row algorithm is unchanged; no allocation reduction or elapsed benefit is attributed to Memory.",
                   "Native compares every frame with the independent Python full-plane scalar oracle, and host independently checks final output files.",
                   "Source and test oracle arrays remain external to the domain; domain backing excludes them, metadata, RSS, PSS and energy.",
                   "The caller keeps the raw domain valid and serializes destroy after both pressure worker and image worker have joined.",
                   "This test injects native pressure requests; it does not create system memory exhaustion or qualify Android callbacks."]}
    args.receipt.parent.mkdir(parents=True,exist_ok=True)
    args.receipt.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"all_passed":receipt["all_passed"],"providers":len(rows),
                      "actual_cycles":[row["result"].get("cycles_completed") for row in rows],
                      "actual_frames":[row["result"].get("frames_completed") for row in rows]}))
    return 0 if receipt["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
