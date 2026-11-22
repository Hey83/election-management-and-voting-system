"""Enforce the single active election across both voting methods."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from tests.api_setup import create_constituency, create_election, create_station, expect_json


def activation_request(admin: TestClient, voting_method: str) -> tuple[str, dict[str, int]]:
    election = create_election(admin, voting_method=voting_method, name=f"{voting_method} election")
    constituency = create_constituency(admin, election["id"])
    station = create_station(admin, election["id"], constituency["id"])
    path = f"/api/elections/{election['id']}"
    constituency_path = f"{path}/constituencies/{constituency['id']}"
    expect_json(
        admin.post(
            constituency_path + "/candidates",
            json={"name": "Alex Chan", "ballot_number": 1, "request_id": str(uuid4())},
        ),
        201,
    )
    if voting_method == "paper":
        expect_json(
            admin.post(
                f"{constituency_path}/stations/{station['id']}/counters",
                json={"code": "C1", "name": "Counter 1", "request_id": str(uuid4())},
            ),
            201,
        )
    schedule = expect_json(
        admin.put(
            path + "/schedule",
            json={
                "election_date": "2026-11-22",
                "planned_opening_at": "2026-11-22T08:30:00+08:00",
                "planned_closing_at": "2026-11-22T17:00:00+08:00",
                "reporting_interval_minutes": 60,
                "revision": 0,
            },
        )
    )
    return path, {
        "election_revision": election["revision"],
        "schedule_revision": schedule["revision"],
    }


@pytest.mark.mysql
def test_competing_activations_leave_one_shared_active_election(
    admin_client: TestClient, api_client: TestClient
) -> None:
    paper_path, paper_command = activation_request(admin_client, "paper")
    electronic_path, electronic_command = activation_request(admin_client, "electronic")
    headers = {"Authorization": admin_client.headers["Authorization"]}
    start = Barrier(2, timeout=10)

    def activate(client: TestClient, path: str, command: dict[str, int]) -> tuple[str, Response]:
        start.wait()
        return path, client.post(path + "/activate", headers=headers, json=command)

    with ThreadPoolExecutor(max_workers=2) as workers:
        paper = workers.submit(activate, admin_client, paper_path, paper_command)
        electronic = workers.submit(activate, api_client, electronic_path, electronic_command)
        (_, accepted), (loser_path, rejected) = sorted(
            (paper.result(), electronic.result()), key=lambda attempt: attempt[1].status_code
        )

    winner = expect_json(accepted)
    assert expect_json(rejected, 409)["detail"]["code"] == "active_election_conflict"
    assert winner["election"]["status"] == "active"
    assert expect_json(admin_client.get("/api/active-election")) == winner
    assert expect_json(admin_client.get(loser_path))["status"] == "draft"
