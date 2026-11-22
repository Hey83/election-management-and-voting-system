import json
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject

from election.desktop.configuration import ResultHandler
from election.desktop.epolling import EpollingController
from election.desktop.epolling_recovery import VotingSessionJournal


def test_voting_terminal_recovers_lost_acknowledgment_without_replaying_choice(
    tmp_path: Path,
) -> None:
    server = "https://localhost:8479"
    station = 7
    endpoint = f"/api/workspace/stations/{station}/e-polling/sessions"
    directory = tmp_path / "terminal"
    journal = VotingSessionJournal(directory)
    account = {"id": 2, "role": "polling_officer", "must_change_password": False}
    replies: deque[dict[str, Any] | str] = deque(
        [
            {
                "status": "pending",
                "context": {
                    "election": {"name": "Test election"},
                    "constituency": {"name": "North"},
                    "station": {"id": station, "name": "North Hall"},
                },
                "candidates": [{"id": 13, "number": 1, "name": "Alex Chan"}],
            },
            "request_failed",  # The vote acknowledgment was lost after submission.
            "request_failed",  # The network is still unavailable for the first status check.
            {"status": "recorded"},
        ]
    )
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def transport(
        path: str,
        method: str,
        payload: dict[str, Any] | None,
        loaded: ResultHandler,
        rejected: Callable[[str], None],
    ) -> None:
        calls.append((method, path, payload))
        reply = replies.popleft()
        if isinstance(reply, str):
            rejected(reply)
        else:
            loaded(reply)

    parent = QObject()
    terminal = EpollingController(transport, journal, lambda: None, parent)
    restarted = EpollingController(transport, VotingSessionJournal(directory), lambda: None, parent)
    try:
        terminal.set_account(server, account)
        terminal.start(station)
        assert terminal.state["stage"] == "select"
        session = journal.read()
        assert session is not None
        reference = {"session_id": session.session_id}

        terminal.cast(13)
        assert terminal.state["stage"] == "checking"
        assert terminal.state["retry"] is False
        terminal.retryCast()
        terminal.startNext()
        assert calls == [
            ("POST", endpoint, reference),
            ("PUT", f"{endpoint}/vote", {**reference, "candidate_id": 13}),
            ("POST", f"{endpoint}/status", reference),
        ]
        saved = json.loads((directory / "e-polling.pending").read_text(encoding="utf-8"))
        assert saved == {**reference, "server_url": server, "station_id": station}

        terminal.reset()
        restarted.set_account(server, {**account, "id": 3})
        assert calls[3:] == [("POST", f"{endpoint}/status", reference)]
        assert restarted.state["stage"] == "recorded"
        assert restarted.state["candidates"] == []
        assert journal.read() is None
    finally:
        terminal.reset()
        restarted.reset()
