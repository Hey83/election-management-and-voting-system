import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from election.manage import bootstrap_account
from election.server.app import create_app
from election.server.db import make_engine


@pytest.fixture
def database_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    url = os.getenv("ELECTION_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Set ELECTION_TEST_DATABASE_URL to an empty, dedicated MySQL database.")
    try:
        database = make_url(url)
    except ArgumentError:
        pytest.fail("ELECTION_TEST_DATABASE_URL is not a valid database URL.")
    if (
        database.drivername != "mysql+pymysql"
        or not database.database
        or not database.database.endswith("_test")
    ):
        pytest.fail("Use a mysql+pymysql URL with a schema ending in _test.")

    monkeypatch.setenv("ELECTION_DATABASE_URL", url)
    engine = make_engine(url)
    try:
        # Keep the advisory lock while letting each inspection see the latest DDL.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            locked = connection.execute(
                text("SELECT GET_LOCK(:name, 0)"), {"name": database.database}
            ).scalar_one()
            if locked != 1:
                pytest.fail("The MySQL test schema is already in use.")
            schema = inspect(connection)
            if schema.get_table_names() or schema.get_view_names():
                pytest.fail("The MySQL test schema must start empty. Existing data was preserved.")

            try:
                command.upgrade(Config("alembic.ini"), "head")
                yield url
            finally:
                schema.clear_cache()
                quote = connection.dialect.identifier_preparer.quote
                connection.exec_driver_sql("SET SESSION FOREIGN_KEY_CHECKS = 0")
                try:
                    for table in schema.get_table_names():
                        connection.exec_driver_sql(f"DROP TABLE {quote(table)}")
                finally:
                    connection.exec_driver_sql("SET SESSION FOREIGN_KEY_CHECKS = 1")
    finally:
        engine.dispose()


@pytest.fixture
def api_now() -> datetime:
    return datetime(2026, 11, 22, 10, tzinfo=UTC)


@pytest.fixture
def make_client(database_url: str, api_now: datetime) -> Iterator[Callable[[], TestClient]]:
    with ExitStack() as stack:

        def new_client() -> TestClient:
            app = create_app(database_url, now=lambda: api_now)
            return stack.enter_context(TestClient(app, base_url="https://testserver"))

        yield new_client


@pytest.fixture
def api_client(make_client: Callable[[], TestClient]) -> TestClient:
    return make_client()


@pytest.fixture
def admin_login(database_url: str) -> dict[str, str]:
    username = "test_admin"
    password = "Initial-Test-Password-42!"
    bootstrap_account(database_url, username, "Test Administrator", password)
    return {"username": username, "password": password}


@pytest.fixture
def admin_client(make_client: Callable[[], TestClient], admin_login: dict[str, str]) -> TestClient:
    client = make_client()
    login = client.post("/api/auth/login", json=admin_login)
    assert login.status_code == 200
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    changed = client.post(
        "/api/auth/password",
        json={
            "current_password": admin_login["password"],
            "new_password": "Administrator-Ready-84!",
        },
    )
    assert changed.status_code == 200
    client.headers["Authorization"] = f"Bearer {changed.json()['token']}"
    return client
