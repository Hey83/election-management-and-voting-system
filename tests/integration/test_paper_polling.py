"""Regressions from the polling, turnout and EPR fallback API suites at c68fb54."""

from dataclasses import dataclass
from typing import Any
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


@dataclass(frozen=True)
class PaperStation:
    path: str
    other_station_id: int
    schedule_revision: int
    officer: dict[str, str]
    supervisor: dict[str, str]


@pytest.fixture
def paper_station(admin_client: TestClient, api_client: TestClient) -> PaperStation:
    election = create_election(admin_client)
    constituency = create_constituency(admin_client, election["id"])
    election_path = f"/api/elections/{election['id']}"
    constituency_path = f"{election_path}/constituencies/{constituency['id']}"
    expect_json(
        admin_client.post(
            f"{constituency_path}/candidates",
            json={"name": "Alex Chan", "ballot_number": 1, "request_id": str(uuid4())},
        ),
        201,
    )
    station = create_station(admin_client, election["id"], constituency["id"], code="P1")
    other = create_station(admin_client, election["id"], constituency["id"], code="P2")
    for station_id in (station["id"], other["id"]):
        expect_json(
            admin_client.post(
                f"{constituency_path}/stations/{station_id}/counters",
                json={"code": "C1", "name": "Counter 1", "request_id": str(uuid4())},
            ),
            201,
        )
    schedule = expect_json(
        admin_client.put(
            f"{election_path}/schedule",
            json={
                "election_date": "2026-11-22",
                "planned_opening_at": OPENING,
                "planned_closing_at": "2026-11-22T10:30:00+08:00",
                "reporting_interval_minutes": 60,
                "revision": 0,
            },
        )
    )
    expect_json(
        admin_client.post(
            f"{election_path}/activate",
            json={
                "election_revision": election["revision"],
                "schedule_revision": schedule["revision"],
            },
        )
    )
    officer = create_staff(
        admin_client,
        api_client,
        username="test.paper.officer",
        role="polling_officer",
        station_ids=[station["id"]],
    )
    supervisor = create_staff(
        admin_client,
        api_client,
        username="test.paper.supervisor",
        role="supervisor",
        station_ids=[station["id"]],
    )
    return PaperStation(
        f"/api/workspace/stations/{station['id']}",
        other["id"],
        schedule["revision"],
        officer,
        supervisor,
    )


def polling_command(revision: int) -> dict[str, str | int]:
    return {"revision": revision, "request_id": str(uuid4())}


def test_opening_approval_requires_current_submission_and_password_even_on_retry(
    api_client: TestClient, paper_station: PaperStation
) -> None:
    station = paper_station
    path = f"{station.path}/polling/opening"
    draft = expect_json(
        api_client.put(
            path,
            headers=station.officer,
            json={**polling_command(0), "event_at": OPENING},
        )
    )
    draft_approval = {**polling_command(draft["revision"]), "password": READY_PASSWORD}
    unsubmitted = api_client.post(
        f"{path}/approve", headers=station.supervisor, json=draft_approval
    )
    assert expect_json(unsubmitted, 409)["detail"]["code"] == "polling_state_conflict"

    corrected = expect_json(
        api_client.put(
            path,
            headers=station.officer,
            json={**polling_command(draft["revision"]), "event_at": "2026-11-22T08:29:00+08:00"},
        )
    )
    submitted = expect_json(
        api_client.post(
            f"{path}/submit",
            headers=station.officer,
            json=polling_command(corrected["revision"]),
        )
    )
    stale = api_client.post(f"{path}/approve", headers=station.supervisor, json=draft_approval)
    assert expect_json(stale, 409)["detail"]["code"] == "revision_conflict"

    approval = {**polling_command(submitted["revision"]), "password": READY_PASSWORD}
    wrong_password = {**approval, "password": "Incorrect-Fixture-Password-42!"}
    rejected = api_client.post(f"{path}/approve", headers=station.supervisor, json=wrong_password)
    assert expect_json(rejected, 401)["detail"]["code"] == "invalid_credentials"
    assert expect_json(api_client.get(f"{path}/history", headers=station.officer))["items"][-1] == (
        submitted
    )

    approved = expect_json(
        api_client.post(f"{path}/approve", headers=station.supervisor, json=approval)
    )
    assert (approved["status"], approved["revision"]) == ("approved", submitted["revision"])
    assert (
        expect_json(api_client.post(f"{path}/approve", headers=station.supervisor, json=approval))
        == approved
    )
    rejected_retry = api_client.post(
        f"{path}/approve", headers=station.supervisor, json=wrong_password
    )
    assert expect_json(rejected_retry, 401)["detail"]["code"] == "invalid_credentials"
    audit = expect_json(api_client.get(f"{station.path}/polling/audit", headers=station.officer))
    approvals = [event for event in audit["items"] if event["action"] == "polling_approve"]
    assert [event["details"]["record"] for event in approvals] == [approved]


