"""Lecture du `.env` et validation du panier de paires."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from dotenv import dotenv_values

PAIR_PATTERN = re.compile(r"^[A-Z0-9]{1,20}-EUR$")
DEFAULT_DB_PATH = "data/mcs.sqlite"
DEFAULT_LOG_DIR = "logs"


class ConfigError(ValueError):
    """Configuration invalide (message en français, destiné à l'utilisateur)."""


class JournalMode(StrEnum):
    WAL = "WAL"
    DELETE = "DELETE"


@dataclass(frozen=True)
class Settings:
    basket: tuple[str, ...]
    db_path: Path
    journal_mode: JournalMode
    log_dir: Path
    # Secrets : jamais affichés (repr=False) ni journalisés.
    api_key: str | None = field(default=None, repr=False)
    api_secret: str | None = field(default=None, repr=False)

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(s for s in (self.api_key, self.api_secret) if s)


def parse_basket(raw: str | None) -> tuple[str, ...]:
    if not raw or not raw.strip():
        raise ConfigError("CRYPTO_BASKET est vide : indiquez au moins une paire (ex. BTC-EUR).")
    pairs = [p.strip().upper() for p in raw.split(",") if p.strip()]
    invalid = [p for p in pairs if not PAIR_PATTERN.match(p)]
    if invalid:
        raise ConfigError(
            f"Paires invalides dans CRYPTO_BASKET : {', '.join(invalid)} "
            "(format attendu : XXX-EUR)."
        )
    duplicates = sorted({p for p in pairs if pairs.count(p) > 1})
    if duplicates:
        raise ConfigError(f"Paires en double dans CRYPTO_BASKET : {', '.join(duplicates)}.")
    return tuple(pairs)


def parse_journal_mode(raw: str | None) -> JournalMode:
    value = (raw or JournalMode.WAL).strip().upper()
    try:
        return JournalMode(value)
    except ValueError as exc:
        raise ConfigError(
            f"SQLITE_JOURNAL_MODE invalide : {raw!r} (valeurs possibles : WAL, DELETE)."
        ) from exc


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def load_settings(env_file: Path | str | None = ".env", base_dir: Path | None = None) -> Settings:
    """Charge la configuration : variables d'environnement, complétées par le fichier `.env`.

    Les variables d'environnement l'emportent sur le fichier (utile en Docker).
    Les chemins relatifs sont résolus par rapport à `base_dir` (répertoire courant par défaut).
    """
    values: dict[str, str | None] = {}
    if env_file is not None and Path(env_file).is_file():
        values.update(dotenv_values(env_file))
    values.update({k: v for k, v in os.environ.items() if k in _KEYS})

    base = base_dir or Path.cwd()

    def resolve(raw: str | None, default: str) -> Path:
        path = Path(_blank_to_none(raw) or default).expanduser()
        return path if path.is_absolute() else base / path

    return Settings(
        basket=parse_basket(values.get("CRYPTO_BASKET")),
        db_path=resolve(values.get("DB_PATH"), DEFAULT_DB_PATH),
        journal_mode=parse_journal_mode(values.get("SQLITE_JOURNAL_MODE")),
        log_dir=resolve(values.get("LOG_DIR"), DEFAULT_LOG_DIR),
        api_key=_blank_to_none(values.get("BITVAVO_API_KEY")),
        api_secret=_blank_to_none(values.get("BITVAVO_API_SECRET")),
    )


_KEYS = frozenset(
    {
        "CRYPTO_BASKET",
        "DB_PATH",
        "SQLITE_JOURNAL_MODE",
        "LOG_DIR",
        "BITVAVO_API_KEY",
        "BITVAVO_API_SECRET",
    }
)
