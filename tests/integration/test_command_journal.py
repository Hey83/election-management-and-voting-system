import os
import stat
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from election.desktop.client_command_journal import CommandJournal, JournalError

SERVER = "https://localhost:8479"
ACTOR = 19
STATION = 7
ALLOCATION = "/api/workspace/stations/7/allocations/11"


@pytest.mark.skipif(os.name != "posix", reason="Requires the POSIX C locale")
def test_command_journal_recovers_utf8_details_without_saving_credentials(tmp_path: Path) -> None:
    directory = tmp_path / "commands"
    journal = CommandJournal(directory)
    payload = {
        "request_id": str(uuid4()),
        "revision": 1,
        "reason": "核對紙本後更正序號",
        "password": "test-password-must-stay-in-memory",
    }
    command = journal.prepare(SERVER, ACTOR, STATION, "allocation", ALLOCATION, "PUT", payload)
    assert command["confirmation_required"] is True
    assert "password" not in command["payload"]

    # An implicit text encoding would fail to reload the Chinese reason in this process.
    child = """
import codecs
import locale
import sys
from pathlib import Path

from election.desktop.client_command_journal import CommandJournal

assert not sys.flags.utf8_mode and codecs.lookup(locale.getencoding()).name == 'ascii'
journal = CommandJournal(Path(sys.argv[1]))
server, actor, station = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
(pending,) = journal.commands(server, actor, station)
assert pending['status'] == 'pending'
journal.record_outcome(server, actor, pending['request_id'], 'committed', record={
    'id': 11, 'revision': 2, 'reason': pending['payload']['reason'],
    'authorization': 'test-token-must-stay-in-memory',
})
"""
    source = Path(__file__).resolve().parents[2] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8=0",
            "-c",
            child,
            str(directory),
            SERVER,
            str(ACTOR),
            str(STATION),
        ],
        env={**os.environ, "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONPATH": str(source)},
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    (settled,) = CommandJournal(directory).commands(SERVER, ACTOR, STATION)
    assert settled["request_id"] == payload["request_id"]
    assert settled["status"] == "committed"
    assert settled["receipt"] == {"record": {"id": 11, "revision": 2, "reason": payload["reason"]}}
    (saved,) = directory.glob("*.json")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    persisted = saved.read_text(encoding="utf-8")
    assert payload["reason"] in persisted
    assert payload["password"] not in persisted
    assert "test-token-must-stay-in-memory" not in persisted


def test_uncertain_command_can_retry_only_after_the_server_reports_not_found(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "commands"
    journal = CommandJournal(directory)
    request_id = str(uuid4())
    payload = {"request_id": request_id, "revision": 1, "serial_to": "000002"}
    journal.prepare(SERVER, ACTOR, STATION, "allocation", ALLOCATION, "PUT", payload)
    journal.record_outcome(SERVER, ACTOR, request_id, "uncertain", error="request_failed")

    restarted = CommandJournal(directory)
    with pytest.raises(JournalError, match="^recovery_outcome_check_required$"):
        restarted.prepare(SERVER, ACTOR, STATION, "allocation", ALLOCATION, "PUT", payload)

    restarted.record_outcome(SERVER, ACTOR, request_id, "uncertain", error="request_not_found")
    changed = {**payload, "serial_to": "000003"}
    with pytest.raises(JournalError, match="^recovery_command_conflict$"):
        restarted.prepare(SERVER, ACTOR, STATION, "allocation", ALLOCATION, "PUT", changed)
    retry = restarted.prepare(SERVER, ACTOR, STATION, "allocation", ALLOCATION, "PUT", payload)
    assert retry["retry_after_not_found"] is True
    assert retry["status"] == "uncertain"
    assert retry["payload"] == payload
    with pytest.raises(JournalError, match="^recovery_outcome_check_required$"):
        restarted.prepare(SERVER, ACTOR, STATION, "allocation", ALLOCATION, "PUT", payload)


def test_unresolved_count_prevents_a_new_command_for_the_same_round(tmp_path: Path) -> None:
    journal = CommandJournal(tmp_path / "commands")
    round_path = "/api/workspace/stations/7/counting-scopes/3/rounds/11"
    submitted = {"request_id": str(uuid4()), "revision": 1}
    journal.prepare(SERVER, ACTOR, STATION, "count", f"{round_path}/submit", "POST", submitted)

    amendment = {"request_id": str(uuid4()), "revision": 1}
    with pytest.raises(JournalError, match="^recovery_pending_command$"):
        journal.prepare(SERVER, ACTOR, STATION, "count", f"{round_path}/f1", "PUT", amendment)

    (pending,) = journal.commands(SERVER, ACTOR, STATION)
    assert pending["payload"] == submitted
    assert journal.commands(SERVER, ACTOR + 1, STATION) == []
    assert journal.commands("https://localhost:8480", ACTOR, STATION) == []
    assert journal.commands(SERVER, ACTOR, STATION + 1) == []


def test_rejected_election_command_keeps_its_original_payload(tmp_path: Path) -> None:
    directory = tmp_path / "commands"
    journal = CommandJournal(directory)
    path = "/api/workspace/elections/7/finalizations"
    payload = {
        "request_id": str(uuid4()),
        "expected_election_revision": 2,
        "basis_digest": "a" * 64,
    }
    journal.prepare_election(SERVER, ACTOR, 7, path, "POST", payload)
    journal.record_outcome(
        SERVER, ACTOR, payload["request_id"], "rejected", error="revision_conflict"
    )
    (saved,) = directory.glob("*.json")
    original = saved.read_bytes()

    restarted = CommandJournal(directory)
    with pytest.raises(JournalError, match="^recovery_command_conflict$"):
        restarted.prepare_election(
            SERVER, ACTOR, 7, path, "POST", {**payload, "expected_election_revision": 3}
        )

    assert saved.read_bytes() == original
    (rejected,) = restarted.election_commands(SERVER, ACTOR, 7)
    assert rejected["status"] == "rejected"
    assert rejected["payload"] == payload
    assert restarted.commands(SERVER, ACTOR, STATION) == []


def test_unreadable_existing_journal_blocks_a_new_submission(tmp_path: Path) -> None:
    directory = tmp_path / "commands"
    journal = CommandJournal(directory)
    journal.prepare(
        SERVER, ACTOR, STATION, "allocation", ALLOCATION, "PUT", {"request_id": str(uuid4())}
    )
    (saved,) = directory.glob("*.json")
    saved.write_bytes(b"\xff")

    with pytest.raises(JournalError, match="^recovery_journal_unavailable$"):
        CommandJournal(directory).prepare(
            SERVER, ACTOR, STATION, "allocation", ALLOCATION, "PUT", {"request_id": str(uuid4())}
        )
    assert list(directory.glob("*.json")) == [saved]
    assert saved.read_bytes() == b"\xff"
