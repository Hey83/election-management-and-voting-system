"""Station complaint, reporting period and queue interfaces (c68fb54 API tests)."""

import csv
from io import StringIO
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.api_setup import READY_PASSWORD, create_staff, expect_json
from tests.integration.operations_setup import PollingOperations
from tests.integration.operations_setup import operations as operations
from tests.integration.operations_setup import polling_headers as polling_headers

pytestmark = pytest.mark.mysql


@pytest.fixture
def complaint_category(
    api_client: TestClient, operations: PollingOperations, polling_headers: dict[str, str]
) -> dict[str, Any]:
    categories = expect_json(
        api_client.get(f"{operations.station}/complaint-categories", headers=polling_headers)
    )["items"]
    return next(row for row in categories if row["code"] == "9")


def new_complaint(category: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": str(uuid4()),
        "case_reference": "  000AbC  ",
        "received_at": "2026-11-22T08:45:00+08:00",
        "category_id": category["id"],
        "expected_category_revision": category["revision"],
        "description": "投票站入口安排需要協助。",
        "work_status": "in_progress",
    }


def test_complaint_retry_preserves_original_reference_category_and_times(
    api_client: TestClient,
    operations: PollingOperations,
    polling_headers: dict[str, str],
    complaint_category: dict[str, Any],
) -> None:
    path = f"{operations.station}/complaints"
    command = new_complaint(complaint_category)
    created = expect_json(api_client.post(path, headers=polling_headers, json=command), 201)
    assert created["case_reference"] == "000AbC"
    assert created["category_label_tc"] == "投票安排"
    assert created["category_source"] == "verified_reference"
    assert created["received_at"] == "2026-11-22T00:45:00Z"
    assert created["entered_at"] == "2026-11-22T10:00:00Z"
    assert created["period_index"] == 1

    replay = api_client.post(path, headers=polling_headers, json=command)
    assert expect_json(replay) == created
    conflicting_retry = api_client.post(
        path, headers=polling_headers, json={**command, "description": "Changed content"}
    )
    assert expect_json(conflicting_retry, 409)["detail"]["code"] == "request_conflict"
    assert expect_json(api_client.get(path, headers=polling_headers))["items"] == [created]


def test_disabled_complaint_category_preserves_existing_case_history(
    api_client: TestClient,
    admin_client: TestClient,
    operations: PollingOperations,
    polling_headers: dict[str, str],
    complaint_category: dict[str, Any],
) -> None:
    path = f"{operations.station}/complaints"
    command = new_complaint(complaint_category)
    original = expect_json(api_client.post(path, headers=polling_headers, json=command), 201)
    catalog = f"/api/elections/{operations.election_id}/complaint-categories"
    edited = expect_json(
        admin_client.put(
            f"{catalog}/{complaint_category['id']}",
            json={
                "request_id": str(uuid4()),
                "expected_revision": complaint_category["revision"],
                "label_en": "Updated display name",
                "label_tc": "新名稱",
                "label_sc": "新名称",
                "enabled": False,
                "description_required": complaint_category["description_required"],
            },
        )
    )
    completion = {
        **command,
        "request_id": str(uuid4()),
        "expected_revision": original["revision"],
        "expected_category_revision": edited["revision"],
        "work_status": "completed",
        "completed_at": "2026-11-22T09:10:00+08:00",
    }
    case_path = f"{path}/{original['id']}"
    completed = expect_json(api_client.put(case_path, headers=polling_headers, json=completion))
    assert completed["category_revision"] == original["category_revision"]
    assert completed["category_label_tc"] == original["category_label_tc"]
    assert completed["completed_at"] == "2026-11-22T01:10:00Z"

    attempted = new_complaint(edited)
    attempted["case_reference"] = "000AbD"
    forbidden = api_client.post(path, headers=polling_headers, json=attempted)
    assert expect_json(forbidden, 422)["detail"]["code"] == "complaint_category_disabled"
    assert expect_json(api_client.get(f"{case_path}/history", headers=polling_headers))[
        "items"
    ] == [
        original,
        completed,
    ]


