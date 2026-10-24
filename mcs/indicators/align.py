"""Alignement temporel entre résolutions, sans fuite d'information future.

Une bougie n'est utilisable qu'à sa clôture : la bougie `interval` qui démarre à `ts` est
connue à `ts + durée`. Une décision prise à la clôture d'une bougie 4 h (ts + 4 h) ne voit que
les bougies journalières déjà clôturées à cet instant.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from mcs.market_data import INTERVAL_MS


def as_of(
    source: pd.Series, source_interval: str, target_index: pd.Index, target_interval: str
) -> pd.Series:
    """Valeur de `source` connue à la clôture de chaque bougie cible (NaN si aucune).

    `source` est indexée par le début des bougies `source_interval` ; le résultat est indexé
    par `target_index` (début des bougies `target_interval`).
    """
    if source.empty:
        return pd.Series(np.nan, index=target_index, name=source.name, dtype="float64")
    available_at = source.index.to_numpy(dtype="int64") + INTERVAL_MS[source_interval]
    decided_at = target_index.to_numpy(dtype="int64") + INTERVAL_MS[target_interval]
    order = np.argsort(available_at, kind="stable")
    available_at = available_at[order]
    values = source.to_numpy(dtype="float64")[order]
    position = np.searchsorted(available_at, decided_at, side="right") - 1
    out = np.where(position >= 0, values[np.clip(position, 0, None)], np.nan)
    return pd.Series(out, index=target_index, name=source.name)


def as_of_flag(
    source: pd.Series, source_interval: str, target_index: pd.Index, target_interval: str
) -> pd.Series:
    """Variante booléenne (False quand aucune bougie source n'est encore clôturée)."""
    aligned = as_of(source.astype("float64"), source_interval, target_index, target_interval)
    return aligned.fillna(0.0).astype(bool)
