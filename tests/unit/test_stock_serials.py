"""Serial partitions and boundary cases from the historical stock API suite."""

import pytest
from pydantic import ValidationError

from election.server.stock_serials import StockRangeInput, pool_totals


@pytest.mark.parametrize(
    ("first", "last", "quantity"),
    [
        pytest.param("0", "0", 1, id="one-digit-zero"),
        pytest.param("000007", "000009", 3, id="inclusive-leading-zeroes"),
        pytest.param("999999999999999998", "999999999999999999", 2, id="18-digit-exact"),
    ],
)
def test_inclusive_range_preserves_printed_serials(first: str, last: str, quantity: int) -> None:
    row = StockRangeInput(pool="ordinary", serial_from=first, serial_to=last)

    assert (row.serial_from, row.serial_to, row.quantity) == (first, last, quantity)


@pytest.mark.parametrize(
    ("first", "last"),
    [
        pytest.param("2", "1", id="reversed"),
        pytest.param("", "1", id="empty"),
        pytest.param("0" * 19, "1" * 19, id="19-digits"),
        pytest.param("-1", "1", id="negative"),
        pytest.param("0.1", "1", id="fraction"),
        pytest.param("０", "１", id="non-ascii-digits"),
        pytest.param(0, "1", id="numeric-input"),
    ],
)
def test_invalid_serial_range_is_rejected(first: object, last: object) -> None:
    with pytest.raises(ValidationError):
        StockRangeInput.model_validate(
            {"pool": "ordinary", "serial_from": first, "serial_to": last}
        )


def test_adjacent_ranges_are_counted_in_their_own_pools() -> None:
    ranges = [
        StockRangeInput(pool="replacement", serial_from="000009", serial_to="000010"),
        StockRangeInput(pool="ordinary", serial_from="000001", serial_to="000008"),
    ]

    assert pool_totals(ranges) == {"ordinary": 8, "replacement": 2, "tendered": 0}


def test_same_serial_in_two_pools_is_an_overlap_despite_different_padding() -> None:
    ranges = [
        StockRangeInput(pool="ordinary", serial_from="000001", serial_to="000008"),
        StockRangeInput(pool="replacement", serial_from="8", serial_to="8"),
    ]

    with pytest.raises(ValueError, match="must not overlap"):
        pool_totals(ranges)
