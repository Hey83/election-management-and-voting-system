"""Drive use cases across the desktop, TLS API, and disposable MySQL database."""

import csv
import socket
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from statistics import median
from typing import Any
from uuid import uuid4

import pytest
import uvicorn
from PySide6.QtCore import QCoreApplication, QEvent

from election.demo.__main__ import EPOLLING_TERMINAL, import_demo, initial_state, scenario_data
from election.demo.client import DemoAPI
from election.deployment.configuration import prepare
from election.desktop.controller import DesktopController
from election.server.app import create_app

pytestmark = pytest.mark.mysql


@pytest.fixture(scope="session")
def qt_app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def wait_until(qt_app: QCoreApplication, condition: Callable[[], bool], outcome: str) -> None:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if condition():
            return
        time.sleep(0.01)
    pytest.fail(f"Timed out waiting for {outcome}.")


@pytest.fixture
def https_service(
    database_url: str, api_now: datetime, qt_app: QCoreApplication, tmp_path: Path
) -> Iterator[tuple[str, Path]]:
    """Use an ephemeral loopback listener and the normal deployment certificate chain."""
    host = "127.0.0.1"
    deployment = tmp_path / "deployment"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((host, 0))
        port = listener.getsockname()[1]
        prepare(deployment, host, host, port, "election-system-test")
        tls = deployment / "tls"
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(database_url, now=lambda: api_now),
                host=host,
                port=port,
                ssl_certfile=tls / "server.pem",
                ssl_keyfile=tls / "server-key.pem",
                ws="none",
                lifespan="on",
                timeout_graceful_shutdown=2,
                access_log=False,
                log_level="warning",
            )
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            wait_until(qt_app, lambda: server.started or not thread.is_alive(), "HTTPS startup")
            assert server.started, "The HTTPS server stopped during startup."
            yield f"https://{host}:{port}", tls / "ca.pem"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "The temporary HTTPS server did not shut down."


@pytest.fixture
def import_system_demo(
    admin_login: dict[str, str],
    https_service: tuple[str, Path],
    api_now: datetime,
    tmp_path: Path,
) -> Iterator[Callable[[str, str], DemoAPI]]:
    server, ca = https_service
    source = Path(__file__).resolve().parents[2] / "demo" / "scenario.json"
    scenario, digest = scenario_data(source)
    past_day = (min(api_now, datetime.now(UTC)).date() - timedelta(days=1)).isoformat()

    with ExitStack() as stack:

        def import_stage(voting_method: str, stage: str) -> DemoAPI:
            state = initial_state(past_day, scenario, digest)
            state["voting_method"] = voting_method
            demo = DemoAPI(server, ca, tmp_path / "demo" / "state.json", state)
            stack.callback(demo.close)
            demo.login(admin_login["username"], admin_login["password"])
            ready_password = "Ready-System-Password-84!"
            session = demo.request(
                "POST",
                "/api/auth/password",
                {"current_password": admin_login["password"], "new_password": ready_password},
            )
            demo.token, demo.password = session["token"], ready_password
            import_demo(demo, stage)
            return demo

        yield import_stage


