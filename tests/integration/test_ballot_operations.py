"""Ballot box, reserve and paper reentry interfaces (c68fb54 API tests)."""

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.api_setup import create_staff, expect_json
from tests.integration.operations_setup import OPENED_AT, PollingOperations
from tests.integration.operations_setup import operations as operations
from tests.integration.operations_setup import polling_headers as polling_headers

pytestmark = pytest.mark.mysql
SERIES = "Station ballot series A"


@pytest.fixture
def prepared_box(
    api_client: TestClient, operations: PollingOperations, polling_headers: dict[str, str]
) -> dict[str, Any]:
    return expect_json(
        api_client.post(
            f"{operations.station}/boxes",
            headers=polling_headers,
            json={
                "request_id": str(uuid4()),
                "box_reference": "票箱 0001",
                "seal_reference": "封條 000099",
                "prepared_at": "2026-11-22T16:40:00+08:00",
            },
        ),
        201,
    )


@pytest.fixture
def receiver_headers(
    admin_client: TestClient, api_client: TestClient, operations: PollingOperations
) -> dict[str, str]:
    return create_staff(
        admin_client,
        api_client,
        username="operations.counter",
        role="counting_officer",
        station_ids=[operations.destination_id],
    )


def event_command(revision: int, event_at: str) -> dict[str, int | str]:
    return {"request_id": str(uuid4()), "expected_revision": revision, "event_at": event_at}


def test_unreceived_transferred_box_cannot_be_opened_or_received_before_departure(
    api_client: TestClient,
    operations: PollingOperations,
    polling_headers: dict[str, str],
    prepared_box: dict[str, Any],
    receiver_headers: dict[str, str],
) -> None:
    destination = f"/api/workspace/stations/{operations.destination_id}/boxes"
    box_id = prepared_box["id"]
    received = api_client.post(
        f"{destination}/{box_id}/receipt",
        headers=receiver_headers,
        json=event_command(prepared_box["revision"], "2026-11-22T17:10:00+08:00"),
    )
    assert expect_json(received, 409)["detail"]["code"] == "box_departure_required"
    opened = api_client.put(
        f"{destination}/milestones/first_box_opening",
        headers=receiver_headers,
        json={**event_command(0, "2026-11-22T17:15:00+08:00"), "box_id": box_id},
    )
    assert expect_json(opened, 409)["detail"]["code"] == "box_receipt_required"
    history = expect_json(
        api_client.get(f"{operations.station}/boxes/{box_id}/history", headers=polling_headers)
    )
    assert [event["kind"] for event in history["items"]] == ["prepared"]


def test_transferred_box_has_one_receipt_and_records_both_station_roles(
    api_client: TestClient,
    operations: PollingOperations,
    polling_headers: dict[str, str],
    prepared_box: dict[str, Any],
    receiver_headers: dict[str, str],
) -> None:
    source = f"{operations.station}/boxes"
    destination = f"/api/workspace/stations/{operations.destination_id}/boxes"
    box_id = prepared_box["id"]
    departed = expect_json(
        api_client.post(
            f"{source}/{box_id}/departure",
            headers=polling_headers,
            json=event_command(prepared_box["revision"], "2026-11-22T16:45:00+08:00"),
        ),
        201,
    )
    assert api_client.get(source, headers=receiver_headers).status_code == 403
    path = f"{destination}/{box_id}/receipt"
    command = event_command(departed["revision"], "2026-11-22T17:10:00+08:00")
    received = expect_json(api_client.post(path, headers=receiver_headers, json=command), 201)
    assert received["status"] == "received"
    assert received["origin_station_id"] == operations.station_id
    assert received["destination_station_id"] == operations.destination_id
    assert received["departure_at"] == "2026-11-22T08:45:00Z"
    assert received["receipt_at"] == "2026-11-22T09:10:00Z"

    assert expect_json(api_client.post(path, headers=receiver_headers, json=command)) == received
    duplicate = api_client.post(
        path,
        headers=receiver_headers,
        json=event_command(received["revision"], "2026-11-22T17:10:00+08:00"),
    )
    assert expect_json(duplicate, 409)["detail"]["code"] == "box_state_conflict"
    history = expect_json(
        api_client.get(f"{destination}/{box_id}/history", headers=receiver_headers)
    )
    sender = expect_json(api_client.get("/api/auth/me", headers=polling_headers))
    receiver = expect_json(api_client.get("/api/auth/me", headers=receiver_headers))
    assert [(row["kind"], row["actor_id"]) for row in history["items"]] == [
        ("prepared", sender["id"]),
        ("departure", sender["id"]),
        ("receipt", receiver["id"]),
    ]
    assert expect_json(api_client.get(source, headers=polling_headers))["items"] == [received]


@pytest.fixture
def received_stock(
    api_client: TestClient, operations: PollingOperations, polling_headers: dict[str, str]
) -> None:
    expect_json(
        api_client.post(
            f"{operations.station}/stock",
            headers=polling_headers,
            json={
                "request_id": str(uuid4()),
                "series": SERIES,
                "event_at": OPENED_AT,
                "ordinary_quantity": 10,
                "replacement_quantity": 3,
                "tendered_quantity": 2,
                "ranges": [
                    {"pool": "ordinary", "serial_from": "000001", "serial_to": "000010"},
                    {"pool": "replacement", "serial_from": "000011", "serial_to": "000013"},
                    {"pool": "tendered", "serial_from": "000014", "serial_to": "000015"},
                ],
            },
        ),
        201,
    )


