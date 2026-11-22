"""API setup for station operations and physical ballot interfaces."""

from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.api_setup import (
    create_constituency,
    create_election,
    create_staff,
    create_station,
    expect_json,
)

OPENED_AT = "2026-11-22T08:30:00+08:00"


@dataclass(frozen=True)
class PollingOperations:
    election_id: int
    station_id: int
    destination_id: int
    counter_id: int
    schedule_revision: int

    @property
    def station(self) -> str:
        return f"/api/workspace/stations/{self.station_id}"


@pytest.fixture
def operations(admin_client: TestClient) -> PollingOperations:
    election = create_election(admin_client)
    election_id = election["id"]
    constituency = create_constituency(admin_client, election_id)
    base = f"/api/elections/{election_id}"
    district = f"{base}/constituencies/{constituency['id']}"
    expect_json(
        admin_client.post(
            f"{district}/candidates",
            json={"name": "S. Chan", "ballot_number": 1, "request_id": str(uuid4())},
        ),
        201,
    )
    destination = create_station(admin_client, election_id, constituency["id"], code="N1")
    origin = expect_json(
        admin_client.post(
            f"{district}/stations",
            json={
                "code": "N2",
                "name": "Sending polling station",
                "counting_mode": "send",
                "destination_station_id": destination["id"],
                "request_id": str(uuid4()),
            },
        ),
        201,
    )
    counters = []
    for station in (destination, origin):
        counters.append(
            expect_json(
                admin_client.post(
                    f"{district}/stations/{station['id']}/counters",
                    json={"code": "C1", "name": "Counter 1", "request_id": str(uuid4())},
                ),
                201,
            )["id"]
        )
    schedule = expect_json(
        admin_client.put(
            f"{base}/schedule",
            json={
                "election_date": "2026-11-22",
                "planned_opening_at": OPENED_AT,
                "planned_closing_at": "2026-11-22T16:30:00+08:00",
                "reporting_interval_minutes": 60,
                "revision": 0,
            },
        )
    )
    expect_json(
        admin_client.post(
            f"{base}/activate",
            json={
                "election_revision": election["revision"],
                "schedule_revision": schedule["revision"],
            },
        )
    )
    return PollingOperations(
        election_id, origin["id"], destination["id"], counters[1], schedule["revision"]
    )


@pytest.fixture
def polling_headers(
    admin_client: TestClient, api_client: TestClient, operations: PollingOperations
) -> dict[str, str]:
    return create_staff(
        admin_client,
        api_client,
        username="operations.polling",
        role="polling_officer",
        station_ids=[operations.station_id],
    )