def test_polling_records_and_approval_require_role_and_station_assignment(
    api_client: TestClient, admin_client: TestClient, paper_station: PaperStation
) -> None:
    station = paper_station
    path = f"{station.path}/polling/opening"
    draft = expect_json(
        api_client.put(
            path,
            headers=station.officer,
            json={**polling_command(0), "event_at": OPENING},
        )
    )
    submitted = expect_json(
        api_client.post(
            f"{path}/submit", headers=station.officer, json=polling_command(draft["revision"])
        )
    )
    other_supervisor = create_staff(
        admin_client,
        api_client,
        username="test.paper.other",
        role="supervisor",
        station_ids=[station.other_station_id],
    )
    admin_history = admin_client.get(f"{path}/history")
    assert expect_json(admin_history, 403)["detail"]["code"] == "operational_role_required"
    other_history = api_client.get(f"{path}/history", headers=other_supervisor)
    assert expect_json(other_history, 403)["detail"]["code"] == "station_access_denied"

    approval = {**polling_command(submitted["revision"]), "password": READY_PASSWORD}
    for headers, code in (
        (station.officer, "supervisor_required"),
        (other_supervisor, "station_access_denied"),
    ):
        response = api_client.post(f"{path}/approve", headers=headers, json=approval)
        assert expect_json(response, 403)["detail"]["code"] == code
    history = expect_json(api_client.get(f"{path}/history", headers=station.supervisor))
    assert history["items"] == [submitted]


def turnout_command(station: PaperStation, revision: int) -> dict[str, str | int]:
    return {
        "request_id": str(uuid4()),
        "expected_revision": revision,
        "expected_schedule_revision": station.schedule_revision,
    }


def submit_and_approve_turnout(
    client: TestClient, station: PaperStation, path: str, revision: int
) -> dict[str, Any]:
    expect_json(
        client.post(
            f"{path}/submit", headers=station.officer, json=turnout_command(station, revision)
        )
    )
    return expect_json(
        client.post(
            f"{path}/approve",
            headers=station.supervisor,
            json={**turnout_command(station, revision), "password": READY_PASSWORD},
        )
    )