@pytest.fixture
def sign_in_desktop(
    https_service: tuple[str, Path],
    qt_app: QCoreApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[dict[str, Any]], DesktopController]]:
    server, ca = https_service

    with ExitStack() as stack:

        def sign_in(account: dict[str, Any]) -> DesktopController:
            username = account["username"]
            with monkeypatch.context() as environment:
                environment.setenv("ELECTION_CLIENT_STATE_DIR", str(tmp_path / username))
                desktop = DesktopController(server, str(ca))
            stack.callback(desktop.deleteLater)
            stack.callback(desktop.shutdown)
            desktop.signIn(server, username, account["password"])
            wait_until(
                qt_app,
                lambda: bool(desktop.workspace.state["selected"]) or bool(desktop.state["error"]),
                f"{username}'s assigned station",
            )
            assert desktop.state["error"] == ""
            assert desktop.state["phase"] == "workspace"
            return desktop

        yield sign_in

    qt_app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_successive_electors_update_the_supervisors_live_tally(
    import_system_demo: Callable[[str, str], DemoAPI],
    sign_in_desktop: Callable[[dict[str, Any]], DesktopController],
    qt_app: QCoreApplication,
) -> None:
    demo = import_system_demo("electronic", "polling")
    accounts = demo.state["accounts"]
    station_id = demo.state["context"]["station_id"]
    terminal = sign_in_desktop(accounts[EPOLLING_TERMINAL])
    supervisor = sign_in_desktop(accounts["supervisor"])
    assert terminal.workspace.state["selected"]["station"]["id"] == station_id
    assert supervisor.workspace.state["selected"]["station"]["id"] == station_id

    voting = terminal.epolling
    results = supervisor.epollingResults
    results.setViewing(True)
    wait_until(qt_app, lambda: results.state["tally"].get("total") == "0", "the baseline tally")
    assert [row["votes"] for row in results.state["tally"]["candidates"]] == ["0", "0"]

    voting.start(station_id)
    wait_until(qt_app, lambda: voting.state["stage"] == "select", "the first voting session")
    first, second = voting.state["candidates"]
    assert (first["number"], second["number"]) == (1, 2)
    voting.cast(first["id"])
    wait_until(qt_app, lambda: voting.state["stage"] == "recorded", "the first vote receipt")
    assert voting.state["candidates"] == []
    # Let the Supervisor's normal background refresh deliver the new tally.
    wait_until(qt_app, lambda: results.state["tally"].get("total") == "1", "a refreshed live tally")
    assert [row["votes"] for row in results.state["tally"]["candidates"]] == ["1", "0"]

    voting.done()
    voting.startNext()
    wait_until(qt_app, lambda: voting.state["stage"] == "select", "the next voting session")
    voting.cast(second["id"])
    wait_until(qt_app, lambda: voting.state["stage"] == "recorded", "the second vote receipt")
    results.refresh()
    wait_until(qt_app, lambda: results.state["tally"].get("total") == "2", "both recorded votes")
    assert [row["votes"] for row in results.state["tally"]["candidates"]] == ["1", "1"]
    assert terminal.state["account"]["username"] == accounts[EPOLLING_TERMINAL]["username"]


def test_paper_workflow_finalizes_the_count_and_exports_results(
    import_system_demo: Callable[[str, str], DemoAPI],
) -> None:
    # The real demo client prepares, approves, counts, and exports through trusted HTTPS.
    demo = import_system_demo("paper", "results")
    context = demo.state["context"]
    finalization = context["finalization"]
    assert finalization["election"]["status"] == "finalized"
    assert finalization["active_election_id"] is None

    directory = demo.state_path.parent
    with (directory / "demo-results.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    first, second = context["candidates"]
    assert [str(first["id"]), "1", "Alex Chan / 陳明", "170"] in rows
    assert [str(second["id"]), "2", "Jamie Wong / 黃欣", "125"] in rows

    pdf = (directory / "demo-results.pdf").read_bytes()
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")


def test_live_tally_is_consistent_for_concurrent_supervisor_clients(
    import_system_demo: Callable[[str, str], DemoAPI],
    https_service: tuple[str, Path],
    tmp_path: Path,
    record_testsuite_property: Callable[[str, Any], None],
) -> None:
    demo = import_system_demo("electronic", "polling")
    context = demo.state["context"]
    station_path = f"/api/workspace/stations/{context['station_id']}/e-polling"
    terminal = demo.as_role(EPOLLING_TERMINAL)
    reference = {"session_id": str(uuid4())}
    terminal.request("POST", f"{station_path}/sessions", reference)
    vote = {**reference, "candidate_id": context["candidates"][0]["id"]}
    assert terminal.request("PUT", f"{station_path}/sessions/vote", vote) == {"status": "recorded"}

    def observe(barrier: threading.Barrier, client: DemoAPI) -> tuple[dict[str, Any], float]:
        barrier.wait(timeout=15)
        start = time.perf_counter()
        tally = client.request("GET", f"{station_path}/results")
        return tally, (time.perf_counter() - start) * 1000

    server, ca = https_service
    supervisor = demo.state["accounts"]["supervisor"]
    with ExitStack() as stack:
        clients = []
        for index in range(20):
            client = DemoAPI(server, ca, tmp_path / f"observer-{index}.json", {})
            stack.callback(client.close)
            client.login(supervisor["username"], supervisor["password"])
            clients.append(client)

        for count in (1, 20):
            observe_together = partial(observe, threading.Barrier(count))
            with ThreadPoolExecutor(max_workers=count) as pool:
                results = list(pool.map(observe_together, clients[:count]))
            for tally, _ in results:
                assert tally["context"]["station"]["id"] == context["station_id"]
                assert tally["total_votes"] == 1
                assert [(row["number"], row["votes"]) for row in tally["candidates"]] == [
                    (1, 1),
                    (2, 0),
                ]
            durations = [duration for _, duration in results]
            record_testsuite_property(
                f"tally_{count}_clients_median_ms", round(median(durations), 2)
            )
            record_testsuite_property(f"tally_{count}_clients_max_ms", round(max(durations), 2))
