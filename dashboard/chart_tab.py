"""Onglet « Graphiques » : mosaïque des paires et chandeliers avec indicateurs (décision 41)."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from dashboard import charts, fmt
from mcs.storage.db import now_ms

GREEN, RED, GREY, BLUE, ORANGE = "#26a269", "#c0392b", "#9ca3af", "#2f6fd6", "#e07a1f"
EXIT_COLORS = {
    "stop": "#c0392b",
    "trailing": "#e07a1f",
    "take_profit": "#26a269",
    "reverse_signal": "#7b61c9",
    "max_duration": "#6b7280",
    "trend_filter": "#c2417a",
    "circuit_breaker": "#111827",
}
REASON_LABELS = {
    "signal": "Entrée",
    "stop": "Stop",
    "trailing": "Trailing",
    "take_profit": "Take profit",
    "reverse_signal": "Signal inverse",
    "max_duration": "Durée max",
    "trend_filter": "Filtre de fond",
    "circuit_breaker": "Circuit breaker",
}
TILE_COLUMNS = 4


def paris(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, fmt.PARIS)


def render(conn: Any, markets: list[str], version: tuple[Any, ...]) -> None:
    if not markets:
        st.warning("Aucune paire sélectionnée dans « Marchés du panier ».")
        return
    days = st.select_slider(
        "Période de la mosaïque",
        options=[7, 14, 30, 60, 90],
        value=14,
        format_func=lambda d: f"{d} j",
        key="tiles_days",
    )
    mosaic(conn, markets, days, version)
    detail(conn, markets, version)


# --- Mosaïque -------------------------------------------------------------------------------


@st.cache_data(ttl=600, show_spinner=False)
def _tiles(_conn: Any, markets: tuple[str, ...], days: int, version: tuple[Any, ...]) -> list[Any]:
    now = now_ms()
    return [charts.tile(_conn, m, days, now) for m in markets]


def mosaic(conn: Any, markets: list[str], days: int, version: tuple[Any, ...]) -> None:
    tiles = _tiles(conn, tuple(markets), days, version)
    rows = math.ceil(len(tiles) / TILE_COLUMNS)
    titles = []
    for t in tiles:
        change = "—" if t.change_pct is None else fmt.pct(t.change_pct, 1, signed=True)
        trend = {True: "filtre ✓", False: "filtre ✗", None: "filtre —"}[t.trend_ok]
        titles.append(f"{t.market.split('-')[0]} {change} · {trend}")
    fig = make_subplots(
        rows=rows,
        cols=TILE_COLUMNS,
        subplot_titles=titles,
        vertical_spacing=0.09,
        horizontal_spacing=0.04,
    )
    for i, t in enumerate(tiles):
        color = GREEN if (t.change_pct or 0) >= 0 else RED
        fig.add_scatter(
            x=[paris(ts) for ts in t.closes.index],
            y=t.closes.to_numpy(),
            mode="lines",
            line={"color": color, "width": 1.5},
            row=i // TILE_COLUMNS + 1,
            col=i % TILE_COLUMNS + 1,
            name=t.market,
            customdata=[t.market] * len(t.closes),
            hovertemplate="%{customdata}<br>%{x|%d/%m %H:%M}<br>%{y}<extra></extra>",
        )
    fig.update_xaxes(showticklabels=False, showgrid=False)
    fig.update_yaxes(showticklabels=False, showgrid=False)
    fig.update_layout(
        height=150 * rows + 40,
        showlegend=False,
        separators=", ",
        margin={"l": 6, "r": 6, "t": 30, "b": 6},
    )
    fig.update_annotations(font_size=12)
    event = st.plotly_chart(
        fig, use_container_width=True, on_select="rerun", selection_mode="points", key="tiles_chart"
    )
    points = event.selection.points if event and event.selection else []
    if points:
        picked = points[0].get("customdata")
        if isinstance(picked, list):
            picked = picked[0]
        if picked in markets:
            st.session_state["chart_market"] = picked
    st.caption(
        f"Clôtures 4 h sur {days} jours ; « filtre ✓ » : filtre de fond typique (prix > SMA 200 "
        "journalière) favorable. Cliquez sur une miniature pour l'afficher en détail."
    )


# --- Vue détaillée --------------------------------------------------------------------------


def detail(conn: Any, markets: list[str], version: tuple[Any, ...]) -> None:
    st.subheader("Chandeliers et indicateurs")
    cols = st.columns([2, 1, 1, 2])
    current = st.session_state.get("chart_market", markets[0])
    market = cols[0].selectbox(
        "Paire",
        markets,
        index=markets.index(current) if current in markets else 0,
        key="chart_market_select",
    )
    st.session_state["chart_market"] = market
    interval_label = cols[1].selectbox(
        "Intervalle", list(charts.INTERVALS), index=1, key="chart_interval"
    )
    interval = charts.INTERVALS[interval_label]
    default_days = charts.DEFAULT_DAYS[interval]
    days = cols[2].selectbox(
        "Fenêtre",
        charts.WINDOWS,
        index=charts.WINDOWS.index(default_days),
        format_func=lambda d: f"{d} j",
        key=f"chart_days_{interval}",
    )
    sims = [None, *range(1, 301)]
    sim_id = cols[3].selectbox(
        "Réglages",
        sims,
        key="chart_sim",
        format_func=lambda s: "Valeurs typiques" if s is None else f"Simulation {fmt.sim_label(s)}",
    )
    settings = charts.ChartIndicators()
    if sim_id is not None:
        config = charts.simulation_config(conn, sim_id)
        if config is None:
            st.warning(f"{fmt.sim_label(sim_id)} n'existe pas dans le plan.")
            sim_id = None
        else:
            settings = charts.indicators_for(config, f"réglages de {fmt.sim_label(sim_id)}")
    toggles = st.columns(5)
    show = {
        "ema": toggles[0].toggle("EMA", value=True, key="ov_ema"),
        "supertrend": toggles[1].toggle("Supertrend", value=False, key="ov_st"),
        "donchian": toggles[2].toggle("Donchian", value=False, key="ov_dc"),
        "filter": toggles[3].toggle("Filtre de fond", value=True, key="ov_filter"),
        "band": toggles[4].toggle("Bande des signaux", value=True, key="ov_band"),
    }
    panel_names = st.multiselect(
        "Sous-graphiques",
        ["Volume", "RSI", "MACD", "ADX"],
        default=["Volume", "RSI"],
        key="chart_panels",
    )
    data = _chart(conn, market, interval, days, settings, version)
    if data is None or data.frame.empty:
        st.info(f"Pas de bougies {interval_label} pour {market} sur cette période.")
        return
    figure(data, market, interval, settings, show, panel_names, conn, sim_id)
    st.caption(
        f"Indicateurs calculés sur tout l'historique avec le code du moteur ({settings.label}), "
        "bougies clôturées uniquement ; heure de Paris. Bande : vert = signal principal en "
        "achat et filtre favorable, orange = signal en achat mais filtre bloquant, gris = pas "
        "de signal."
    )


@st.cache_data(ttl=600, show_spinner=False)
def _chart(
    _conn: Any, market: str, interval: str, days: int, settings: Any, version: tuple[Any, ...]
) -> Any:
    last = _conn.execute(
        "SELECT MAX(ts) FROM candles WHERE market = ? AND interval = ?", (market, interval)
    ).fetchone()[0]
    if last is None:
        return None
    history = charts.load_history(
        _conn, market, interval, charts.history_start(interval, int(last), days)
    )
    daily = charts.load_history(_conn, market, "1d")
    start = int(history.index[-1]) - days * charts.DAY
    return charts.chart_data(history, daily, interval, start, settings)


def figure(
    data: Any,
    market: str,
    interval: str,
    settings: Any,
    show: dict[str, bool],
    panel_names: list[str],
    conn: Any,
    sim_id: int | None,
) -> None:
    frame = data.frame
    x = [paris(int(ts)) for ts in frame.index]
    panels = list(panel_names)
    band = show["band"] and data.main_buy is not None
    heights = [0.62] + ([0.04] if band else []) + [0.34 / max(len(panels), 1)] * len(panels)
    rows = len(heights)
    fig = make_subplots(
        rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.02, row_heights=heights
    )
    hover = [
        f"{fmt.timestamp(int(ts))}<br>O {fmt.price(o)} · H {fmt.price(h)}<br>"
        f"B {fmt.price(lo)} · C {fmt.price(c)}<br>Volume {fmt.number(v, 0)}"
        for ts, o, h, lo, c, v in frame[["open", "high", "low", "close", "volume"]].itertuples()
    ]
    fig.add_trace(
        go.Candlestick(
            x=x,
            open=frame["open"],
            high=frame["high"],
            low=frame["low"],
            close=frame["close"],
            name=market,
            hovertext=hover,
            hoverinfo="text",
            increasing_line_color=GREEN,
            decreasing_line_color=RED,
        ),
        row=1,
        col=1,
    )
    overlay_colors = iter([BLUE, ORANGE, "#2a9d8f", "#7b61c9", "#7b61c9"])
    for name, series in data.overlays.items():
        color = next(overlay_colors)
        kind = name.split()[0].lower()
        if (kind == "ema" and show["ema"]) or (kind == "donchian" and show["donchian"]):
            dash = "dot" if kind == "donchian" else None
            fig.add_scatter(
                x=x,
                y=series,
                mode="lines",
                name=name,
                row=1,
                col=1,
                line={"color": color, "width": 1.3, "dash": dash},
            )
        if kind == "supertrend" and show["supertrend"]:
            up = data.panels["_supertrend_up"]["up"].to_numpy()
            for flag, line_color, label in ((True, GREEN, "haussier"), (False, RED, "baissier")):
                values = series.where(up == flag)
                fig.add_scatter(
                    x=x,
                    y=values,
                    mode="lines",
                    name=f"{name} {label}",
                    row=1,
                    col=1,
                    line={"color": line_color, "width": 1.5},
                    connectgaps=False,
                )
    if show["filter"] and data.filter_line is not None:
        fig.add_scatter(
            x=x,
            y=data.filter_line,
            mode="lines",
            name="Filtre de fond (1 j)",
            line={"color": GREY, "width": 2, "shape": "hv"},
            row=1,
            col=1,
        )
    if sim_id is not None:
        markers(fig, conn, sim_id, market, int(frame.index[0]), x[-1])
    row = 2
    if band:
        buy = data.main_buy.to_numpy()
        ok = data.trend_ok.to_numpy() if data.trend_ok is not None else [True] * len(buy)
        colors = [GREEN if b and o else ORANGE if b else GREY for b, o in zip(buy, ok, strict=True)]
        fig.add_bar(
            x=x,
            y=[1] * len(x),
            marker={"color": colors},
            name="Signaux",
            showlegend=False,
            hoverinfo="skip",
            row=row,
            col=1,
        )
        fig.update_yaxes(visible=False, row=row, col=1)
        row += 1
    for name in panels:
        add_panel(fig, data, name, x, settings, row)
        row += 1
    fig.update_layout(
        height=520 + 140 * len(panels),
        separators=", ",
        bargap=0,
        xaxis_rangeslider_visible=False,
        hovermode="x",
        legend={"orientation": "h", "y": 1.02, "x": 0},
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
    )
    if float(frame["close"].iloc[-1]) < 0.01:
        fig.update_yaxes(exponentformat="e", row=1, col=1)
    st.plotly_chart(fig, use_container_width=True, key="detail_chart")


def add_panel(
    fig: go.Figure, data: Any, name: str, x: list[datetime], settings: Any, row: int
) -> None:
    key = next(k for k in data.panels if k.startswith(name))
    values = data.panels[key]
    if name == "Volume":
        fig.add_bar(x=x, y=values["volume"], name="Volume", marker={"color": GREY}, row=row, col=1)
        fig.add_scatter(
            x=x,
            y=values["moyenne"],
            mode="lines",
            name=f"Volume moyen {settings.volume.window}",
            line={"color": BLUE, "width": 1},
            row=row,
            col=1,
        )
    elif name == "RSI":
        fig.add_scatter(
            x=x, y=values["rsi"], mode="lines", name=key, line={"color": "#7b61c9"}, row=row, col=1
        )
        fig.add_hline(y=settings.rsi.max_level, line_dash="dot", line_color=RED, row=row, col=1)
        fig.update_yaxes(range=[0, 100], row=row, col=1)
    elif name == "MACD":
        fig.add_bar(
            x=x,
            y=values["hist"],
            name="Histogramme",
            row=row,
            col=1,
            marker={"color": [GREEN if v >= 0 else RED for v in values["hist"].fillna(0)]},
        )
        fig.add_scatter(
            x=x, y=values["macd"], mode="lines", name="MACD", line={"color": BLUE}, row=row, col=1
        )
        fig.add_scatter(
            x=x,
            y=values["signal"],
            mode="lines",
            name="Signal",
            line={"color": ORANGE},
            row=row,
            col=1,
        )
    else:
        fig.add_scatter(
            x=x,
            y=values["adx"],
            mode="lines",
            name="ADX",
            line={"color": "#111827"},
            row=row,
            col=1,
        )
        fig.add_scatter(
            x=x,
            y=values["plus_di"],
            mode="lines",
            name="+DI",
            line={"color": GREEN},
            row=row,
            col=1,
        )
        fig.add_scatter(
            x=x, y=values["minus_di"], mode="lines", name="−DI", line={"color": RED}, row=row, col=1
        )
        fig.add_hline(y=settings.adx.threshold, line_dash="dot", line_color=GREY, row=row, col=1)
    fig.update_yaxes(title_text=name, title_font_size=11, row=row, col=1)


def markers(
    fig: go.Figure, conn: Any, sim_id: int, market: str, start: int, last_x: datetime
) -> None:
    orders = charts.simulation_orders(conn, sim_id, market, start)
    for side, symbol in (("buy", "triangle-up"), ("sell", "triangle-down")):
        rows = orders[orders["side"] == side]
        if rows.empty:
            continue
        colors = [GREEN if side == "buy" else EXIT_COLORS.get(r, GREY) for r in rows["reason"]]
        fig.add_scatter(
            x=[paris(int(t)) for t in rows["filled_at"]],
            y=rows["price"].astype(float),
            mode="markers",
            name="Achats" if side == "buy" else "Ventes",
            marker={
                "symbol": symbol,
                "size": 11,
                "color": colors,
                "line": {"width": 1, "color": "#fff"},
            },
            hovertext=[
                f"{REASON_LABELS.get(r, r)} à {fmt.price(float(p))}"
                for r, p in zip(rows["reason"], rows["price"], strict=True)
            ],
            hoverinfo="text",
            row=1,
            col=1,
        )
    position = charts.simulation_position(conn, sim_id, market)
    if position:
        levels = [
            ("Stop", RED, position["stop"]),
            ("Trailing", ORANGE, position["trailing"]),
            ("Take profit", GREEN, position["take_profit"]),
        ]
        for label, color, level in levels:
            if level is not None:
                fig.add_hline(
                    y=float(level),
                    line_dash="dash",
                    line_color=color,
                    row=1,
                    col=1,
                    annotation_text=f"{label} {fmt.price(float(level))}",
                    annotation_position="top left",
                )
