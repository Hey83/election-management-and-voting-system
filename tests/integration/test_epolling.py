"""Exercise electronic polling through the station API against MySQL."""

from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.api_setup import (
    READY_PASSWORD,
    create_constituency,
    create_election,
    create_staff,
    create_station,
    expect_json,
)

pytestmark = pytest.mark.mysql

OPENING = "2026-11-22T08:30:00+08:00"
ADMISSION_CLOSURE = "2026-11-22T17:00:00+08:00"
SERVICE_COMPLETION = "2026-11-22T17:15:00+08:00"


@dataclass(frozen=True)
class ElectronicStation:
    id: int
    path: str
    other_path: str
    first_candidate: int
    second_candidate: int
    terminal: dict[str, str]
    supervisor: dict[str, str]


@pytest.fixture
def electronic_station(api_client: TestClient, admin_client: TestClient) -> ElectronicStation:
    election = create_election(admin_client, voting_method="electronic")
    constituency = create_constituency(admin_client, election["id"])
    election_path = f"/api/elections/{election['id']}"
    candidate_path = f"{election_path}/constituencies/{constituency['id']}/candidates"
    second = expect_json(
        admin_client.post(
            candidate_path,
            json={"name": "Jamie Wong", "ballot_number": 2, "request_id": str(uuid4())},
        ),
        201,
    )
    first = expect_json(
        admin_client.post(
            candidate_path,
            json={"name": "Alex Chan", "ballot_number": 1, "request_id": str(uuid4())},
        ),
        201,
    )
    station = create_station(admin_client, election["id"], constituency["id"], code="N1")
    other = create_station(admin_client, election["id"], constituency["id"], code="N2")
    schedule = expect_json(
        admin_client.put(
            f"{election_path}/schedule",
            json={
                "election_date": "2026-11-22",
                "planned_opening_at": OPENING,
                "planned_closing_at": ADMISSION_CLOSURE,
                "reporting_interval_minutes": 60,
                "revision": 0,
            },
        )
    )
    admin_client.post(
        f"{election_path}/activate",
        json={"election_revision": election["revision"], "schedule_revision": schedule["revision"]},
    ).raise_for_status()
    terminal = create_staff(
        admin_client,
        api_client,
        username="test.terminal",
        role="polling_officer",
        station_ids=[station["id"]],
    )
    supervisor = create_staff(
        admin_client,
        api_client,
        username="test.supervisor",
        role="supervisor",
        station_ids=[station["id"]],
    )
    return ElectronicStation(
        id=station["id"],
        path=f"/api/workspace/stations/{station['id']}",
        other_path=f"/api/workspace/stations/{other['id']}",
        first_candidate=first["id"],
        second_candidate=second["id"],
        terminal=terminal,
        supervisor=supervisor,
    )


def approve_polling_event(
    client: TestClient, station: ElectronicStation, kind: str, event_at: str
) -> None:
    path = f"{station.path}/polling/{kind}"
    draft = expect_json(
        client.put(
            path,
            headers=station.terminal,
            json={"request_id": str(uuid4()), "revision": 0, "event_at": event_at},
        )
    )
    submitted = expect_json(
        client.post(
            f"{path}/submit",
            headers=station.terminal,
            json={"request_id": str(uuid4()), "revision": draft["revision"]},
        )
    )
    client.post(
        f"{path}/approve",
        headers=station.supervisor,
        json={
            "request_id": str(uuid4()),
            "revision": submitted["revision"],
            "password": READY_PASSWORD,
        },
    ).raise_for_status()


def test_vote_retry_changes_tally_once_and_recovery_only_returns_status(
    api_client: TestClient, admin_client: TestClient, electronic_station: ElectronicStation
) -> None:
    station = electronic_station
    approve_polling_event(api_client, station, "opening", OPENING)
    reference = {"session_id": str(uuid4())}
    sessions = f"{station.path}/e-polling/sessions"
    start = expect_json(api_client.post(sessions, headers=station.terminal, json=reference))
    assert start["status"] == "pending"
    assert [row["number"] for row in start["candidates"]] == [1, 2]

    vote = {**reference, "candidate_id": station.first_candidate}
    recorded = expect_json(api_client.put(f"{sessions}/vote", headers=station.terminal, json=vote))
    assert recorded == {"status": "recorded"}

    replacement = create_staff(
        admin_client,
        api_client,
        username="test.replacement",
        role="polling_officer",
        station_ids=[station.id],
    )
    status = expect_json(api_client.post(f"{sessions}/status", headers=replacement, json=reference))
    assert status == {"status": "recorded"}

    retry = expect_json(api_client.put(f"{sessions}/vote", headers=station.terminal, json=vote))
    assert retry == {"status": "recorded"}
    tally = expect_json(
        api_client.get(f"{station.path}/e-polling/results", headers=station.supervisor)
    )
    assert tally["total_votes"] == 1
    assert [(row["id"], row["votes"]) for row in tally["candidates"]] == [
        (station.first_candidate, 1),
        (station.second_candidate, 0),
    ]


def test_live_results_require_a_supervisor_assigned_to_the_station(
    api_client: TestClient, electronic_station: ElectronicStation
) -> None:
    station = electronic_station
    officer_view = api_client.get(f"{station.path}/e-polling/results", headers=station.terminal)
    assert officer_view.status_code == 403
    assert officer_view.json()["detail"]["code"] == "supervisor_required"

    other_view = api_client.get(
        f"{station.other_path}/e-polling/results", headers=station.supervisor
    )
    assert other_view.status_code == 403
    assert other_view.json()["detail"]["code"] == "station_access_denied"


def test_voting_service_requires_opening_and_remains_open_for_queued_electors(
    api_client: TestClient, electronic_station: ElectronicStation
) -> None:
    station = electronic_station
    sessions = f"{station.path}/e-polling/sessions"
    reference = {"session_id": str(uuid4())}
    too_early = api_client.post(sessions, headers=station.terminal, json=reference)
    assert too_early.status_code == 409
    assert too_early.json()["detail"]["code"] == "epolling_service_closed"

    approve_polling_event(api_client, station, "opening", OPENING)
    approve_polling_event(api_client, station, "admission_closure", ADMISSION_CLOSURE)
    queued = expect_json(api_client.post(sessions, headers=station.terminal, json=reference))
    assert queued["status"] == "pending"

    approve_polling_event(api_client, station, "service_completion", SERVICE_COMPLETION)
    too_late = api_client.put(
        f"{sessions}/vote",
        headers=station.terminal,
        json={**reference, "candidate_id": station.first_candidate},
    )
    assert too_late.status_code == 409
    assert too_late.json()["detail"]["code"] == "epolling_service_closed"
