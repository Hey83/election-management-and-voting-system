"""Create the minimum election data through the authenticated API."""

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from httpx2 import Response

INITIAL_PASSWORD = "Initial-Fixture-Password-42!"
READY_PASSWORD = "Ready-Fixture-Password-84!"


def expect_json(response: Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    return response.json()


def create_election(
    admin: TestClient, *, voting_method: str = "paper", name: str = "Test election"
) -> dict[str, Any]:
    return expect_json(
        admin.post(
            "/api/elections",
            json={"name": name, "voting_method": voting_method, "request_id": str(uuid4())},
        ),
        201,
    )


def create_constituency(admin: TestClient, election_id: int, *, code: str = "N") -> dict[str, Any]:
    return expect_json(
        admin.post(
            f"/api/elections/{election_id}/constituencies",
            json={"code": code, "name": f"Constituency {code}", "request_id": str(uuid4())},
        ),
        201,
    )


def create_station(
    admin: TestClient, election_id: int, constituency_id: int, *, code: str = "N1"
) -> dict[str, Any]:
    return expect_json(
        admin.post(
            f"/api/elections/{election_id}/constituencies/{constituency_id}/stations",
            json={"code": code, "name": f"Polling station {code}", "request_id": str(uuid4())},
        ),
        201,
    )


def create_staff(
    admin: TestClient,
    api: TestClient,
    *,
    username: str,
    role: str,
    station_ids: list[int],
) -> dict[str, str]:
    expect_json(
        admin.post(
            "/api/accounts",
            json={
                "username": username,
                "display_name": username,
                "role": role,
                "station_ids": station_ids,
                "temporary_password": INITIAL_PASSWORD,
            },
        ),
        201,
    )
    first = expect_json(
        api.post("/api/auth/login", json={"username": username, "password": INITIAL_PASSWORD})
    )
    ready = expect_json(
        api.post(
            "/api/auth/password",
            headers={"Authorization": f"Bearer {first['token']}"},
            json={"current_password": INITIAL_PASSWORD, "new_password": READY_PASSWORD},
        )
    )
    return {"Authorization": f"Bearer {ready['token']}"}
