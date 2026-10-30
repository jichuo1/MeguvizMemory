#!/usr/bin/env python3
"""Execute the frozen four-group suite after current device admission.

This entry point is separate from the historical offline-only draft utility.
It never invents release, freezes missing fields, or grants performance/BIL
qualification. Formal family eligibility is recomputed from the raw pilot.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from device_capture_v2 import CaptureV2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--admission", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("stage", "pilot", "formal"), required=True)
    parser.add_argument("--window")
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--pilot-journal", type=Path)
    args = parser.parse_args()
    if args.phase != "stage" and args.deployment is None:
        parser.error("--deployment is required for pilot/formal")
    if args.phase == "formal" and args.pilot_journal is None:
        parser.error("--pilot-journal is required before formal capture")
    try:
        runner = CaptureV2(Path(__file__).resolve().parents[1], args.adb, args.serial,
                           args.admission, args.protocol, args.plan, args.out_dir,
                           args.phase, window_id=args.window,
                           deployment_path=args.deployment,
                           pilot_journal_path=args.pilot_journal)
        result = runner.run()
        print(json.dumps({"state": result["state"], "phase": args.phase,
                          "native_tasks_recorded": result["native_tasks_recorded"],
                          "performance_qualified": False}, ensure_ascii=False))
        return 0
    except (RuntimeError, ValueError, OSError) as failure:
        # Source and path details belong to the local failure receipt. Avoid
        # echoing private command arguments or input paths from an exception.
        print(json.dumps({"state": "CAPTURE_STOPPED", "failure_type": type(failure).__name__,
                          "receipt_directory": args.out_dir.name,
                          "performance_qualified": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
