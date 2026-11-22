"""Exercise the paper-ballot F4 count projection from F1 and F2 observations."""

from typing import Any

import pytest

from election.server.counting_totals import f2_summary


def count_summary(second_candidate: int | None, rejected: int) -> dict[str, Any]:
    return f2_summary(
        {
            "candidate_votes": [
                {"candidate_id": 1, "quantity": 4},
                {"candidate_id": 2, "quantity": 1},
            ],
            "clearly_invalid_quantity": 1,
            "questionable_quantity": 3,
            "actual_counted_quantity": 9,
            "f1_complete": True,
            "blockers": [],
            "scope_basis": {"estimated_box_from_stock_quantity": 9},
            "f2": {
                "determination_started_at": "2026-11-08T23:00:00+08:00",
                "accepted_votes": [
                    {"candidate_id": 1, "quantity": 2},
                    {"candidate_id": 2, "quantity": second_candidate},
                ],
                "rejected_papers": [{"reason_id": 1, "quantity": rejected}],
            },
        }
    )


def test_balanced_adjudication_accounts_for_each_paper_once() -> None:
    summary = count_summary(second_candidate=0, rejected=1)

    assert [(row["candidate_id"], row["quantity"]) for row in summary["final_candidate_votes"]] == [
        (1, 6),
        (2, 1),
    ]
    assert summary["final_valid_quantity"] == 7
    assert summary["final_invalid_quantity"] == 2
    assert summary["final_accounted_quantity"] == 9
    assert summary["count_inputs_complete"] is True
    assert summary["count_complete"] is False
    assert summary["blockers"] == []


@pytest.mark.parametrize(
    ("second_candidate", "rejected", "blocker"),
    [(None, 1, "f2_candidate_missing"), (0, 0, "f2_outcomes_unbalanced")],
    ids=["blank-is-not-zero", "questionable-papers-missing"],
)
def test_incomplete_adjudication_cannot_complete_count_inputs(
    second_candidate: int | None, rejected: int, blocker: str
) -> None:
    summary = count_summary(second_candidate, rejected)

    assert summary["count_inputs_complete"] is False
    assert blocker in {row["code"] for row in summary["blockers"]}
