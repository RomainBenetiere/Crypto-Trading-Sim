"""Dashboard Streamlit : barre de pilotage et trois onglets (maquette `docs/maquette/`).

    streamlit run dashboard/app.py

Lecture seule de la base, sauf deux écritures : les commandes destinées au moteur et la
sélection du panier. Cours et détail du top 5 rafraîchis chaque minute ; le reste suit les
métriques horaires écrites par le moteur. Accès : réseau local uniquement (décision 35).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from dashboard import chart_tab, fmt, queries  # noqa: E402
from mcs.config import Settings, load_settings  # noqa: E402
from mcs.health import Level, run_checks  # noqa: E402
from mcs.live.state import Command  # noqa: E402
from mcs.metrics import (  # noqa: E402
    buy_and_hold_detail,
    buy_and_hold_series,
    buy_and_hold_value,
    cash_balance,
    latest_prices,
    load_buy_and_hold,
    load_records,
    positions_detail,
    synthesis,
    tornado,
)
from mcs.storage.db import connect, now_ms  # noqa: E402

PRIMARY, MUTED, BH_COLOR = "#2f6fd6", "#a7b3c6", "#6b7280"
TOP_COLORS = ["#2f6fd6", "#e07a1f", "#2a9d8f", "#c2417a", "#7b61c9"]
HEARTBEAT_STALE_MS = 3 * 60_000
COMMAND_LABELS = {
    Command.OPTIMIZE: "Optimiser les indicateurs",
    Command.START: "Lancer les simulations",
    Command.STOP: "Arrêter",
    Command.RESUME: "Reprendre",
    Command.RESET: "Remettre à zéro",
}

st.set_page_config(page_title="Simulations Monte-Carlo crypto", page_icon="📈", layout="wide")


# --- Ressources -----------------------------------------------------------------------------


@st.cache_resource
def settings() -> Settings:
    return load_settings(ROOT / ".env", base_dir=ROOT)


@st.cache_resource
def ro() -> Any:
    return queries.open_readonly(settings().db_path)


@st.cache_resource
def rw() -> Any:
    conn = connect(settings().db_path, settings().journal_mode)
    return conn


def data_version() -> tuple[Any, ...]:
    """Change dès que le moteur écrit de nouvelles métriques ou change d'état."""
    row = (
        ro()
        .execute(
            "SELECT (SELECT MAX(updated_at) FROM metrics), updated_at, "
            "(SELECT COUNT(*) FROM simulations), (SELECT MAX(optimized_at) FROM simulations) "
            "FROM engine_state"
        )
        .fetchone()
    )
    return tuple(row)


@st.cache_data(show_spinner=False)
def simulations(version: tuple[Any, ...]) -> pd.DataFrame:
    return queries.simulations_table(ro())


@st.cache_data(show_spinner=False)
def analysis(version: tuple[Any, ...]) -> tuple[Any, Any, float | None]:
    conn = ro()
    records = load_records(conn)
    lines = load_buy_and_hold(conn)
    bh = buy_and_hold_value(lines, latest_prices(conn), settings().fee_taker)
    bh_pnl = float(bh - 1000) if bh is not None else None
    return synthesis(records, bh_pnl), tornado(records), bh_pnl


@st.cache_data(show_spinner=False)
def indicator_table(version: tuple[Any, ...]) -> pd.DataFrame:
    return queries.indicator_params_table(ro())


def paris(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, fmt.PARIS)


# --- Barre de pilotage ----------------------------------------------------------------------


