"""Lecture du `.env` et validation du panier de paires."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from dotenv import dotenv_values

PAIR_PATTERN = re.compile(r"^[A-Z0-9]{1,20}-EUR$")
DEFAULT_DB_PATH = "data/mcs.sqlite"
DEFAULT_LOG_DIR = "logs"
# Grille Bitvavo, palier < 100 000 €/mois (à confirmer par l'utilisateur, SPEC §5).
DEFAULT_FEE_MAKER = Decimal("0.0015")
DEFAULT_FEE_TAKER = Decimal("0.0025")


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
    fee_maker: Decimal = DEFAULT_FEE_MAKER
    fee_taker: Decimal = DEFAULT_FEE_TAKER
    optimizer_workers: int = 4
    # Mode réel (palier 3) : double verrou, activé seulement avec PRODUCTION_LIVE=1 et un
    # plafond de capital ; `operator_id` identifie le moteur auprès de Bitvavo.
    production_live: bool = False
    production_max_capital: Decimal | None = None
    operator_id: int = 1
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


def parse_fee(raw: str | None, default: Decimal, name: str) -> Decimal:
    """Taux de frais en fraction (0.0025 = 0,25 %)."""
    value = _blank_to_none(raw)
    if value is None:
        return default
    try:
        rate = Decimal(value)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} invalide : {raw!r} (ex. 0.0025 pour 0,25 %).") from exc
    if not Decimal(0) <= rate < Decimal("0.01"):
        raise ConfigError(f"{name} hors limites : {rate} (fraction attendue, ex. 0.0025).")
    return rate


def parse_workers(raw: str | None) -> int:
    value = _blank_to_none(raw)
    if value is None:
        return 4
    if not value.isdigit() or not 1 <= int(value) <= 64:
        raise ConfigError(f"OPTIMIZER_WORKERS invalide : {raw!r} (entier entre 1 et 64).")
    return int(value)


def parse_flag(raw: str | None, name: str) -> bool:
    value = (_blank_to_none(raw) or "0").lower()
    if value not in ("0", "1", "true", "false"):
        raise ConfigError(f"{name} invalide : {raw!r} (0 ou 1).")
    return value in ("1", "true")


def parse_max_capital(raw: str | None) -> Decimal | None:
    value = _blank_to_none(raw)
    if value is None:
        return None
    try:
        capital = Decimal(value)
    except InvalidOperation as exc:
        raise ConfigError(f"PRODUCTION_MAX_CAPITAL invalide : {raw!r} (ex. 300).") from exc
    if not capital.is_finite() or capital <= 0:
        raise ConfigError(f"PRODUCTION_MAX_CAPITAL doit être positif : {raw!r}.")
    return capital


def parse_operator_id(raw: str | None) -> int:
    value = _blank_to_none(raw)
    if value is None:
        return 1
    if not value.isdigit() or not 1 <= int(value) < 2**63:
        raise ConfigError(f"BITVAVO_OPERATOR_ID invalide : {raw!r} (entier positif).")
    return int(value)


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
        fee_maker=parse_fee(values.get("FEE_MAKER"), DEFAULT_FEE_MAKER, "FEE_MAKER"),
        fee_taker=parse_fee(values.get("FEE_TAKER"), DEFAULT_FEE_TAKER, "FEE_TAKER"),
        optimizer_workers=parse_workers(values.get("OPTIMIZER_WORKERS")),
        production_live=parse_flag(values.get("PRODUCTION_LIVE"), "PRODUCTION_LIVE"),
        production_max_capital=parse_max_capital(values.get("PRODUCTION_MAX_CAPITAL")),
        operator_id=parse_operator_id(values.get("BITVAVO_OPERATOR_ID")),
        api_key=_blank_to_none(values.get("BITVAVO_API_KEY")),
        api_secret=_blank_to_none(values.get("BITVAVO_API_SECRET")),
    )


_KEYS = frozenset(
    {
        "CRYPTO_BASKET",
        "DB_PATH",
        "SQLITE_JOURNAL_MODE",
        "LOG_DIR",
        "FEE_MAKER",
        "FEE_TAKER",
        "OPTIMIZER_WORKERS",
        "PRODUCTION_LIVE",
        "PRODUCTION_MAX_CAPITAL",
        "BITVAVO_OPERATOR_ID",
        "BITVAVO_API_KEY",
        "BITVAVO_API_SECRET",
    }
)
