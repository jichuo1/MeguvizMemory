#!/usr/bin/env python3
"""Windows file-I/O regression for paired capture. Never invokes ADB.

Uses real no-share-delete Windows reader handles. All test files are retained in
a new workspace build directory; they are synthetic I/O tests, not device data.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import threading
import time
from types import SimpleNamespace

from run_paired_device import (ATOMIC_REPLACE_RETRY_SECONDS, PairedRunner,
                               load_command_journal, public_value, write_json)


class Reader:
    def __init__(self, path):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                           ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        self.kernel.CreateFileW.restype = ctypes.c_void_p
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_int
        # Share read/write, deliberately omit FILE_SHARE_DELETE. An existing
        # target reader then blocks Path.replace until this real handle closes.
        self.handle = self.kernel.CreateFileW(str(path), 0x80000000, 0x00000003,
                                              None, 3, 0x80, None)
        if self.handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle is not None:
            if not self.kernel.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


def replace_in_thread(path, value):
    result, entered = {}, threading.Event()

    def work():
        started = time.monotonic()
        entered.set()
        try:
            write_json(path, value)
            result["success"] = True
        except Exception as failure:
            result.update({"success": False, "exception": type(failure).__name__})
        finally:
            result["elapsed_seconds"] = time.monotonic() - started

    worker = threading.Thread(target=work)
    worker.start()
    assert entered.wait(1), "writer did not start"
    return worker, result


def reader_conflicts(directory):
    target = directory / "atomic-receipt.json"
    old, new = {"version": "old", "complete": list(range(20))}, {"version": "new", "complete": list(range(40))}
    write_json(target, old)
    held = Reader(target)
    worker = None
    observations = []
    try:
        worker, result = replace_in_thread(target, new)
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            observations.append(json.loads(target.read_text(encoding="utf-8")))
            time.sleep(0.01)
        assert worker.is_alive(), "no-share-delete reader did not actually block replacement"
        assert all(value == old for value in observations), "target was partially/directly overwritten"
        held.close()
        deadline = time.monotonic() + 2
        while worker.is_alive() and time.monotonic() < deadline:
            observations.append(json.loads(target.read_text(encoding="utf-8")))
            time.sleep(0.005)
        worker.join(0.1)
        assert not worker.is_alive() and result.get("success") is True, result
        assert json.loads(target.read_text(encoding="utf-8")) == new
        assert all(value in (old, new) for value in observations), "a reader observed incomplete JSON"
    finally:
        held.close()
        if worker is not None:
            worker.join(2)
    short_result = dict(result)

    held = Reader(target)
    worker = None
    held_started = time.monotonic()
    try:
        worker, result = replace_in_thread(target, {"version": "must-not-publish", "complete": [1, 2, 3]})
        worker.join(2)
        assert not worker.is_alive(), "retry exceeded its bounded policy"
        assert result.get("success") is False and result.get("exception") == "PermissionError", result
        assert result["elapsed_seconds"] >= ATOMIC_REPLACE_RETRY_SECONDS * 0.9, result
        assert result["elapsed_seconds"] < 1.25, "retry budget or host scheduling exceeded test tolerance"
        assert json.loads(target.read_text(encoding="utf-8")) == new, "failed replacement changed complete target"
        assert json.loads(target.with_name(target.name + ".pending").read_text(encoding="utf-8"))["version"] == "must-not-publish"
        # Keep the actual conflicting handle open for more than one second, even
        # though the bounded writer has already reported its explicit failure.
        time.sleep(max(0, 1.1 - (time.monotonic() - held_started)))
    finally:
        held.close()
        if worker is not None:
            worker.join(2)
    return {"short_hold_recovered": short_result, "persistent_hold_failed_explicitly": result,
            "reader_observations_complete": len(observations),
            "declared_retry_budget_seconds": ATOMIC_REPLACE_RETRY_SECONDS}


def journal_recovery(directory):
    output = directory / "journal"
    output.mkdir()
    args = SimpleNamespace(phase="pilot", serial="synthetic-file-test-device",
                           input=directory / "not-a-real-input.dex")
    runner = PairedRunner(args, output)  # Construction only; run()/command() are never called.
    runner.save()
    original_snapshot = (output / "pilot-receipt.json").read_bytes()
    first = {"record_id": 1, "name": "synthetic-public-command", "stdout": "hash-only\n",
             "stderr": "", "exit_code": 0,
             "_inventory_stdout": "PRIVATE_MARKER_NEVER_PERSIST",
             "nested": {"public": "kept", "_inventory_stderr": "PRIVATE_MARKER_NEVER_PERSIST"}}
    runner.receipt["commands"].append(first)
    runner.save()
    runner.save()
    first["selected_inventory"] = {"observation": "filtered synthetic update"}
    runner.save()
    second = {"record_id": 2, "name": "synthetic-second-command", "stdout": "complete\n",
              "stderr": "", "exit_code": 0}
    runner.receipt["commands"].append(second)
    runner.save()
    journal = output / "pilot-commands.jsonl"
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [(item["record_id"], item["revision"]) for item in events] == [(1, 1), (1, 2), (2, 1)]
    assert (output / "pilot-receipt.json").read_bytes() == original_snapshot, "full snapshot was not throttled"
    recovered = load_command_journal(journal)
    assert recovered == {1: public_value(first), 2: second}, "journal lost the latest public command update"
    assert "PRIVATE_MARKER_NEVER_PERSIST" not in journal.read_text(encoding="utf-8")
    runner.receipt["state"] = "PASS"
    runner.save()  # State transition must publish immediately despite throttle.
    assert json.loads((output / "pilot-receipt.json").read_text())["state"] == "PASS"
    runner.receipt["completed_at_utc"] = "synthetic-final-marker"
    runner.save(force=True)
    final = json.loads((output / "pilot-receipt.json").read_text())
    assert final["completed_at_utc"] == "synthetic-final-marker"
    assert final["commands"] == [recovered[1], recovered[2]]
    assert "PRIVATE_MARKER_NEVER_PERSIST" not in (output / "pilot-receipt.json").read_text()
    return {"events": len(events), "recovered_commands": len(recovered),
            "duplicate_unchanged_save_suppressed": True, "state_and_final_forced_snapshot": True,
            "private_inventory_excluded": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("requires real Windows file-sharing semantics; no simulated PASS")
    root = Path(__file__).resolve().parents[1]
    output = args.out_dir.resolve() if args.out_dir else root / "build" / ("capture-journal-regression-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(4))
    if not output.is_relative_to(root / "build") or output.exists():
        parser.error("select a new output directory inside workspace build")
    output.mkdir(parents=True, exist_ok=False)
    report = {"scope": "SYNTHETIC_WINDOWS_FILE_IO_REGRESSION_NOT_DEVICE_OR_PERFORMANCE_EVIDENCE", "all_passed": False}
    try:
        report["atomic_replace"] = reader_conflicts(output)
        report["journal"] = journal_recovery(output)
        report["all_passed"] = True
    finally:
        write_json(output / "regression-receipt.json", report)
    print(json.dumps({"all_passed": report["all_passed"], "receipt": str(output / "regression-receipt.json")}))


if __name__ == "__main__":
    main()