@st.fragment(run_every=10)
def control_bar() -> None:
    info = queries.engine_info(ro())
    status = queries.STATUS_LABELS[info.status]
    optimization = queries.OPTIMIZATION_LABELS[info.optimization_status]
    opt_detail = f"{info.optimized} / {info.simulations // 2} jumelles"
    if info.optimized_at:
        opt_detail += f", le {fmt.timestamp(info.optimized_at)}"
    started = "—"
    if info.started_at:
        started = (
            f"{fmt.timestamp(info.started_at, with_time=False)} (actif "
            f"{fmt.duration_days(info.active_ms)}, hold ou interruption "
            f"{fmt.duration_days(info.paused_ms)})"
        )
    st.markdown(
        f"**Statut** : {status} · **Optimisation** : {optimization} ({opt_detail}) · "
        f"**Démarré le** : {started}"
    )
    buttons(info)
    if info.heartbeat_at is None or now_ms() - info.heartbeat_at > HEARTBEAT_STALE_MS:
        last = fmt.timestamp(info.heartbeat_at) if info.heartbeat_at else "jamais"
        st.warning(
            f"Le moteur ne tourne pas (dernier signe de vie : {last}). Les commandes seront "
            "traitées à son démarrage : `python -m mcs.engine`."
        )
    if info.pending_command:
        label = COMMAND_LABELS[Command(info.pending_command)]
        st.info(f"Commande « {label} » envoyée : en attente du moteur.")
    elif info.last_result and info.last_command:
        st.caption(
            f"Dernière commande ({COMMAND_LABELS[Command(info.last_command)]}) : {info.last_result}."
        )


def buttons(info: queries.EngineInfo) -> None:
    allowed = queries.allowed_commands(info)
    confirming = st.session_state.get("confirm")
    if confirming:
        message = {
            Command.RESET: "Positions, ordres et P&L live seront effacés ; les configurations "
            "sont conservées.",
            Command.OPTIMIZE: "L'optimisation est déjà faite : toutes les jumelles optimisées "
            "seront recalibrées (environ 15 minutes).",
        }[confirming]
        st.warning(message)
        left, right = st.columns(2)
        if left.button("Confirmer", key="btn_confirm", type="primary"):
            queries.send_command(rw(), confirming)
            st.session_state.pop("confirm")
            st.rerun()
        if right.button("Annuler", key="btn_cancel"):
            st.session_state.pop("confirm")
            st.rerun()
        return
    row = st.columns([1] * max(len(allowed), 1) + [3])
    for col, command in zip(row, allowed, strict=False):
        needs_confirm = command is Command.RESET or (
            command is Command.OPTIMIZE and info.optimization_status == "DONE"
        )
        kind: Literal["primary", "secondary"] = (
            "primary" if command in (Command.START, Command.RESUME) else "secondary"
        )
        if col.button(COMMAND_LABELS[command], key=f"btn_{command.value}", type=kind):
            if needs_confirm:
                st.session_state["confirm"] = command
            else:
                queries.send_command(rw(), command)
            st.rerun()


# --- Onglet « Suivi des simulations » -------------------------------------------------------


def kpis(summary: Any) -> None:
    st.markdown(
        f"**{summary.count} simulations** · meilleur P&L {fmt.signed_eur(summary.best_pnl)} "
        f"({fmt.sim_label(summary.best_sim)}) · P&L médian {fmt.signed_eur(summary.pnl_median)} · "
        f"{summary.beat_bh} battent le buy & hold ({fmt.share(summary.beat_bh_share)})."
    )
    cols = st.columns(6)
    cols[0].metric(
        "Meilleur P&L",
        fmt.signed_eur(summary.best_pnl),
        fmt.sim_label(summary.best_sim),
        delta_color="off",
        delta_arrow="off",
    )
    cols[1].metric(
        "P&L médian",
        fmt.signed_eur(summary.pnl_median),
        "sur l'ensemble",
        delta_color="off",
        delta_arrow="off",
    )
    cols[2].metric(
        "Buy & hold",
        fmt.signed_eur(summary.bh_pnl),
        "1 000 € à poids égaux",
        delta_color="off",
        delta_arrow="off",
    )
    cols[3].metric(
        "Battent le buy & hold",
        fmt.share(summary.beat_bh_share),
        f"{summary.beat_bh} / {summary.count}",
        delta_color="off",
        delta_arrow="off",
    )
    cols[4].metric(
        "Optimisé − typique",
        fmt.signed_eur(summary.optimized_gap_mean),
        "écart moyen entre jumelles",
        delta_color="off",
        delta_arrow="off",
    )
    cols[5].metric(
        "Trades clôturés",
        fmt.number(summary.trades_mean, 1),
        "par simulation",
        delta_color="off",
        delta_arrow="off",
    )
    if summary.preliminary:
        st.warning(
            "Résultats préliminaires : moins de 10 trades clôturés par simulation en moyenne, "
            "la tornade n'est pas encore exploitable."
        )


