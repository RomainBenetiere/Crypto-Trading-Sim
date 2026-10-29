"""Lecture seule du solde du compte Bitvavo (mode production, palier 2 « ombre »).

Seul appel authentifié du projet : la lecture du solde (GET). Signature HMAC-SHA256 (hex)
de horodatage + méthode + chemin préfixé de /v2, avec le secret ; en-têtes `bitvavo-access-*`
(SDK officiel `python-bitvavo-api`). La clé doit n'avoir que le droit « Consulter » : ce
module n'envoie aucun ordre et ne sait faire que des GET.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import httpx

from mcs.bitvavo.rest import BASE_URL, BitvavoError
from mcs.storage.db import now_ms

BALANCE_PATH = "/balance"
ACCESS_WINDOW_MS = 10_000
TIMEOUT_S = 15.0


@dataclass(frozen=True, slots=True)
class Balance:
    symbol: str
    available: Decimal
    in_order: Decimal


def sign(secret: str, timestamp: int, method: str, path: str, body: str = "") -> str:
    message = f"{timestamp}{method}/v2{path}{body}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


class BitvavoAccount:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self._key, self._secret, self._clock = api_key, api_secret, clock
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT_S)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def balances(self) -> list[Balance]:
        """Soldes non nuls du compte."""
        timestamp = self._clock()
        headers = {
            "bitvavo-access-key": self._key,
            "bitvavo-access-signature": sign(self._secret, timestamp, "GET", BALANCE_PATH),
            "bitvavo-access-timestamp": str(timestamp),
            "bitvavo-access-window": str(ACCESS_WINDOW_MS),
        }
        response = await self._client.get(BALANCE_PATH, headers=headers)
        payload = response.json()
        if response.status_code != 200:
            error = payload if isinstance(payload, dict) else {}
            raise BitvavoError(
                response.status_code, error.get("errorCode"), str(error.get("error", payload))
            )
        balances = [
            Balance(row["symbol"], Decimal(row["available"]), Decimal(row["inOrder"]))
            for row in payload
        ]
        return [b for b in balances if b.available or b.in_order]
