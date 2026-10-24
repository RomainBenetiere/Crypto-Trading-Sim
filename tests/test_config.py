from pathlib import Path

import pytest

from mcs.config import ConfigError, JournalMode, load_settings, parse_basket, parse_journal_mode


def test_parse_basket_normalises_and_keeps_order() -> None:
    assert parse_basket(" eth-eur, BTC-EUR ,PEPE-EUR,") == ("ETH-EUR", "BTC-EUR", "PEPE-EUR")


@pytest.mark.parametrize("raw", ["", "   ", "BTC-USD", "BTC_EUR", "BTC-EUR,BTC-EUR"])
def test_parse_basket_rejects_invalid(raw: str) -> None:
    with pytest.raises(ConfigError):
        parse_basket(raw)


def test_journal_mode_default_and_invalid() -> None:
    assert parse_journal_mode(None) is JournalMode.WAL
    assert parse_journal_mode("delete") is JournalMode.DELETE
    with pytest.raises(ConfigError):
        parse_journal_mode("MEMORY")


def test_load_settings_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "CRYPTO_BASKET",
        "DB_PATH",
        "SQLITE_JOURNAL_MODE",
        "BITVAVO_API_KEY",
        "BITVAVO_API_SECRET",
        "LOG_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "CRYPTO_BASKET=ETH-EUR,BTC-EUR\n"
        "BITVAVO_API_KEY=        # optionnel\n"
        "BITVAVO_API_SECRET=super-secret\n"
        "DB_PATH=data/x.sqlite\n"
        "SQLITE_JOURNAL_MODE=DELETE\n"
    )
    settings = load_settings(env, base_dir=tmp_path)
    assert settings.basket == ("ETH-EUR", "BTC-EUR")
    assert settings.db_path == tmp_path / "data/x.sqlite"
    assert settings.journal_mode is JournalMode.DELETE
    assert settings.api_key is None
    assert settings.secrets == ("super-secret",)
    assert "super-secret" not in repr(settings)


def test_environment_overrides_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("CRYPTO_BASKET=ETH-EUR\nDB_PATH=a.sqlite\n")
    monkeypatch.setenv("DB_PATH", "/data/mcs.sqlite")
    monkeypatch.setenv("CRYPTO_BASKET", "BTC-EUR")
    settings = load_settings(env, base_dir=tmp_path)
    assert settings.db_path == Path("/data/mcs.sqlite")
    assert settings.basket == ("BTC-EUR",)