def value_chart(table: pd.DataFrame) -> None:
    st.subheader("Valeur du portefeuille : top 5 et buy & hold")
    conn = ro()
    top = table.dropna(subset=["pnl_eur"]).nlargest(5, "pnl_eur")
    series = queries.value_series(conn, top["sim_id"].tolist())
    fig = go.Figure()
    for color, sim_id in zip(TOP_COLORS, top["sim_id"], strict=False):
        rows = series[series["sim_id"] == sim_id]
        fig.add_scatter(
            x=[paris(t) for t in rows["ts"]],
            y=rows["value"],
            mode="lines",
            name=fmt.sim_label(sim_id),
            line={"color": color, "width": 2},
        )
    lines = load_buy_and_hold(conn)
    if lines and len(series):
        start, end = int(series["ts"].min()), now_ms()
        bh = buy_and_hold_series(conn, lines, settings().fee_taker, start - 3_600_000, end)
        fig.add_scatter(
            x=[paris(t) for t, _ in bh],
            y=[float(v) for _, v in bh],
            mode="lines",
            name="Buy & hold",
            line={"color": BH_COLOR, "dash": "dash"},
        )
    for kind, start_ms, end_ms in queries.periods(conn):
        if kind in ("hold", "outage"):
            fig.add_vrect(
                x0=paris(start_ms),
                x1=paris(end_ms or now_ms()),
                fillcolor="#9ca3af",
                opacity=0.18,
                line_width=0,
                annotation_text="Hold" if kind == "hold" else "Interruption",
                annotation_position="top left",
            )
    fig.update_layout(
        separators=", ",
        height=360,
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        yaxis_title="€",
        legend={"orientation": "h", "y": -0.15},
        hovermode="x unified",
    )
    fig.update_xaxes(tickformat="%d/%m %H:%M", hoverformat="%d/%m/%Y %H:%M")
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "Valeur horaire depuis le lancement, frais déduits et positions nettes des frais de vente "
        "estimés. Zones grisées : Hold ou interruption du moteur."
    )


def tornado_chart(rows: list[Any], mean: float) -> None:
    st.subheader("Variables les plus influentes sur le P&L")
    if not rows:
        st.info("Pas encore assez de simulations avec des métriques.")
        return
    rows = rows[::-1]  # la plus grande amplitude en haut
    labels = [r.variable for r in rows]
    opacity = [1.0 if r.significant else 0.35 for r in rows]
    fig = go.Figure()
    for name, color, values, texts in (
        (
            "Option la moins favorable / tiers bas",
            MUTED,
            [r.low_pnl for r in rows],
            [f"{r.low_label} : {fmt.signed_eur(r.low_pnl)} ({r.low_n} sim.)" for r in rows],
        ),
        (
            "Option la plus favorable / tiers haut",
            PRIMARY,
            [r.high_pnl for r in rows],
            [f"{r.high_label} : {fmt.signed_eur(r.high_pnl)} ({r.high_n} sim.)" for r in rows],
        ),
    ):
        fig.add_bar(
            y=labels,
            x=[v - mean for v in values],
            base=mean,
            orientation="h",
            name=name,
            marker={"color": color, "opacity": opacity},
            hovertext=[
                t + ("" if r.significant else " — non significatif")
                for t, r in zip(texts, rows, strict=True)
            ],
            hoverinfo="text",
        )
    fig.add_vline(x=mean, line_color="#9ca3af", line_width=1)
    fig.update_layout(
        barmode="group",
        separators=", ",
        height=max(320, 26 * len(rows) + 80),
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        xaxis_title="P&L moyen (€)",
        legend={"orientation": "h", "y": -0.08},
    )
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "Écart de P&L moyen entre la pire et la meilleure option (variables par catégories) ou "
        "entre le tiers bas et le tiers haut (variables numériques), autour du P&L moyen. Barres "
        "pâles : écart inférieur à 2 erreurs-types. Variables conditionnelles calculées sur leur "
        "sous-groupe ; « Calibrage » mesuré par l'écart entre jumelles."
    )


