#!/usr/bin/env python3
"""Negative fixtures for the historical audit; original evidence is read-only."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from audit_paired_archive import BASE, recompute


class AuditFixtures(unittest.TestCase):
    def fixture(self, edit):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        target = root / BASE
        target.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / BASE
        rows = [json.loads(line) for line in (source / "formal.jsonl").read_text(encoding="utf-8").splitlines()]
        plan = json.loads((source / "formal-plan.json").read_text(encoding="utf-8"))
        report = json.loads((source / "report.json").read_text(encoding="utf-8"))
        edit(rows, plan, report)
        raw = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
        planned = json.dumps(plan).encode()
        report["formal_file_sha256"] = hashlib.sha256(raw).hexdigest()
        report["plan_sha256"] = hashlib.sha256(planned).hexdigest()
        (target / "formal.jsonl").write_bytes(raw)
        (target / "formal-plan.json").write_bytes(planned)
        (target / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return root

    def test_missing_row_rejected(self):
        root = self.fixture(lambda rows, plan, report: rows.pop())
        with self.assertRaisesRegex(ValueError, "wrong block/task count"):
            recompute(root)

    def test_reordered_rows_rejected(self):
        def edit(rows, plan, report):
            rows[0], rows[1] = rows[1], rows[0]
        with self.assertRaisesRegex(ValueError, "reordered row"):
            recompute(self.fixture(edit))

    def test_wrong_checksum_rejected_even_with_correct_raw_hash(self):
        def edit(rows, plan, report):
            rows[0]["native"]["result_checksum"] += 1
        with self.assertRaisesRegex(ValueError, "semantic/lifetime"):
            recompute(self.fixture(edit))

    def test_false_as_zero_does_not_pass_lifetime(self):
        def edit(rows, plan, report):
            rows[0]["native"]["closed_backing_bytes"] = False
        with self.assertRaisesRegex(ValueError, "semantic/lifetime"):
            recompute(self.fixture(edit))


if __name__ == "__main__":
    unittest.main()
