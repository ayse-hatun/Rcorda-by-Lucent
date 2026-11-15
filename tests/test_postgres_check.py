from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg

from apps.api import config
from scripts import check_postgres


def test_missing_database_url_fails_without_connecting(monkeypatch, capsys):
    settings = config.Settings(
        app_env="test",
        api_host="127.0.0.1",
        api_port=8000,
        cors_allowed_origins=("http://localhost:3000",),
        database_url=None,
    )
    monkeypatch.setattr(check_postgres, "load_settings", lambda: settings)
    connect = Mock()
    monkeypatch.setattr(psycopg, "connect", connect)

    assert check_postgres.main() == 2
    connect.assert_not_called()
    assert "DATABASE_URL is required" in capsys.readouterr().err


def test_success_executes_select_one_and_closes_connection(monkeypatch, capsys):
    cursor = Mock()
    cursor.fetchone.return_value = (1,)

    @contextmanager
    def cursor_context():
        yield cursor

    connection = Mock()
    connection.cursor.return_value = cursor_context()
    connect = Mock(return_value=connection)
    settings = SimpleNamespace(
        require_postgres_url=lambda: "postgresql://localhost/recorda"
    )
    monkeypatch.setattr(check_postgres, "load_settings", lambda: settings)
    monkeypatch.setattr(psycopg, "connect", connect)

    assert check_postgres.main() == 0
    connect.assert_called_once_with(
        "postgresql://localhost/recorda", connect_timeout=5
    )
    cursor.execute.assert_called_once_with("SELECT 1")
    connection.close.assert_called_once()
    output = capsys.readouterr()
    assert "successful (SELECT 1)" in output.out
    assert "postgresql://" not in output.out + output.err


def test_connection_error_does_not_expose_database_url(monkeypatch, capsys):
    database_url = "postgresql://localhost/recorda"
    settings = SimpleNamespace(require_postgres_url=lambda: database_url)
    monkeypatch.setattr(check_postgres, "load_settings", lambda: settings)

    def fail_connect(*args, **kwargs):
        raise psycopg.OperationalError("driver internal details")

    monkeypatch.setattr(psycopg, "connect", fail_connect)

    assert check_postgres.main() == 1
    output = capsys.readouterr()
    assert "OperationalError" in output.err
    assert database_url not in output.out + output.err
    assert "driver internal details" not in output.out + output.err