def test_approved_zero_complaint_period_is_distinct_from_missing_in_report(
    api_client: TestClient,
    admin_client: TestClient,
    operations: PollingOperations,
    polling_headers: dict[str, str],
) -> None:
    reviewer = create_staff(
        admin_client,
        api_client,
        username="operations.complaint-reviewer",
        role="supervisor",
        station_ids=[operations.station_id],
    )
    periods = f"{operations.station}/complaint-periods"
    initial = expect_json(api_client.get(periods, headers=polling_headers))
    assert initial["items"][0]["status"] == "missing"
    assert initial["items"][0]["record"] is None

    def period_command(revision: int) -> dict[str, str | int]:
        return {
            "request_id": str(uuid4()),
            "expected_revision": revision,
            "expected_schedule_revision": operations.schedule_revision,
        }

    draft = expect_json(
        api_client.put(f"{periods}/1", headers=polling_headers, json=period_command(0))
    )
    assert draft["status"] == "draft"
    assert draft["total_received"] == 0
    submitted = expect_json(
        api_client.post(
            f"{periods}/1/submit", headers=polling_headers, json=period_command(draft["revision"])
        )
    )
    approved = expect_json(
        api_client.post(
            f"{periods}/1/approve",
            headers=reviewer,
            json={**period_command(submitted["revision"]), "password": READY_PASSWORD},
        )
    )
    assert approved["status"] == "approved" and approved["approval_valid"] is True
    report = expect_json(api_client.get(f"{periods}/statement?locale=en", headers=reviewer))
    assert report["summary"] == {"required": 8, "approved_current": 1, "missing": 7}
    assert report["saved_period_count"] == 1 and report["items"][0]["total_received"] == 0

    response = api_client.get(f"{periods}/statement.csv?locale=en", headers=reviewer)
    assert response.status_code == 200
    csv_rows = list(csv.reader(StringIO(response.content.decode("utf-8-sig"))))
    assert ["Periods with no saved record", "7"] in csv_rows


def queue_observation(time: str, *, revision: int = 0, band: str = "under_30") -> dict[str, Any]:
    return {
        "request_id": str(uuid4()),
        "expected_revision": revision,
        "event_at": time,
        "band": band,
    }


def test_queue_reminder_boundary_and_backdated_history(
    api_client: TestClient, operations: PollingOperations, polling_headers: dict[str, str]
) -> None:
    path = f"{operations.station}/queue"

    def snapshot() -> dict[str, Any]:
        return expect_json(api_client.get(path, headers=polling_headers))

    missing = snapshot()
    assert missing["missing"] is True and missing["current"] is None
    assert missing["due_at"] is None and missing["reminder_due"] is False
    at_boundary = queue_observation("2026-11-22T17:30:00+08:00")
    expect_json(api_client.post(path, headers=polling_headers, json=at_boundary), 201)
    due = snapshot()
    assert due["due_at"] == "2026-11-22T10:00:00Z"
    assert due["reminder_due"] is True and due["overdue_periods"] == 1

    near_boundary = queue_observation("2026-11-22T17:30:01+08:00", revision=1, band="30_59")
    current = expect_json(api_client.post(path, headers=polling_headers, json=near_boundary), 201)
    earlier = queue_observation("2026-11-22T17:15:00+08:00", revision=2, band="over_90")
    late_entry = expect_json(api_client.post(path, headers=polling_headers, json=earlier), 201)
    updated = snapshot()
    assert updated["items"][-1] == late_entry
    assert updated["current"] == current
    assert updated["due_at"] == "2026-11-22T10:00:01Z"
    assert updated["reminder_due"] is False and updated["overdue_periods"] == 0


def test_queue_confirmation_replays_without_appending_or_changing_the_band(
    api_client: TestClient, operations: PollingOperations, polling_headers: dict[str, str]
) -> None:
    path = f"{operations.station}/queue"
    observed = expect_json(
        api_client.post(
            path,
            headers=polling_headers,
            json=queue_observation("2026-11-22T17:20:00+08:00", band="60_90"),
        ),
        201,
    )
    command = {
        "request_id": str(uuid4()),
        "expected_revision": observed["revision"],
        "current_event_id": observed["id"],
        "event_at": "2026-11-22T17:40:00+08:00",
    }
    confirm_path = f"{path}/confirm"
    confirmed = expect_json(
        api_client.post(confirm_path, headers=polling_headers, json=command), 201
    )
    assert confirmed["kind"] == "unchanged_confirmation"
    assert confirmed["band"] == observed["band"]
    assert confirmed["confirmed_event_id"] == observed["id"]
    latest = expect_json(api_client.get(path, headers=polling_headers))
    assert latest["items"] == [observed, confirmed]
    replay = api_client.post(confirm_path, headers=polling_headers, json=command)
    assert expect_json(replay) == confirmed
    assert expect_json(api_client.get(path, headers=polling_headers)) == latest
