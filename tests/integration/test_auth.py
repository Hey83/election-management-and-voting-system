from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from election.server.app import create_app
from tests.api_setup import READY_PASSWORD, create_staff, expect_json


@pytest.mark.mysql
def test_first_password_change_unlocks_workspace_and_revokes_earlier_sessions(
    api_client: TestClient, admin_login: dict[str, str]
) -> None:
    first = api_client.post("/api/auth/login", json=admin_login)
    other = api_client.post("/api/auth/login", json=admin_login)
    assert first.status_code == other.status_code == 200
    first_session = {"Authorization": f"Bearer {first.json()['token']}"}
    other_session = {"Authorization": f"Bearer {other.json()['token']}"}

    locked = api_client.get("/api/workspace", headers=first_session)
    assert locked.status_code == 403
    assert locked.json()["detail"]["code"] == "password_change_required"

    updated_login = {**admin_login, "password": "Personal-Test-Password-84!"}
    changed = api_client.post(
        "/api/auth/password",
        headers=first_session,
        json={
            "current_password": admin_login["password"],
            "new_password": updated_login["password"],
        },
    )
    assert changed.status_code == 200
    assert changed.json()["account"]["must_change_password"] is False

    for session in (first_session, other_session):
        assert api_client.get("/api/auth/me", headers=session).status_code == 401

    changed_session = {"Authorization": f"Bearer {changed.json()['token']}"}
    workspace = api_client.get("/api/workspace", headers=changed_session)
    assert workspace.status_code == 200
    assert workspace.json()["account"]["username"] == admin_login["username"]
    assert api_client.post("/api/auth/login", json=admin_login).status_code == 401
    assert api_client.post("/api/auth/login", json=updated_login).status_code == 200

    assert api_client.delete("/api/auth/session", headers=changed_session).status_code == 204
    assert api_client.get("/api/auth/me", headers=changed_session).status_code == 401


@pytest.mark.mysql
def test_session_has_no_idle_timeout_and_expires_at_24_hours(
    database_url: str, admin_login: dict[str, str]
) -> None:
    current_time = datetime(2026, 11, 22, 10, tzinfo=UTC)
    app = create_app(database_url, now=lambda: current_time)
    with TestClient(app, base_url="https://testserver") as client:
        session = expect_json(client.post("/api/auth/login", json=admin_login))
        assert session["expires_at"] == "2026-11-23T10:00:00Z"
        client.headers["Authorization"] = f"Bearer {session['token']}"

        current_time += timedelta(hours=23, minutes=59, seconds=59)
        assert client.get("/api/auth/me").status_code == 200

        current_time += timedelta(seconds=1)
        expired = client.get("/api/auth/me")
        assert expect_json(expired, 401)["detail"]["code"] == "session_expired"


@pytest.mark.mysql
def test_only_current_revision_can_reset_staff_password(
    admin_client: TestClient, api_client: TestClient
) -> None:
    username = "reset_officer"
    headers = create_staff(
        admin_client, api_client, username=username, role="polling_officer", station_ids=[]
    )
    staff = expect_json(api_client.get("/api/auth/me", headers=headers))
    path = f"/api/accounts/{staff['id']}"
    original = expect_json(admin_client.get(path))
    updated = expect_json(
        admin_client.put(
            path,
            json={
                "display_name": "Updated officer",
                "role": original["role"],
                "enabled": original["enabled"],
                "station_ids": [],
                "revision": original["revision"],
            },
        )
    )
    login = {"username": username, "password": READY_PASSWORD}
    current = expect_json(api_client.post("/api/auth/login", json=login))
    active_headers = {"Authorization": f"Bearer {current['token']}"}
    reset = {"temporary_password": "Reset-Test-Password-64!", "revision": original["revision"]}

    rejected = admin_client.post(path + "/password-reset", json=reset)
    assert expect_json(rejected, 409)["detail"]["code"] == "revision_conflict"
    assert api_client.get("/api/auth/me", headers=active_headers).status_code == 200
    assert expect_json(admin_client.get(path)) == updated

    accepted = expect_json(
        admin_client.post(path + "/password-reset", json={**reset, "revision": updated["revision"]})
    )
    assert accepted["must_change_password"] is True
    assert api_client.get("/api/auth/me", headers=active_headers).status_code == 401
    assert api_client.post("/api/auth/login", json=login).status_code == 401
    renewed = expect_json(
        api_client.post("/api/auth/login", json={**login, "password": reset["temporary_password"]})
    )
    assert renewed["account"]["must_change_password"] is True
