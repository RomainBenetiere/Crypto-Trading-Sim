"""Formatage fr-FR du dashboard."""

from decimal import Decimal

from dashboard import fmt

T = chr(0x202F)
M = chr(0x2212)


def test_numbers_and_euros() -> None:
    assert fmt.number(1234.5) == f"1{T}234,50"
    assert fmt.number(-3.2) == f"{M}3,20"
    assert fmt.number(-0.001) == "0,00"  # pas de « −0,00 »
    assert fmt.eur(Decimal("1000")) == f"1{T}000,00 €"
    assert fmt.eur(None) == "—"
    assert fmt.signed_eur(41.97) == "+41,97 €"
    assert fmt.signed_eur(-2.5) == f"{M}2,50 €"
    assert fmt.signed_eur(0) == "0,00 €"


def test_percentages() -> None:
    assert fmt.pct(4.25) == "4,2 %" or fmt.pct(4.25) == "4,3 %"
    assert fmt.pct(-14.0) == f"{M}14,0 %"
    assert fmt.pct(3, signed=True) == "+3,0 %"
    assert fmt.share(0.463) == "46,3 %"


def test_prices_by_magnitude() -> None:
    assert fmt.price(96426.4) == f"96{T}426,40 €"
    assert fmt.price(1.1814) == "1,1814 €"
    assert fmt.price(0.1476) == "0,1476 €"
    assert fmt.price(0.0000095120) == "0,0₅9512 €"  # exemple de CLAUDE.md
    assert fmt.price(0.000008864) == "0,0₅8864 €"
    assert fmt.price(0.00012) == "0,0₃12 €"
    assert fmt.price(Decimal("0.0000038594")) == "0,0₅3859 €"
    assert fmt.price(None) == "—"


def test_tiny_rounding_carry() -> None:
    assert fmt.tiny(0.0000099999) == "0,0₄1"


def test_quantities_dates_labels() -> None:
    assert fmt.quantity(149.595) == "149,6" or fmt.quantity(149.595) == "149,59"
    assert fmt.quantity(1297431) == f"1{T}297{T}431"
    assert fmt.quantity(0.00183281) == "0,001833"
    # 25/10/2026 12:00 UTC = 13:00 à Paris (heure d'hiver depuis le 25/10 à 3 h).
    assert fmt.timestamp(1792929600000) == "25/10/2026 13:00"
    assert fmt.timestamp(1792929600000, with_time=False) == "25/10/2026"
    assert fmt.sim_label(7) == "S-007"
    assert fmt.duration_days(4 * 86_400_000) == "4,0 j"
