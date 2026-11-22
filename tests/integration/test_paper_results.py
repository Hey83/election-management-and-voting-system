"""Regressions from the historical counting workflow and election-completion suite."""

import csv
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from election.demo.__main__ import import_demo, initial_state, scenario_data
from election.demo.client import DemoAPI, DemoError
from election.demo.counting import prepare_results
from tests.api_setup import expect_json

pytestmark = pytest.mark.mysql


class InProcessDemo(DemoAPI):
    """Send the real demo import requests through the test application's transport."""

    def __init__(self, client: TestClient, admin: TestClient, state_path: Path) -> None:
        scenario, digest = scenario_data(Path("demo/scenario.json"))
        self.state = initial_state("2026-11-21", scenario, digest)
        self.state["voting_method"] = "paper"
        self.state_path = state_path
        self.client = client
        self.token = admin.headers["Authorization"].removeprefix("Bearer ")
        self.clients = {}

    def _request(self, method: str, path: str, data: dict[str, Any] | None, token: str) -> bytes:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self.client.request(method, path, json=data, headers=headers)
        if response.is_error:
            raise DemoError(f"{method} {path}: HTTP {response.status_code}, {response.text}")
        return response.content


@pytest.fixture
def counting_demo(
    api_client: TestClient, admin_client: TestClient, tmp_path: Path
) -> InProcessDemo:
    demo = InProcessDemo(api_client, admin_client, tmp_path / "demo.json")
    import_demo(demo, "counting")
    return demo


def test_approved_count_requires_explicit_selection_before_finalization(
    counting_demo: InProcessDemo, api_client: TestClient
) -> None:
    context = counting_demo.state["context"]
    counting = counting_demo.as_role("counting_officer")
    supervisor = counting_demo.as_role("supervisor")
    scope = context["counting_scope"]
    draft = context["count_round"]
    round_path = (
        f"/api/workspace/stations/{context['station_id']}/counting-scopes/{scope['id']}"
        f"/rounds/{draft['id']}"
    )
    submitted = counting.request(
        "POST",
        round_path + "/submit",
        {
            "request_id": str(uuid4()),
            "expected_revision": draft["revision"],
            "scope_revision": scope["revision"],
        },
    )
    approval = {
        "request_id": str(uuid4()),
        "expected_revision": submitted["revision"],
        "scope_revision": scope["revision"],
        "password": supervisor.password,
    }
    supervisor_headers = {"Authorization": f"Bearer {supervisor.token}"}
    stale = api_client.post(
        round_path + "/approve",
        headers=supervisor_headers,
        json={**approval, "expected_revision": draft["revision"]},
    )
    assert expect_json(stale, 409)["detail"]["code"] == "revision_conflict"
    approved = supervisor.request("POST", round_path + "/approve", approval)
    assert approved["status"] == "approved"
    assert approved["reviewed_revision"] == submitted["revision"]
    assert approved["selected"] is False

    election_path = f"/api/workspace/elections/{context['election_id']}"
    review = supervisor.request("GET", election_path + "/completion")
    assert review["can_finalize"] is False
    assert [row["code"] for row in review["blockers"]] == ["selected_count_missing"]
    rejected = api_client.post(
        election_path + "/finalizations",
        headers=supervisor_headers,
        json={
            "request_id": str(uuid4()),
            "expected_election_revision": review["election"]["revision"],
            "basis_digest": review["basis_digest"],
            "password": supervisor.password,
        },
    )
    assert expect_json(rejected, 409)["detail"] == {
        "code": "election_not_ready",
        "blockers": review["blockers"],
    }
    history = supervisor.request("GET", election_path + "/completion/history")
    assert history["active_election_id"] == context["election_id"]
    assert history["events"] == []


def test_reopening_and_recount_preserve_the_finalized_result_and_selected_round(
    counting_demo: InProcessDemo,
) -> None:
    context = counting_demo.state["context"]
    prepare_results(counting_demo, context)
    supervisor = counting_demo.as_role("supervisor")
    election_id = context["election_id"]
    election_path = f"/api/workspace/elections/{election_id}"
    finalized = context["finalization"]
    saved_event = finalized["event"]
    assert finalized["active_election_id"] is None
    assert finalized["election"]["status"] == "finalized"
    (result,) = saved_event["outcome"]["results"]
    assert [row["quantity"] for row in result["candidate_totals"]] == [170, 125]
    assert result["winner_candidate_id"] == context["candidates"][0]["id"]
    (contribution,) = result["contributions"]
    selected = context["selected_count_round"]
    assert contribution["round_revision_id"] == selected["revision_id"]

    reopened = supervisor.request(
        "POST",
        election_path + "/reopenings",
        {
            "request_id": str(uuid4()),
            "expected_election_revision": finalized["election"]["revision"],
            "previous_finalization_id": saved_event["id"],
            "reason": "Review a later report while retaining the finalized result.",
            "password": supervisor.password,
        },
    )
    assert reopened["election"]["status"] == "active"
    assert reopened["event"]["previous_finalization_id"] == saved_event["id"]

    scope = context["counting_scope"]
    station_path = f"/api/workspace/stations/{context['station_id']}"
    recount = supervisor.request(
        "POST",
        f"{station_path}/counting-scopes/{scope['id']}/recounts",
        {
            "request_id": str(uuid4()),
            "scope_revision": scope["revision"],
            "expected_selection_revision": selected["selection"]["revision"],
            "source_round_id": selected["id"],
            "source_round_revision": selected["revision"],
            "event_at": "2026-11-22T18:00:00+08:00",
            "reason": "Retally the same received boxes.",
            "password": supervisor.password,
        },
    )
    assert recount["round"]["id"] != selected["id"]
    assert recount["round"]["round_number"] == 2
    current = supervisor.request("GET", election_path + "/completion")
    assert current["can_finalize"] is False
    assert "recount_pending" in {row["code"] for row in current["blockers"]}

    assert supervisor.request("GET", election_path + "/completion/history") == {
        "election": reopened["election"],
        "active_election_id": election_id,
        "events": [reopened["event"], saved_event],
    }
    export = counting_demo.state_path.with_name("finalized.csv")
    supervisor.download(
        f"{election_path}/completion/history/{saved_event['id']}/statement.csv?locale=en",
        export,
    )
    with export.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    assert ["Event ID", str(saved_event["id"])] in rows
    alex, jamie = context["candidates"]
    assert [str(alex["id"]), alex["name"], "170"] in rows
    assert [str(jamie["id"]), jamie["name"], "125"] in rows

    pdf_export = export.with_suffix(".pdf")
    supervisor.download(
        f"{election_path}/completion/history/{saved_event['id']}/statement.pdf?locale=tc",
        pdf_export,
    )
    pdf_text = "\n".join(page.extract_text() for page in PdfReader(pdf_export).pages)
    assert alex["name"] in pdf_text
    assert jamie["name"] in pdf_text
    assert {"170", "125"} <= set(pdf_text.split())
