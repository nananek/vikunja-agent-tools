import pytest

from vikunja_agent_tools.config import ConfigError, Settings, load_settings


def test_load_settings_reads_env(monkeypatch):
    monkeypatch.delenv("VIKUNJA_PROJECT_ID", raising=False)
    monkeypatch.setenv("VIKUNJA_BASE_URL", "https://vikunja.example.com/")
    monkeypatch.setenv("VIKUNJA_API_TOKEN", "super-secret-token")

    settings = load_settings()

    assert settings.base_url == "https://vikunja.example.com"
    assert settings.vikunja_api_token.get_secret_value() == "super-secret-token"
    assert settings.vikunja_timeout_seconds == 15
    assert settings.vikunja_verify_tls is True
    assert settings.vikunja_agent_label_prefix == "agent-"
    assert settings.vikunja_status_label_prefix == "status-"


def test_load_settings_missing_token_raises_config_error(monkeypatch):
    monkeypatch.setenv("VIKUNJA_BASE_URL", "https://vikunja.example.com")
    monkeypatch.delenv("VIKUNJA_API_TOKEN", raising=False)

    with pytest.raises(ConfigError) as exc_info:
        load_settings()

    message = str(exc_info.value)
    assert "vikunja_api_token" in message
    assert ".env" in message


def test_secret_str_does_not_leak_token_in_repr(monkeypatch):
    monkeypatch.setenv("VIKUNJA_BASE_URL", "https://vikunja.example.com")
    monkeypatch.setenv("VIKUNJA_API_TOKEN", "super-secret-token")

    settings = load_settings()

    assert "super-secret-token" not in repr(settings)
    assert "super-secret-token" not in str(settings)
    assert "super-secret-token" not in repr(settings.vikunja_api_token)
