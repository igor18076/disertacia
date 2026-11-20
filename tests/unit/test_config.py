import pytest
from pydantic import ValidationError

from coursegen.core.config import Settings


def test_database_password_with_url_characters(monkeypatch):
    password = "%p@ss/word?#"
    monkeypatch.setenv("POSTGRES_PASSWORD", password)
    monkeypatch.setenv("POSTGRES_HOST", "database")
    settings = Settings(_env_file=None)

    assert settings.database_url.host == "database"
    assert settings.database_url.password == password
    assert password not in repr(settings)
    assert password not in str(settings.database_url)


def test_missing_password_is_rejected(monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("timeout", ["0", "31"])
def test_probe_timeout_has_bounds(monkeypatch, timeout):
    monkeypatch.setenv("POSTGRES_PASSWORD", "unit-test-password")
    monkeypatch.setenv("PROBE_TIMEOUT_SECONDS", timeout)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
