"""Schedule interval boundaries adapted from the historical turnout API cases."""

from datetime import datetime

import pytest

from election.server.lifecycle_models import ElectionSchedule
from election.server.reporting_periods import period_bounds, period_count


@pytest.fixture
def overnight_schedule() -> ElectionSchedule:
    return ElectionSchedule(
        planned_opening_at=datetime.fromisoformat("2026-11-15T23:30:00+08:00"),
        planned_closing_at=datetime.fromisoformat("2026-11-16T00:10:00+08:00"),
        reporting_interval_minutes=30,
    )


def test_final_partial_interval_stops_at_closing(overnight_schedule: ElectionSchedule) -> None:
    assert period_count(overnight_schedule) == 2
    assert [
        tuple(value.isoformat() for value in period_bounds(overnight_schedule, index))
        for index in (1, 2)
    ] == [
        ("2026-11-15T23:30:00+08:00", "2026-11-16T00:00:00+08:00"),
        ("2026-11-16T00:00:00+08:00", "2026-11-16T00:10:00+08:00"),
    ]


@pytest.mark.parametrize("index", [0, 3], ids=["before-first", "after-last"])
def test_period_index_outside_schedule_is_rejected(
    overnight_schedule: ElectionSchedule, index: int
) -> None:
    with pytest.raises(IndexError, match="outside the configured schedule"):
        period_bounds(overnight_schedule, index)


def test_longest_supported_schedule_keeps_last_partial_minute() -> None:
    schedule = ElectionSchedule(
        planned_opening_at=datetime(1000, 1, 1),
        planned_closing_at=datetime.max,
        reporting_interval_minutes=1,
    )

    assert period_count(schedule) == 4_733_542_080
    assert period_bounds(schedule, 4_733_542_080) == (
        datetime(9999, 12, 31, 23, 59),
        datetime.max,
    )