def test_turnout_amendment_requires_review_against_new_predecessor_revision(
    api_client: TestClient, paper_station: PaperStation
) -> None:
    station = paper_station
    path = f"{station.path}/turnout"
    first_content = {
        "supplied_hourly": 100,
        "event_at": "2026-11-22T09:30:00+08:00",
        "source_context": "First period register report",
    }
    second_content = {
        "supplied_cumulative": 115,
        "event_at": "2026-11-22T10:30:00+08:00",
        "source_context": "Second period register report",
    }
    first = expect_json(
        api_client.put(
            f"{path}/1",
            headers=station.officer,
            json={**turnout_command(station, 0), **first_content},
        )
    )
    first_approved = submit_and_approve_turnout(api_client, station, f"{path}/1", first["revision"])
    second = expect_json(
        api_client.put(
            f"{path}/2",
            headers=station.officer,
            json={**turnout_command(station, 0), **second_content},
        )
    )
    second_approved = submit_and_approve_turnout(
        api_client, station, f"{path}/2", second["revision"]
    )
    assert (second_approved["derived_hourly"], second_approved["derived_cumulative_end"]) == (
        15,
        115,
    )

    amended_first = expect_json(
        api_client.post(
            f"{path}/1/amend",
            headers=station.officer,
            json={
                **turnout_command(station, first_approved["revision"]),
                **first_content,
                "supplied_hourly": 110,
                "amendment_reason": "Corrected first period after checking the register",
            },
        )
    )
    affected = expect_json(api_client.get(path, headers=station.officer))
    assert affected["approved_prefix"]["periods"] == 0
    assert affected["items"][1]["review_required"] is True
    assert affected["items"][1]["record"] == second_approved
    stale_approval = api_client.post(
        f"{path}/2/approve",
        headers=station.supervisor,
        json={**turnout_command(station, second_approved["revision"]), "password": READY_PASSWORD},
    )
    assert expect_json(stale_approval, 409)["detail"]["code"] == "turnout_review_required"

    reviewed_first = submit_and_approve_turnout(
        api_client, station, f"{path}/1", amended_first["revision"]
    )
    amended_second = expect_json(
        api_client.post(
            f"{path}/2/amend",
            headers=station.officer,
            json={
                **turnout_command(station, second_approved["revision"]),
                **second_content,
                "amendment_reason": "Reconcile with the corrected first period",
            },
        )
    )
    reviewed_second = submit_and_approve_turnout(
        api_client, station, f"{path}/2", amended_second["revision"]
    )
    assert (reviewed_second["derived_hourly"], reviewed_second["derived_cumulative_end"]) == (
        5,
        115,
    )
    assert reviewed_second["basis_predecessor_record_id"] == reviewed_first["id"]
    assert reviewed_second["basis_predecessor_revision"] == reviewed_first["revision"]
    latest = expect_json(api_client.get(path, headers=station.officer))
    assert latest["approved_prefix"]["periods"] == 2
    assert latest["approved_prefix"]["hourly_total"] == 115

    history = expect_json(api_client.get(f"{path}/2/history", headers=station.officer))
    assert history["items"] == [second_approved, reviewed_second]


