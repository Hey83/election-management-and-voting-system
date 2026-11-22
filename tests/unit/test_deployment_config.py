from pathlib import Path

import pytest
import yaml

from election.deployment.quick_config import QuickConfigError, load_quick_config


def test_compose_password_keeps_literal_dollar_characters(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(
        """x-election:
  accounts:
    - username: ' test.admin '
      password: 'Fixture$$Literal$$Password42!'
services:
  api:
    image: election-fixture
""",
        encoding="utf-8",
    )

    settings = load_quick_config(path)
    account = settings.accounts[0]
    assert account.username == account.display_name == "test.admin"
    assert account.password.get_secret_value() == "Fixture$Literal$Password42!"
    assert "Fixture" not in repr(settings)
    assert "Fixture" not in settings.model_dump_json()


def test_initial_accounts_reject_duplicate_normalized_usernames(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(
        """x-election:
  accounts:
    - username: ' Local.Admin '
      password: Fixture-First-Password42!
    - username: local.admin
      password: Fixture-Second-Password42!
""",
        encoding="utf-8",
    )

    with pytest.raises(QuickConfigError, match="^invalid_quick_configuration$"):
        load_quick_config(path)


@pytest.mark.parametrize(
    "account_changes",
    [
        pytest.param({"role": "unsupported-role"}, id="unknown-role"),
        pytest.param({"password": "${PRIVATE_FIXTURE_PASSWORD}"}, id="unescaped-dollar"),
        pytest.param({"password": "CHANGE_ME_Privately"}, id="placeholder"),
    ],
)
def test_invalid_initial_account_errors_do_not_include_config_values(
    tmp_path: Path, account_changes: dict[str, str]
) -> None:
    path = tmp_path / "config.yml"
    account = {"username": "private-marker", "password": "Fixture-Password-42!", **account_changes}
    path.write_text(yaml.safe_dump({"x-election": {"accounts": [account]}}), encoding="utf-8")

    with pytest.raises(QuickConfigError) as failure:
        load_quick_config(path)

    assert str(failure.value) == "invalid_quick_configuration"
    for value in account.values():
        assert value not in repr(failure.value)
