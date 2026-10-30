#!/usr/bin/env python3
"""Offline v2 plan/check entry point. This revision cannot execute on a device.

There is deliberately no ADB discovery, subprocess import, deployment, sampling,
device cleanup or execution branch. A future live collector needs a separate
reviewed implementation after the protocol/device/artifact/cadence gates freeze.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from paired_analysis_v2 import freeze_errors, make_plan, strict_json, write_new_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--draft-plan", type=Path)
    actions.add_argument("--check-plan", type=Path)
    actions.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    # Reject execution even if a caller supplies a document marked FROZEN.
    if args.execute:
        print(json.dumps({"state": "BLOCKED_DEVICE_EXECUTION_NOT_IMPLEMENTED_IN_OFFLINE_BATCH",
                          "reason": "Requires separately reviewed live collector and frozen authorized protocol.",
                          "device_operations": 0}))
        return 2
    protocol = strict_json(args.protocol.read_text(encoding="utf-8-sig"))
    if args.draft_plan:
        plan = make_plan(protocol)
        write_new_json(args.draft_plan, plan)
        print(json.dumps({"state": plan["state"], "tasks": len(plan["entries"]),
                          "device_operations": 0, "output": str(args.draft_plan)}))
        return 0
    plan = strict_json(args.check_plan.read_text(encoding="utf-8-sig"))
    errors = freeze_errors(protocol, plan)
    print(json.dumps({"state": "NOT_READY_FOR_DEVICE_EXECUTION" if errors else "OFFLINE_FREEZE_FIELDS_PRESENT_ONLY",
                      "freeze_errors": errors, "device_operations": 0}))
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
