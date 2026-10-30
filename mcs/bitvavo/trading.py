"""Client d'ordres réels Bitvavo (mode production, palier 3, SPEC §15).

Seul module du projet autorisé à créer, lire et annuler des ordres sur le compte. Il n'est
utilisé que par `BitvavoBroker` (`mcs/broker/live.py`), lui-même démarré seulement si le double
verrou du `.env` est levé (`PRODUCTION_LIVE=1` et `PRODUCTION_MAX_CAPITAL`). La clé doit avoir
les droits « Consulter » et « Trader », jamais « Retirer » : aucun retrait n'est codé ici.

Documentation officielle vérifiée (oct. 2026) :
- `POST /order` : `market`, `side`, `orderType` (`market` / `limit`), `operatorId` (obligatoire),
  `clientOrderId` (UUID, unique parmi les ordres ouverts), `amount` (base) ou `amountQuote`
  (marché), `price` (limite, multiple du `tickSize`), `postOnly`, `timeInForce` ;
- `GET /order` et `DELETE /order` : paramètres de requête `market`, `orderId` ou
  `clientOrderId` (prioritaire), `operatorId` pour l'annulation ;
- réponse : `status` (`new`, `awaitingTrigger`, `canceled`, `expired`, `filled`,
  `partiallyFilled`), `filledAmount`, `filledAmountQuote`, `feePaid`, `feeCurrency`.
Signature : HMAC-SHA256 de horodatage + méthode + `/v2` + chemin (requête comprise) + corps.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from mcs.bitvavo.account import ACCESS_WINDOW_MS, TIMEOUT_S, Balance, sign
from mcs.bitvavo.rest import BASE_URL, BitvavoError
from mcs.storage.db import now_ms

ORDER_PATH = "/order"
BALANCE_PATH = "/balance"
# Code d'erreur Bitvavo « No order found » (GET / DELETE sur un ordre inconnu ou déjà clos).
ORDER_NOT_FOUND = frozenset({240})


@dataclass(frozen=True, slots=True)
class ExchangeOrder:
    """État d'un ordre sur Bitvavo (montants en `Decimal`)."""

    order_id: str
    client_order_id: str | None
    market: str
    side: str
    status: str
    filled_amount: Decimal
    filled_quote: Decimal
    fee_paid: Decimal
    fee_currency: str | None

    @property
    def done(self) -> bool:
        """Ordre clos : plus aucune exécution possible."""
        return self.status in ("filled", "canceled", "expired", "rejected")


def parse_order(payload: dict[str, Any]) -> ExchangeOrder:
    return ExchangeOrder(
        order_id=str(payload["orderId"]),
        client_order_id=payload.get("clientOrderId"),
        market=str(payload.get("market", "")),
        side=str(payload.get("side", "")),
        status=str(payload["status"]),
        filled_amount=Decimal(payload.get("filledAmount") or 0),
        filled_quote=Decimal(payload.get("filledAmountQuote") or 0),
        fee_paid=Decimal(payload.get("feePaid") or 0),
        fee_currency=payload.get("feeCurrency") or None,
    )


class BitvavoTrading:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        operator_id: int,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self._key, self._secret, self._clock = api_key, api_secret, clock
        self.operator_id = operator_id
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT_S)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def balances(self) -> list[Balance]:
        payload = await self._request("GET", BALANCE_PATH)
        balances = [
            Balance(row["symbol"], Decimal(row["available"]), Decimal(row["inOrder"]))
            for row in payload
        ]
        return [b for b in balances if b.available or b.in_order]

    async def create_order(
        self,
        market: str,
        side: str,
        order_type: str,
        client_order_id: str,
        *,
        amount: Decimal | None = None,
        amount_quote: Decimal | None = None,
        price: Decimal | None = None,
        post_only: bool = False,
    ) -> ExchangeOrder:
        body: dict[str, Any] = {
            "market": market,
            "side": side,
            "orderType": order_type,
            "clientOrderId": client_order_id,
            "operatorId": self.operator_id,
        }
        if amount is not None:
            body["amount"] = format(amount, "f")
        if amount_quote is not None:
            body["amountQuote"] = format(amount_quote, "f")
        if price is not None:
            body["price"] = format(price, "f")
            body["timeInForce"] = "GTC"
            body["postOnly"] = post_only
        return parse_order(await self._request("POST", ORDER_PATH, body=body))

    async def get_order(self, market: str, client_order_id: str) -> ExchangeOrder | None:
        """État de l'ordre, ou None s'il est inconnu de Bitvavo (jamais créé)."""
        try:
            payload = await self._request(
                "GET", ORDER_PATH, params={"market": market, "clientOrderId": client_order_id}
            )
        except BitvavoError as exc:
            if exc.code in ORDER_NOT_FOUND:
                return None
            raise
        return parse_order(payload)

    async def cancel_order(self, market: str, client_order_id: str) -> None:
        """Annule l'ordre ; sans effet s'il est déjà clos (rempli ou annulé)."""
        try:
            await self._request(
                "DELETE",
                ORDER_PATH,
                params={
                    "market": market,
                    "clientOrderId": client_order_id,
                    "operatorId": str(self.operator_id),
                },
            )
        except BitvavoError as exc:
            if exc.code not in ORDER_NOT_FOUND:
                raise

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        target = f"{path}?{urlencode(params)}" if params else path
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        timestamp = self._clock()
        headers = {
            "bitvavo-access-key": self._key,
            "bitvavo-access-signature": sign(self._secret, timestamp, method, target, payload),
            "bitvavo-access-timestamp": str(timestamp),
            "bitvavo-access-window": str(ACCESS_WINDOW_MS),
            "content-type": "application/json",
        }
        response = await self._client.request(
            method, target, headers=headers, content=payload or None
        )
        try:
            data = response.json()
        except ValueError:
            data = response.text
        if response.status_code != 200:
            error = data if isinstance(data, dict) else {}
            raise BitvavoError(
                response.status_code, error.get("errorCode"), str(error.get("error", data))
            )
        return data
