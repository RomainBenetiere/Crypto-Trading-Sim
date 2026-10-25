"""Formatage `fr-FR` du dashboard (mêmes règles que la maquette).

- séparateur des milliers : espace fine insécable (U+202F), décimales avec une virgule ;
- signe moins typographique (U+2212) ;
- prix < 0,001 € en notation à indice : 0,0₅9512 € (cinq zéros après la virgule) ;
- dates en Europe/Paris.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
THIN = chr(0x202F)  # espace fine insécable
MINUS = chr(0x2212)  # signe moins
SUBSCRIPTS = "₀₁₂₃₄₅₆₇₈₉"
Number = float | int | Decimal


def number(value: Number, decimals: int = 2) -> str:
    """1234.5 → « 1 234,50 », −3.2 → « −3,20 »."""
    v = float(value)
    text = f"{abs(v):,.{decimals}f}".replace(",", THIN).replace(".", ",")
    is_zero = not any(c in "123456789" for c in text)
    return (MINUS if v < 0 and not is_zero else "") + text


def eur(value: Number | None, decimals: int = 2) -> str:
    return "—" if value is None else f"{number(value, decimals)} €"


def signed_eur(value: Number | None) -> str:
    """+12,30 € / −12,30 € / 0,00 €."""
    if value is None:
        return "—"
    v = float(value)
    sign = "+" if v > 0.005 else MINUS if v < -0.005 else ""
    return f"{sign}{number(abs(v))} €"


def pct(value: Number | None, decimals: int = 1, signed: bool = False) -> str:
    """Valeur déjà en pourcentage : 4.25 → « 4,3 % »."""
    if value is None:
        return "—"
    v = float(value)
    sign = ("+" if v > 0 else "") if signed else ""
    return f"{sign}{number(v, decimals)} %"


def share(fraction: Number | None, decimals: int = 1) -> str:
    """Fraction : 0.463 → « 46,3 % »."""
    return "—" if fraction is None else pct(float(fraction) * 100, decimals)


def tiny(value: float) -> str:
    """Prix < 0,001 : 0.0000095120 → « 0,0₅9512 » (4 chiffres significatifs)."""
    zeros = -math.floor(math.log10(value)) - 1
    significant = round(value * 10 ** (zeros + 4))
    if significant >= 10_000:  # retenue de l'arrondi (0,0000099999 → 0,0₄1)
        zeros -= 1
        significant //= 10
    digits = str(significant).rstrip("0")
    return "0,0" + "".join(SUBSCRIPTS[int(c)] for c in str(zeros)) + digits


def price(value: Number | None) -> str:
    """Prix adapté à son ordre de grandeur, en euros."""
    if value is None:
        return "—"
    v = abs(float(value))
    if v >= 10:
        text = number(v, 2)
    elif v >= 1:
        text = number(v, 4)
    elif v >= 0.001:
        text = f"{v:.4g}".replace(".", ",")
    elif v == 0:
        text = "0"
    else:
        text = tiny(v)
    return f"{MINUS if float(value) < 0 else ''}{text} €"


def quantity(value: Number | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    decimals = 0 if abs(v) >= 1000 else 2 if abs(v) >= 1 else 6
    text = number(v, decimals)
    if "," in text:
        text = text.rstrip("0").rstrip(",")
    return text


def timestamp(ms: int | None, with_time: bool = True) -> str:
    """Horodatage (ms UTC) affiché en heure de Paris."""
    if ms is None:
        return "—"
    moment = datetime.fromtimestamp(ms / 1000, PARIS)
    return moment.strftime("%d/%m/%Y %H:%M" if with_time else "%d/%m/%Y")


def duration_days(ms: int) -> str:
    days = ms / 86_400_000
    return f"{number(days, 1)} j" if days < 10 else f"{number(days, 0)} j"


def sim_label(sim_id: int) -> str:
    return f"S-{sim_id:03d}"
