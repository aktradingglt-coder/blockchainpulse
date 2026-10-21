import pytest
from pydantic import ValidationError

from rd_core.settings import AppEnv, Settings


def test_defaults_are_dev_and_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    s = Settings()
    assert s.app_env is AppEnv.DEV
    assert not s.is_production
    assert s.config_dir.joinpath("product.yaml").is_file()


def test_feature_flags_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_FLAGS", '{"provider_fred": true}')
    assert Settings().feature_flags == {"provider_fred": True}
    monkeypatch.setenv("FEATURE_FLAGS", "")
    assert Settings().feature_flags == {}


@pytest.mark.parametrize("bad", ['{"a": "yes"}', "[1,2]"])
def test_feature_flags_reject_non_bool(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("FEATURE_FLAGS", bad)
    with pytest.raises(ValidationError):
        Settings()


def test_cors_origins_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.example.com, https://b.example.com")
    assert Settings().cors_allowed_origins == ["https://a.example.com", "https://b.example.com"]


def test_log_level_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert Settings().log_level == "DEBUG"
    monkeypatch.setenv("LOG_LEVEL", "loud")
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_are_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "sk-" + "ant-" + "q" * 30
    monkeypatch.setenv("ANTHROPIC_API_KEY", value)
    s = Settings()
    assert s.anthropic_api_key is not None
    assert value not in repr(s)
    assert value not in str(s.model_dump())


def test_log_format_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_FORMAT", "jsn")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("origins", ["*", "https://*.example.com"])
def test_cors_wildcards_rejected(monkeypatch: pytest.MonkeyPatch, origins: str) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", origins)
    with pytest.raises(ValidationError):
        Settings()


def test_cors_requires_https_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://app.example.com")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://app.example.com")
    assert Settings().cors_allowed_origins == ["https://app.example.com"]


@pytest.mark.parametrize("url", ["postgresql://u@h:5432/d", "postgresql+psycopg2://u@h:5432/d"])
def test_database_url_must_be_asyncpg(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(ValidationError):
        Settings()