def metrics_columns(frame: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Simulation": frame["Simulation"],
            "Calibrage": frame["Calibrage"],
            "Famille": frame["Famille"],
            "P&L": frame["pnl_eur"].map(fmt.signed_eur),
            "P&L %": frame["pnl_pct"].map(lambda v: fmt.pct(v, signed=True)),
            "Drawdown max": frame["dd_pct"].map(fmt.pct),
            "Trades": frame["trades"].map(lambda v: "—" if pd.isna(v) else str(int(v))),
            "Réussite": frame["win_rate"].map(fmt.share),
            "Frais": frame["fees"].map(fmt.eur),
            "vs buy & hold": frame["bh_gap"].map(fmt.signed_eur),
        }
    )


def top5(table: pd.DataFrame) -> None:
    st.subheader("Détail des 5 meilleures simulations")
    top = table.dropna(subset=["pnl_eur"]).nlargest(5, "pnl_eur").reset_index(drop=True)
    shown = metrics_columns(top)
    shown.insert(0, "Rang", range(1, len(top) + 1))
    event = st.dataframe(
        shown,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="top5",
        width="stretch",
    )
    st.caption("Cliquez sur une simulation pour afficher ses positions.")
    picked = event.selection.rows if event and event.selection else []
    if len(top):
        sim = top.iloc[picked[0] if picked else 0]
        sim_detail(int(sim["sim_id"]), sim)


def sim_detail(sim_id: int, sim: pd.Series) -> None:
    chips = [
        ("Calibrage", sim["Calibrage"]),
        ("Famille", sim["Famille"]),
        ("Filtre 1 j", sim["Filtre 1 j"]),
        ("Type de filtre", sim["Type de filtre"]),
        ("Stop", sim["Stop"]),
        ("Trailing", sim["Trailing"]),
        ("Take profit", sim["Take profit"]),
        ("Ordres", sim["Ordres"]),
        ("Dimensionnement", sim["Dimensionnement"]),
        ("% max par crypto", sim["% max par crypto"]),
        ("Exposition max", sim["Exposition max"]),
        ("Max par entrée", sim["Max par entrée"]),
        ("Positions max", sim["Positions max"]),
    ]
    st.markdown(f"#### Positions ouvertes de {fmt.sim_label(sim_id)}")
    st.caption(" · ".join(f"{label} : **{value}**" for label, value in chips))
    live_positions(sim_id)


