"""Protect one current owner per paper-ballot serial across API clients."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from tests.api_setup import (
    create_constituency,
    create_election,
    create_staff,
    create_station,
    expect_json,
)

pytestmark = pytest.mark.mysql


@pytest.fixture
def received_stock(
    admin_client: TestClient, api_client: TestClient
) -> tuple[str, dict[str, str], list[int]]:
    election = create_election(admin_client)
    constituency = create_constituency(admin_client, election["id"])
    station = create_station(admin_client, election["id"], constituency["id"])
    counter_path = (
        f"/api/elections/{election['id']}/constituencies/{constituency['id']}"
        f"/stations/{station['id']}/counters"
    )
    counters = [
        expect_json(
            admin_client.post(
                counter_path,
                json={"code": code, "name": f"Counter {code}", "request_id": str(uuid4())},
            ),
            201,
        )["id"]
        for code in ("C1", "C2")
    ]
    headers = create_staff(
        admin_client,
        api_client,
        username=f"stock_{uuid4().hex[:12]}",
        role="polling_officer",
        station_ids=[station["id"]],
    )
    base = f"/api/workspace/stations/{station['id']}"
    expect_json(
        api_client.post(
            base + "/stock",
            headers=headers,
            json={
                "request_id": str(uuid4()),
                "series": "Ordinary A",
                "event_at": "2026-11-08T08:00:00+08:00",
                "ranges": [
                    {"pool": "ordinary", "serial_from": "000001", "serial_to": "000008"},
                ],
                "ordinary_quantity": 8,
                "replacement_quantity": 0,
                "tendered_quantity": 0,
            },
        ),
        201,
    )
    return base + "/allocations", headers, counters


def allocation(counter_id: int, first: str, last: str) -> dict[str, Any]:
    return {
        "request_id": str(uuid4()),
        "counter_id": counter_id,
        "series": "Ordinary A",
        "serial_from": first,
        "serial_to": last,
        "event_at": "2026-11-08T08:05:00+08:00",
    }


def test_same_allocation_request_has_one_effect(
    api_client: TestClient, received_stock: tuple[str, dict[str, str], list[int]]
) -> None:
    path, headers, counters = received_stock
    command = allocation(counters[0], "000001", "000003")
    created = expect_json(api_client.post(path, headers=headers, json=command), 201)

    assert expect_json(api_client.post(path, headers=headers, json=command)) == created
    changed_retry = api_client.post(path, headers=headers, json={**command, "serial_to": "000004"})
    assert expect_json(changed_retry, 409)["detail"]["code"] == "request_conflict"
    stock = expect_json(api_client.get(path, headers=headers))
    assert stock["items"] == [created]
    assert (created["serial_from"], created["serial_to"], created["quantity"]) == (
        "000001",
        "000003",
        3,
    )
    assert stock["availability"][0]["available_quantity"] == 5


def test_two_counters_cannot_receive_the_same_serial_concurrently(
    api_client: TestClient,
    make_client: Callable[[], TestClient],
    received_stock: tuple[str, dict[str, str], list[int]],
) -> None:
    path, headers, counters = received_stock
    first_client, second_client = make_client(), make_client()
    start = Barrier(2, timeout=10)

    def submit(client: TestClient, command: dict[str, Any]) -> Response:
        start.wait()
        return client.post(path, headers=headers, json=command)

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(submit, first_client, allocation(counters[0], "000001", "000003"))
        second = workers.submit(submit, second_client, allocation(counters[1], "000003", "000005"))
        accepted, rejected = sorted((first.result(), second.result()), key=lambda r: r.status_code)

    winner = expect_json(accepted, 201)
    assert expect_json(rejected, 409)["detail"]["code"] == "allocation_overlap"
    stock = expect_json(api_client.get(path, headers=headers))
    assert stock["items"] == [winner]
    assert stock["availability"][0]["allocated_quantity"] == 3
    assert stock["availability"][0]["available_quantity"] == 5
