"""Configuration complète d'une simulation (variables tirées, SPEC §4).

Les fractions sont exprimées en `Decimal` quand elles touchent l'argent (tailles, stops,
take profit) ; les poids et seuils du score restent en `float` (comparaison de signaux).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from mcs.indicators.signals import (
    AdxConfirmation,
    DonchianBreakout,
    EmaCross,
    Macd,
    RsiConfirmation,
    Supertrend,
    TrendFilter,
    VolumeConfirmation,
)

MainSignalConfig = EmaCross | Macd | Supertrend | DonchianBreakout
ConfirmationConfig = AdxConfirmation | VolumeConfirmation | RsiConfirmation

ATR_PERIOD = 14  # période de l'ATR 4 h des stops et trailings (non tirée)
CHANDELIER_PERIOD = 22  # décision 18
DEFAULT_SUPERTREND = Supertrend(10, 3.0)  # ligne du trailing Supertrend hors famille Supertrend
CIRCUIT_BREAKER_PAUSE_BARS = 42  # 7 jours de bougies 4 h (décision 16)
MIN_DAILY_HISTORY = 200  # décision 2


class TrendAction(StrEnum):
    BLOCK = "block"
    BLOCK_AND_EXIT = "block_and_exit"


class StopType(StrEnum):
    FIXED_PCT = "fixed_pct"
    ATR = "atr"
    LOWEST_LOW = "lowest_low"


class TrailingType(StrEnum):
    NONE = "none"
    ATR = "atr"
    PCT = "pct"
    SUPERTREND = "supertrend"
    CHANDELIER = "chandelier"


class TrailingActivation(StrEnum):
    IMMEDIATE = "immediate"
    GAIN_PCT = "gain_pct"
    ONE_R = "one_r"


class TakeProfitType(StrEnum):
    NONE = "none"
    R_MULTIPLE = "r_multiple"
    FIXED_PCT = "fixed_pct"
    PARTIAL = "partial"


class StopRecalc(StrEnum):
    CLOSE_4H = "close_4h"
    HOURLY = "hourly"


class EntryOrder(StrEnum):
    LIMIT = "limit"
    MARKET = "market"


class Sizing(StrEnum):
    FIXED_PCT = "fixed_pct"
    RISK = "risk"


@dataclass(frozen=True, slots=True)
class TrendFilterConfig:
    enabled: bool
    filter: TrendFilter | None = None
    action: TrendAction = TrendAction.BLOCK

    def __post_init__(self) -> None:
        if self.enabled and self.filter is None:
            raise ValueError("Filtre de fond activé sans paramètres.")


@dataclass(frozen=True, slots=True)
class SignalConfig:
    main: MainSignalConfig
    main_weight: float
    confirmations: tuple[tuple[ConfirmationConfig, float], ...] = ()
    threshold: float = 0.5

    def __post_init__(self) -> None:
        total = self.main_weight + sum(w for _, w in self.confirmations)
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"Les poids doivent totaliser 1 (total {total}).")
        if not 0 < self.threshold <= 1:
            raise ValueError("Seuil du score hors de ]0, 1].")

    @property
    def warmup(self) -> int:
        return max([self.main.warmup, *(c.warmup for c, _ in self.confirmations)])


@dataclass(frozen=True, slots=True)
class StopConfig:
    type: StopType
    pct: Decimal = Decimal("0.05")  # % fixe (fraction)
    atr_multiple: Decimal = Decimal(2)
    lowest_n: int = 10


@dataclass(frozen=True, slots=True)
class TrailingConfig:
    type: TrailingType = TrailingType.NONE
    atr_multiple: Decimal = Decimal(3)  # ATR et Chandelier
    pct: Decimal = Decimal("0.08")
    activation: TrailingActivation = TrailingActivation.IMMEDIATE
    activation_gain: Decimal = Decimal("0.02")  # gain déclencheur (fraction) si GAIN_PCT


@dataclass(frozen=True, slots=True)
class TakeProfitConfig:
    type: TakeProfitType = TakeProfitType.NONE
    r_multiple: Decimal = Decimal(2)  # ratio R, et niveau X R du take profit partiel
    pct: Decimal = Decimal("0.15")
    partial_fraction: Decimal = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class ExitConfig:
    reverse_signal: bool = True
    recalc: StopRecalc = StopRecalc.CLOSE_4H
    max_holding_bars: int | None = None  # bougies 4 h


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    entry_order: EntryOrder = EntryOrder.LIMIT
    limit_expiry_hours: int = 1


@dataclass(frozen=True, slots=True)
class PortfolioConfig:
    max_per_crypto: Decimal = Decimal("0.4")
    max_exposure: Decimal = Decimal(1)
    max_entry: Decimal = Decimal("0.25")  # montant max investi en une fois (et % fixe)
    sizing: Sizing = Sizing.RISK
    risk_per_trade: Decimal = Decimal("0.01")
    max_entries: int = 1  # 1 = pas de renforcement
    max_positions: int = 5
    pause_after_stop_bars: int = 0
    circuit_breaker_drawdown: Decimal = Decimal("0.2")

    def __post_init__(self) -> None:
        for name in ("max_per_crypto", "max_exposure", "max_entry", "risk_per_trade",
                     "circuit_breaker_drawdown"):  # fmt: skip
            value = getattr(self, name)
            if not Decimal(0) < value <= Decimal(1):
                raise ValueError(f"{name} hors de ]0, 1] : {value}")
        if self.max_entries < 1 or self.max_positions < 1:
            raise ValueError("max_entries et max_positions doivent être ≥ 1.")


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    signals: SignalConfig
    trend: TrendFilterConfig = field(default_factory=lambda: TrendFilterConfig(False))
    stop: StopConfig = field(default_factory=lambda: StopConfig(StopType.ATR))
    trailing: TrailingConfig = field(default_factory=TrailingConfig)
    take_profit: TakeProfitConfig = field(default_factory=TakeProfitConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)

    @property
    def warmup_4h(self) -> int:
        """Historique 4 h minimal pour les signaux, stops et trailings de la simulation."""
        needs = [self.signals.warmup, ATR_PERIOD + 1]
        if self.stop.type is StopType.LOWEST_LOW:
            needs.append(self.stop.lowest_n)
        if self.trailing.type is TrailingType.CHANDELIER:
            needs.append(CHANDELIER_PERIOD)
        if self.trailing.type is TrailingType.SUPERTREND:
            needs.append(self.supertrend_for_trailing.warmup)
        return max(needs)

    @property
    def supertrend_for_trailing(self) -> Supertrend:
        main = self.signals.main
        return main if isinstance(main, Supertrend) else DEFAULT_SUPERTREND
