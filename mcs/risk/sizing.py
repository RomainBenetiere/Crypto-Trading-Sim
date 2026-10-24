"""Dimensionnement des entrées (décision 13).

- % fixe : taille = montant max investi en une fois × capital ;
- basé sur le risque : taille = capital × risque par trade ÷ distance relative au stop initial,
  plafonnée par le montant max en une fois ;
- dans les deux cas, plafonds : % max sur une crypto, exposition totale max, liquidités.
Le capital est la valeur totale du portefeuille (capital composé).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from mcs.strategy.config import PortfolioConfig, Sizing


@dataclass(frozen=True, slots=True)
class SizingInputs:
    capital: Decimal  # valeur totale du portefeuille
    available_cash: Decimal
    exposure: Decimal  # valeur des positions + achats en attente
    market_exposure: Decimal  # idem, sur la paire visée
    stop_distance: Decimal  # (prix d'entrée − stop initial) / prix d'entrée


def entry_amount(config: PortfolioConfig, inputs: SizingInputs) -> Decimal:
    """Montant à engager (frais compris), 0 si aucun budget."""
    max_entry = inputs.capital * config.max_entry
    if config.sizing is Sizing.FIXED_PCT:
        amount = max_entry
    else:
        if inputs.stop_distance <= 0:
            return Decimal(0)
        amount = min(inputs.capital * config.risk_per_trade / inputs.stop_distance, max_entry)
    amount = min(
        amount,
        inputs.capital * config.max_per_crypto - inputs.market_exposure,
        inputs.capital * config.max_exposure - inputs.exposure,
        inputs.available_cash,
    )
    return max(amount, Decimal(0))
