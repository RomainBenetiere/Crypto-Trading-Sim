import random

from mcs.ideal import ideal, report
from mcs.metrics import SimRecord
from mcs.sampling import CATEGORICALS, NUMERICS, draw_plan


def records() -> list[SimRecord]:
    """P&L dicté par deux variables connues (+ bruit), Calmar par une troisième."""
    rng = random.Random(1)
    result = []
    for i, draw in enumerate(draw_plan(seed=7)):
        for mode in ("typical", "optimized"):
            pnl = 20.0 * (draw["trailing_type"] == "atr") - 100.0 * float(draw["max_exposure"])
            calmar = 3.0 * (draw["family"] == "donchian") + rng.gauss(0, 0.3)
            result.append(
                SimRecord(
                    2 * i + (mode == "optimized"),
                    i,
                    mode,
                    str(draw["family"]),
                    draw,
                    pnl + rng.gauss(0, 2),
                    12,
                    calmar,
                )
            )
    return result


def test_ideal_finds_planted_effects_and_respects_conditions() -> None:
    data = records()
    best_pnl = ideal(data, "pnl")
    assert best_pnl.draw["trailing_type"] == "atr"
    assert best_pnl.draw["max_exposure"] < 0.67  # tiers bas de 0,50 à 1,00
    assert best_pnl.draw["trailing_pct"] is None  # sans objet avec un trailing ATR
    assert best_pnl.draw["trailing_atr_multiple"] is not None
    assert set(best_pnl.draw) == {v.name for v in (*CATEGORICALS, *NUMERICS)}
    choices = {c.variable: c for c in best_pnl.choices}
    assert choices["trailing_type"].significant and choices["max_exposure"].significant
    assert ideal(data, "calmar").draw["family"] == "donchian"
    text = report(best_pnl)
    assert "Trailing stop" in text and "Simulations existantes les plus proches" in text
