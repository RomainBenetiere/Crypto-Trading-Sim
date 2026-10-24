"""Journalisation console + fichier tournant, avec masquage des secrets."""

from __future__ import annotations

import logging
import logging.handlers
from collections.abc import Iterable
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s : %(message)s"


class SecretRedactingFilter(logging.Filter):
    """Remplace toute occurrence des secrets par `***` dans les messages journalisés."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "***")
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def configure_logging(
    log_dir: Path, name: str, secrets: Iterable[str] = (), level: int = logging.INFO
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    redactor = SecretRedactingFilter(secrets)
    formatter = logging.Formatter(FORMAT)

    console = logging.StreamHandler()
    file = logging.handlers.RotatingFileHandler(
        log_dir / f"{name}.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    root = logging.getLogger()
    root.setLevel(level)
    for handler in (console, file):
        handler.setFormatter(formatter)
        handler.addFilter(redactor)
        root.addHandler(handler)
    # httpx journalise chaque requête en INFO : trop bavard pour une collecte continue.
    logging.getLogger("httpx").setLevel(logging.WARNING)
