"""P(14) model-level tests; run with project's Python 3.14 dependencies.

Command from the repository root:
    uv run --locked python -m unittest discover -s tests -p 'test_p14_allocation_validation.py' -v

These tests do not exercise the database, UI, HTTP authorization or recovery flows.
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Enables direct invocation from the repository root without installing a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import ValidationError

from election.server.allocations import AllocationCorrection, AllocationInput, allocation_digest
from election.server.stock_serials import StockRangeInput, pool_totals, serial_number


FIXED_TIME = datetime(2026, 11, 13, 8, 30, tzinfo=timezone.utc)


def valid_allocation(**changes):
    values = {
        "request_id": uuid4(),
        "counter_id": 1,
        "series": "DEMO",
        "serial_from": "00401",
        "serial_to": "00500",
        "event_at": FIXED_TIME,
    }
    values.update(changes)
    return AllocationInput(**values)


class TestP14AllocationValidation(unittest.TestCase):
    def test_inclusive_quantity(self):
        record = valid_allocation()
        self.assertEqual(record.quantity, 100)
        self.assertEqual(record.serial_from, "00401")  # printed form is retained

    def test_single_serial_is_one_paper(self):
        self.assertEqual(valid_allocation(serial_from="00500", serial_to="00500").quantity, 1)

    def test_reversed_range_is_rejected(self):
        with self.assertRaises(ValidationError):
            valid_allocation(serial_from="00500", serial_to="00401")

    def test_non_ascii_digits_are_rejected(self):
        for value in ("12A34", "１２３", "12 34", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                serial_number(value)

    def test_long_serial_is_rejected(self):
        with self.assertRaises(ValueError):
            serial_number("1" * 19)

    def test_blank_series_is_rejected(self):
        with self.assertRaises(ValidationError):
            valid_allocation(series="   ")

    def test_naive_time_is_rejected(self):
        with self.assertRaises(ValidationError):
            valid_allocation(event_at=datetime(2026, 11, 13, 8, 30))

    def test_correction_requires_reason(self):
        fields = valid_allocation().model_dump()
        with self.assertRaises(ValidationError):
            AllocationCorrection(**fields, revision=1, reason="  ")
        corrected = AllocationCorrection(**fields, revision=1, reason="Correct counter number")
        self.assertEqual(corrected.revision, 1)

    def test_request_id_does_not_change_payload_digest(self):
        one = valid_allocation(request_id=uuid4())
        two = valid_allocation(request_id=uuid4())
        self.assertEqual(allocation_digest(one, 1), allocation_digest(two, 1))
        self.assertNotEqual(allocation_digest(one, 1), allocation_digest(one, 2))

    def test_received_range_overlap_is_rejected(self):
        rows = [
            StockRangeInput(pool="ordinary", serial_from="001", serial_to="050"),
            StockRangeInput(pool="replacement", serial_from="050", serial_to="060"),
        ]
        with self.assertRaises(ValueError):
            pool_totals(rows)

    def test_received_pool_totals_remain_separate(self):
        rows = [
            StockRangeInput(pool="ordinary", serial_from="001", serial_to="100"),
            StockRangeInput(pool="replacement", serial_from="101", serial_to="110"),
            StockRangeInput(pool="tendered", serial_from="111", serial_to="120"),
        ]
        self.assertEqual(
            pool_totals(rows), {"ordinary": 100, "replacement": 10, "tendered": 10}
        )


if __name__ == "__main__":
    unittest.main()