def allocation(operations: PollingOperations, *, end: str, event_at: str) -> dict[str, int | str]:
    return {
        "request_id": str(uuid4()),
        "counter_id": operations.counter_id,
        "series": SERIES,
        "serial_from": "000001",
        "serial_to": end,
        "event_at": event_at,
    }


@pytest.mark.usefixtures("received_stock")
def test_special_paper_reserve_limit_keeps_saved_accounting_and_stock_basis(
    api_client: TestClient, operations: PollingOperations, polling_headers: dict[str, str]
) -> None:
    base = operations.station
    expect_json(
        api_client.post(
            f"{base}/allocations",
            headers=polling_headers,
            json=allocation(operations, end="000010", event_at=OPENED_AT),
        ),
        201,
    )
    event_at = "2026-11-22T09:00:00+08:00"
    snapshot = expect_json(
        api_client.post(
            f"{base}/stock-snapshots",
            headers=polling_headers,
            json={
                "request_id": str(uuid4()),
                "event_at": event_at,
                "counters": [
                    {
                        "counter_id": operations.counter_id,
                        "ranges": [
                            {"series": SERIES, "serial_from": "000006", "serial_to": "000010"}
                        ],
                    }
                ],
            },
        ),
        201,
    )
    full_reserve = {
        "series": SERIES,
        "replacement_issued_quantity": 3,
        "spoilt_original_quantity": 3,
        "tendered_issued_quantity": 2,
        "retrieved_refused_quantity": 0,
        "retrieved_found_quantity": 0,
        "retrieved_held_quantity": 0,
    }
    path = f"{base}/special-papers"
    command = {
        "request_id": str(uuid4()),
        "event_at": event_at,
        "stock_snapshot_id": snapshot["id"],
        "stock_snapshot_revision": snapshot["revision"],
        "series": [full_reserve],
    }
    saved = expect_json(api_client.post(path, headers=polling_headers, json=command), 201)
    assert saved["received_quantity"] == 15
    assert saved["ordinary_issued_quantity"] == 5 and saved["estimated_box_papers_quantity"] == 5
    assert saved["replacement_unissued_quantity"] == saved["tendered_unissued_quantity"] == 0
    assert saved["never_issued_quantity"] == 5

    exceeded = api_client.post(
        path,
        headers=polling_headers,
        json={
            **command,
            "request_id": str(uuid4()),
            "event_at": "2026-11-22T09:01:00+08:00",
            "series": [
                {**full_reserve, "replacement_issued_quantity": 4, "spoilt_original_quantity": 4}
            ],
        },
    )
    assert expect_json(exceeded, 409)["detail"]["code"] == "special_paper_bounds"
    assert expect_json(api_client.get(path, headers=polling_headers))["items"] == [saved]
    assert (
        expect_json(
            api_client.get(f"{base}/stock-snapshots/{snapshot['id']}", headers=polling_headers)
        )
        == snapshot
    )


@pytest.mark.usefixtures("received_stock")
def test_outage_paper_allocation_preserves_event_time_and_cannot_be_entered_twice(
    api_client: TestClient, operations: PollingOperations, polling_headers: dict[str, str]
) -> None:
    outages = f"{operations.station}/outages"
    outage = expect_json(
        api_client.post(
            outages,
            headers=polling_headers,
            json={
                "request_id": str(uuid4()),
                "reference": "Polling station network outage",
                "kind": "network",
                "started_at": "2026-11-22T08:40:00+08:00",
                "ended_at": "2026-11-22T09:00:00+08:00",
            },
        ),
        201,
    )
    # A recovered signed paper allocation has its own occurrence time.
    actual = "2026-11-22T08:45:00+08:00"
    outage_path = f"{outages}/{outage['id']}"
    paper = expect_json(
        api_client.post(
            f"{outage_path}/paper-records",
            headers=polling_headers,
            json={
                "request_id": str(uuid4()),
                "expected_outage_revision": 1,
                "paper_reference": "手寫分配 0001",
                "workflow": "allocation",
                "action": "create",
                "original_event_at": actual,
                "target": {"counter_id": operations.counter_id},
            },
        ),
        201,
    )
    path = f"{operations.station}/allocations"
    command = {
        **allocation(operations, end="000002", event_at=actual),
        "paper_record_id": paper["id"],
    }
    wrong_time = api_client.post(
        path,
        headers=polling_headers,
        json={**command, "event_at": "2026-11-22T08:46:00+08:00"},
    )
    assert expect_json(wrong_time, 409)["detail"]["code"] == "paper_record_time_conflict"
    pending = expect_json(api_client.get(outage_path, headers=polling_headers))["record"]
    assert pending["paper_records"][0]["status"] == "pending"
    assert expect_json(api_client.get(path, headers=polling_headers))["items"] == []

    recorded = expect_json(api_client.post(path, headers=polling_headers, json=command), 201)
    assert recorded["event_at"] == "2026-11-22T00:45:00Z" and recorded["quantity"] == 2
    assert expect_json(api_client.post(path, headers=polling_headers, json=command)) == recorded
    duplicate = api_client.post(
        path, headers=polling_headers, json={**command, "request_id": str(uuid4())}
    )
    assert expect_json(duplicate, 409)["detail"]["code"] == "paper_record_already_entered"
    assert expect_json(api_client.get(path, headers=polling_headers))["items"] == [recorded]
    entered = expect_json(api_client.get(outage_path, headers=polling_headers))["record"]
    entry = entered["paper_records"][0]
    assert entry["status"] == "entered"
    assert entry["reentry"]["record"] == recorded