def test_mixed_turnout_uses_only_ordinary_issues_after_fallback_activation(
    api_client: TestClient, paper_station: PaperStation
) -> None:
    station = paper_station
    path = station.path
    first_end = "2026-11-22T09:30:00+08:00"
    activation_at = "2026-11-22T09:50:00+08:00"
    second_end = "2026-11-22T10:30:00+08:00"
    first = expect_json(
        api_client.put(
            f"{path}/turnout/1",
            headers=station.officer,
            json={
                **turnout_command(station, 0),
                "supplied_hourly": 100,
                "event_at": first_end,
                "source_context": "First period register report",
            },
        )
    )
    submit_and_approve_turnout(api_client, station, f"{path}/turnout/1", first["revision"])

    series = "Series A"
    expect_json(
        api_client.post(
            f"{path}/stock",
            headers=station.officer,
            json={
                "request_id": str(uuid4()),
                "series": series,
                "event_at": OPENING,
                "ordinary_quantity": 200,
                "replacement_quantity": 3,
                "tendered_quantity": 2,
                "ranges": [
                    {"pool": "ordinary", "serial_from": "000001", "serial_to": "000200"},
                    {"pool": "replacement", "serial_from": "000201", "serial_to": "000203"},
                    {"pool": "tendered", "serial_from": "000204", "serial_to": "000205"},
                ],
            },
        ),
        201,
    )
    allocations = expect_json(api_client.get(f"{path}/allocations", headers=station.officer))
    counter_id = allocations["counters"][0]["id"]
    expect_json(
        api_client.post(
            f"{path}/allocations",
            headers=station.officer,
            json={
                "request_id": str(uuid4()),
                "counter_id": counter_id,
                "series": series,
                "serial_from": "000001",
                "serial_to": "000200",
                "event_at": OPENING,
            },
        ),
        201,
    )

    def snapshot(event_at: str, first_remaining: str) -> dict[str, Any]:
        return expect_json(
            api_client.post(
                f"{path}/stock-snapshots",
                headers=station.officer,
                json={
                    "request_id": str(uuid4()),
                    "event_at": event_at,
                    "counters": [
                        {
                            "counter_id": counter_id,
                            "ranges": [
                                {
                                    "series": series,
                                    "serial_from": first_remaining,
                                    "serial_to": "000200",
                                },
                            ],
                        },
                    ],
                },
            ),
            201,
        )

    baseline = snapshot(activation_at, "000111")
    activation = expect_json(
        api_client.post(
            f"{path}/fallback",
            headers=station.officer,
            json={
                "request_id": str(uuid4()),
                "expected_schedule_revision": station.schedule_revision,
                "kind": "activation",
                "event_at": activation_at,
                "stock_snapshot_id": baseline["id"],
                "stock_snapshot_revision": baseline["revision"],
                "cumulative_quantity": 110,
                "source_context": "Last register reading before fallback",
            },
        ),
        201,
    )
    activation_path = f"{path}/fallback/{activation['id']}"
    expect_json(
        api_client.post(
            f"{activation_path}/submit",
            headers=station.officer,
            json=turnout_command(station, activation["revision"]),
        )
    )
    assert (
        expect_json(api_client.get(f"{path}/fallback", headers=station.officer))["mode"] == "normal"
    )
    approved_activation = expect_json(
        api_client.post(
            f"{activation_path}/approve",
            headers=station.supervisor,
            json={**turnout_command(station, activation["revision"]), "password": READY_PASSWORD},
        )
    )
    assert (
        expect_json(api_client.get(f"{path}/fallback", headers=station.officer))["mode"]
        == "fallback"
    )

    endpoint = snapshot(second_end, "000116")
    special = expect_json(
        api_client.post(
            f"{path}/special-papers",
            headers=station.officer,
            json={
                "request_id": str(uuid4()),
                "event_at": second_end,
                "stock_snapshot_id": endpoint["id"],
                "stock_snapshot_revision": endpoint["revision"],
                "series": [
                    {
                        "series": series,
                        "replacement_issued_quantity": 3,
                        "spoilt_original_quantity": 3,
                        "tendered_issued_quantity": 2,
                        "retrieved_refused_quantity": 0,
                        "retrieved_found_quantity": 0,
                        "retrieved_held_quantity": 0,
                    },
                ],
            },
        ),
        201,
    )
    assert (special["replacement_issued_quantity"], special["tendered_issued_quantity"]) == (3, 2)

    mixed = expect_json(
        api_client.put(
            f"{path}/turnout/2",
            headers=station.officer,
            json={
                **turnout_command(station, 0),
                "mode": "fallback",
                "fallback_activation_id": approved_activation["id"],
                "fallback_activation_revision": approved_activation["revision"],
                "stock_snapshot_id": endpoint["id"],
                "stock_snapshot_revision": endpoint["revision"],
                "event_at": second_end,
                "source_context": "Register reading and ordinary stock after fallback",
            },
        )
    )
    approved = submit_and_approve_turnout(
        api_client, station, f"{path}/turnout/2", mixed["revision"]
    )
    assert approved["provenance"] == "mixed"
    assert approved["source_basis"]["ordinary_issued_since_activation_quantity"] == 5
    assert approved["derived_hourly"] == 15
    assert approved["derived_cumulative_end"] == 115
    turnout = expect_json(api_client.get(f"{path}/turnout", headers=station.officer))
    assert turnout["approved_prefix"]["cumulative_end"] == 115