@st.fragment(run_every=60)
def live_positions(sim_id: int) -> None:
    conn = ro()
    prices = latest_prices(conn)
    taker = settings().fee_taker
    details = positions_detail(conn, sim_id, prices, taker)
    if details:
        st.dataframe(
            pd.DataFrame(
                {
                    "Actif": [d.market.split("-")[0] for d in details],
                    "Lots": [d.lots for d in details],
                    "Quantité": [fmt.quantity(d.quantity) for d in details],
                    "Prix d'entrée moyen": [fmt.price(d.avg_entry_price) for d in details],
                    "Cours actuel": [fmt.price(d.price) for d in details],
                    "Coût investi": [fmt.eur(d.invested_cost) for d in details],
                    "Valeur": [fmt.eur(d.value) for d in details],
                    "Frais achat": [fmt.eur(d.buy_fees) for d in details],
                    "Frais vente (est.)": [fmt.eur(d.sell_fees_estimate) for d in details],
                    "P&L latent": [fmt.signed_eur(d.unrealized_pnl) for d in details],
                    "Stop": [fmt.price(d.stop) for d in details],
                    "Trailing": [fmt.price(d.trailing) for d in details],
                    "Take profit": [fmt.price(d.take_profit) for d in details],
                }
            ),
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("Aucune position ouverte.")
    cash = cash_balance(conn, sim_id)
    in_positions = sum((d.value or 0 for d in details), 0)
    sell_fees = sum((d.sell_fees_estimate or 0 for d in details), 0)
    st.markdown(
        f"Valeur totale **{fmt.eur(cash + in_positions - sell_fees)}** · en positions "
        f"**{fmt.eur(in_positions)}** · liquidités **{fmt.eur(cash)}**"
    )
    updated = conn.execute("SELECT MAX(ts) FROM latest_prices").fetchone()[0]
    st.caption(
        f"Cours Bitvavo au {fmt.timestamp(updated)}, rafraîchis chaque minute ; frais de vente "
        f"estimés au taux taker de {fmt.pct(float(taker) * 100, 2)}."
    )


def buy_and_hold_table() -> None:
    st.subheader("Buy & hold à poids égaux")
    conn = ro()
    details = buy_and_hold_detail(
        load_buy_and_hold(conn), latest_prices(conn), settings().fee_taker
    )
    if not details:
        st.info("Le buy & hold est acheté au lancement des simulations.")
        return
    st.dataframe(
        pd.DataFrame(
            {
                "Actif": [d.market.split("-")[0] for d in details],
                "Paire": [d.market for d in details],
                "Poids": [fmt.share(d.weight) for d in details],
                "Prix au départ": [fmt.price(d.start_price) for d in details],
                "Cours actuel": [fmt.price(d.price) for d in details],
                "Variation": [fmt.pct(d.change_pct, signed=True) for d in details],
                "Quantité": [fmt.quantity(d.quantity) for d in details],
                "Valeur nette": [fmt.eur(d.net_value) for d in details],
                "P&L": [fmt.signed_eur(d.pnl_eur) for d in details],
            }
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "1 000 € répartis à parts égales entre les paires du panier au lancement, frais d'achat "
        "taker déduits ; valeur nette des frais de vente estimés."
    )


SORT_KEYS = {
    "P&L": "pnl_eur",
    "Drawdown max": "dd_pct",
    "Trades": "trades",
    "Réussite": "win_rate",
    "Frais": "fees",
    "vs buy & hold": "bh_gap",
    "Simulation": "sim_id",
}


def all_simulations(table: pd.DataFrame) -> None:
    st.subheader("Toutes les simulations")
    cols = st.columns([2, 3, 2, 1])
    mode = cols[0].radio(
        "Calibrage",
        ["Toutes", "Typiques", "Optimisées"],
        horizontal=True,
        key="sims_mode",
        label_visibility="collapsed",
    )
    search = cols[1].text_input(
        "Recherche",
        placeholder="Rechercher (S-042, MACD, Chandelier…)",
        key="sims_search",
        label_visibility="collapsed",
    )
    sort = cols[2].selectbox(
        "Trier par", list(SORT_KEYS), key="sims_sort", label_visibility="collapsed"
    )
    descending = cols[3].toggle("Décroissant", value=True, key="sims_desc")
    view = table
    if mode != "Toutes":
        view = view[view["Calibrage"] == ("Typique" if mode == "Typiques" else "Optimisé")]
    if search:
        text = view.astype(str).apply(" ".join, axis=1).str.lower()
        view = view[text.str.contains(search.lower(), regex=False)]
    view = view.sort_values(SORT_KEYS[sort], ascending=not descending, na_position="last")
    shown = metrics_columns(view)
    shown.insert(1, "Paire", view["Paire"].to_numpy())
    shown.insert(4, "Filtre 1 j", view["Filtre 1 j"].to_numpy())
    shown.insert(5, "Trailing", view["Trailing"].to_numpy())
    shown.insert(6, "Take profit", view["Take profit"].to_numpy())
    st.dataframe(shown, hide_index=True, width="stretch", height=420)
    st.caption(f"{len(view)} simulations affichées.")


def tab_follow_up() -> None:
    version = data_version()
    table = simulations(version)
    if table.empty:
        st.warning("Aucun plan de simulations : `python -m mcs.sampling generate`.")
        return
    summary, rows, _ = analysis(version)
    if summary is None:
        st.info(
            "Les métriques apparaîtront à la première heure pleine après le lancement des "
            "simulations."
        )
    else:
        kpis(summary)
        value_chart(table)
        tornado_chart(rows, summary.pnl_mean)
        top5(table)
        buy_and_hold_table()
    all_simulations(table)


# --- Onglet « Paramètres des indicateurs » ---------------------------------------------------


def tab_indicators() -> None:
    st.subheader("Valeurs et poids des indicateurs par simulation")
    st.caption(
        "Score d'entrée = somme des poids des indicateurs qui donnent un signal d'achat sur la "
        "bougie 4 h clôturée ; achat si le signal principal est en achat, si le score atteint le "
        "seuil et si le filtre de fond 1 j est favorable (bloquant, sans poids). Pour les "
        "simulations optimisées, les valeurs viennent de l'optimisation sur 70 % de l'historique, "
        "validée sur 30 % ; le point « • » signale une valeur différente du réglage typique."
    )
    table = indicator_table(data_version())
    if table.empty:
        st.warning("Aucun plan de simulations.")
        return
    cols = st.columns([2, 2, 3])
    mode = cols[0].radio(
        "Calibrage",
        ["Toutes", "Typiques", "Optimisées"],
        horizontal=True,
        key="ind_mode",
        label_visibility="collapsed",
    )
    role = cols[1].selectbox(
        "Rôle",
        ["Tous les rôles", *queries.ROLE_LABELS.values()],
        key="ind_role",
        label_visibility="collapsed",
    )
    search = cols[2].text_input(
        "Recherche",
        placeholder="Rechercher (S-042, RSI, SMA 200…)",
        key="ind_search",
        label_visibility="collapsed",
    )
    view = table
    if mode != "Toutes":
        view = view[view["Calibrage"] == ("Typique" if mode == "Typiques" else "Optimisé")]
    if role != "Tous les rôles":
        view = view[view["Rôle"] == role]
    if search:
        text = view.astype(str).apply(" ".join, axis=1).str.lower()
        view = view[text.str.contains(search.lower(), regex=False)]
    shown = view.drop(columns="differs").copy()
    shown["Poids"] = shown["Poids"].map(lambda v: "—" if pd.isna(v) else fmt.number(v, 3))
    shown["Seuil du score"] = shown["Seuil du score"].map(
        lambda v: "—" if pd.isna(v) else fmt.number(v, 3)
    )
    st.dataframe(shown, hide_index=True, width="stretch", height=560)
    st.caption(
        f"{len(view)} lignes · {int(view['differs'].sum())} valeurs optimisées différentes du typique."
    )


# --- Onglet « Marchés du panier » -----------------------------------------------------------


def tab_markets() -> None:
    st.subheader("Marchés du panier : historique, spread et sélection")
    taker = settings().fee_taker
    st.caption(
        "Le filtre de fond et l'optimisation 70 / 30 exigent au moins 200 jours de bougies "
        "journalières : par défaut, seules les paires qui les atteignent sont cochées. Le spread "
        "est la médiane sur 24 h des écarts bid / ask ; le coût d'un aller-retour ajoute deux fois "
        f"le taux taker ({fmt.pct(float(taker) * 100, 2)}) au spread."
    )
    table = queries.markets_table(ro(), taker)
    if table.empty:
        st.warning("Aucun marché : `python -m mcs.collector markets`.")
        return
    display = table.copy()
    for column, formatter in (
        ("Spread estimé", lambda v: fmt.pct(v, 3)),
        ("Coût aller-retour", lambda v: fmt.pct(v, 2)),
        ("Volume 24 h", lambda v: fmt.eur(v, 0)),
        ("Volatilité (ATR 4 h)", lambda v: fmt.pct(v, 2)),
    ):
        display[column] = display[column].map(lambda v, f=formatter: "—" if pd.isna(v) else f(v))
    display["Historique 1 j"] = display["Historique 1 j"].map(
        lambda v: "—" if pd.isna(v) else f"{int(v)} j"
    )
    edited = st.data_editor(
        display,
        hide_index=True,
        width="stretch",
        key="markets_editor",
        disabled=[c for c in display.columns if c != "Prendre en compte"],
        column_config={"Prendre en compte": st.column_config.CheckboxColumn()},
    )
    selected = edited.loc[edited["Prendre en compte"], "Paire"].tolist()
    short = edited.loc[
        edited["Prendre en compte"] & (edited["200 jours"] == "Insuffisant"), "Paire"
    ]
    if len(short):
        st.warning(
            f"Moins de 200 jours d'historique : {', '.join(short)}. Ces paires restent exclues des "
            "signaux tant que l'historique n'est pas atteint (décision 2)."
        )
    st.code("CRYPTO_BASKET=" + ",".join(selected), language=None)
    info = queries.engine_info(ro())
    if st.button("Valider la sélection", key="btn_validate", type="primary", disabled=not selected):
        queries.save_selection(rw(), selected)
        later = " après une remise à zéro" if info.status != "NOT_STARTED" else ""
        st.success(
            f"Sélection enregistrée ({len(selected)} paires) : elle s'appliquera au prochain lancement{later}."
        )


# --- Onglet « Graphiques » ----------------------------------------------------------------


def tab_charts() -> None:
    info = queries.engine_info(ro())
    state = ro().execute("SELECT basket_json FROM engine_state").fetchone()[0]
    if info.status != "NOT_STARTED" and state:
        markets = json.loads(state)
    else:
        markets = [
            r[0]
            for r in ro().execute(
                "SELECT market FROM markets WHERE in_basket = 1 AND selected = 1 ORDER BY market"
            )
        ]
    chart_tab.render(ro(), markets, data_version())


# --- Santé du moteur -----------------------------------------------------------------------


@st.cache_data(ttl=60, show_spinner=False)
def health_checks() -> list[Any]:
    return run_checks(ro(), settings().log_dir, hours=72)


def health_panel() -> None:
    checks = health_checks()
    worst = max((c.level for c in checks), key=[Level.OK, Level.WARN, Level.FAIL].index)
    icon = {Level.OK: "✅", Level.WARN: "⚠️", Level.FAIL: "❌"}
    with st.expander(f"{icon[worst]} Santé du moteur (72 dernières heures) : {worst.value}"):
        for check in checks:
            st.markdown(f"{icon[check.level]} **{check.section} · {check.name}** : {check.detail}")
        st.caption("Rapport complet : `python -m mcs.health`. Recalculé chaque minute.")


# --- Page -----------------------------------------------------------------------------------


def main() -> None:
    st.title("Simulations Monte-Carlo crypto")
    st.caption(
        "Paper trading live sur données Bitvavo : 300 simulations (150 paires typique / "
        "optimisé), départ à 1 000 € chacune. Aucun ordre réel."
    )
    control_bar()
    health_panel()
    follow, indicators, markets, graphs = st.tabs(
        ["Suivi des simulations", "Paramètres des indicateurs", "Marchés du panier", "Graphiques"]
    )
    with follow:
        tab_follow_up()
    with indicators:
        tab_indicators()
    with markets:
        tab_markets()
    with graphs:
        tab_charts()


main()
