"""Registro de sinais e paper trading (operações simuladas) em SQLite."""
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    candle_ts TEXT NOT NULL,
    candidate TEXT,
    acao TEXT,
    confianca TEXT,
    resumo TEXT,
    preco REAL,
    stop_loss REAL,
    alvo REAL,
    alerted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    entry REAL NOT NULL,
    qty REAL NOT NULL,
    stop_loss REAL,
    alvo REAL,
    closed_at TEXT,
    exit REAL,
    exit_reason TEXT,
    pnl_usd REAL,
    pnl_pct REAL
);
"""

CONF_ORDER = {"baixa": 0, "media": 1, "alta": 2}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def confidence_ok(conf: str) -> bool:
    return CONF_ORDER.get(conf, 0) >= CONF_ORDER.get(config.MIN_CONFIDENCE, 1)


# ---------- sinais ----------

def last_signal_candle(symbol: str) -> str | None:
    with conn() as c:
        row = c.execute("SELECT candle_ts FROM signals WHERE symbol=? ORDER BY id DESC LIMIT 1",
                        (symbol,)).fetchone()
    return row["candle_ts"] if row else None


def save_signal(symbol: str, candle_ts: str, candidate: dict | None, decision: dict | None,
                price: float, alerted: bool) -> None:
    d = decision or {}
    with conn() as c:
        c.execute(
            "INSERT INTO signals (created_at, symbol, candle_ts, candidate, acao, confianca, resumo,"
            " preco, stop_loss, alvo, alerted) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), symbol, candle_ts, str(candidate) if candidate else None, d.get("acao"),
             d.get("confianca"), d.get("resumo"), price, d.get("stop_loss"), d.get("alvo"),
             int(alerted)),
        )


# ---------- posições simuladas ----------

def open_position(symbol: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM trades WHERE symbol=? AND closed_at IS NULL", (symbol,)).fetchone()
    return dict(row) if row else None


def open_trade(symbol: str, price: float, stop: float, alvo: float) -> dict:
    qty = config.PAPER_TRADE_USD / price
    with conn() as c:
        c.execute("INSERT INTO trades (symbol, opened_at, entry, qty, stop_loss, alvo) VALUES (?,?,?,?,?,?)",
                  (symbol, _now(), price, qty, stop or None, alvo or None))
    return open_position(symbol)


def close_trade(trade: dict, price: float, reason: str) -> dict:
    fee = config.PAPER_FEE_PCT / 100
    cost = trade["entry"] * trade["qty"] * (1 + fee)
    proceeds = price * trade["qty"] * (1 - fee)
    pnl = proceeds - cost
    pnl_pct = pnl / cost * 100
    with conn() as c:
        c.execute("UPDATE trades SET closed_at=?, exit=?, exit_reason=?, pnl_usd=?, pnl_pct=? WHERE id=?",
                  (_now(), price, reason, pnl, pnl_pct, trade["id"]))
    return {**trade, "exit": price, "exit_reason": reason, "pnl_usd": pnl, "pnl_pct": pnl_pct}


def check_stops(symbol: str, low: float, high: float) -> dict | None:
    """Fecha a posição se a vela tocou o stop ou o alvo (stop tem prioridade, por conservadorismo)."""
    t = open_position(symbol)
    if not t:
        return None
    if t["stop_loss"] and low <= t["stop_loss"]:
        return close_trade(t, t["stop_loss"], "stop loss")
    if t["alvo"] and high >= t["alvo"]:
        return close_trade(t, t["alvo"], "alvo atingido")
    return None


def all_open() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM trades WHERE closed_at IS NULL ORDER BY id")]


def performance() -> dict:
    with conn() as c:
        closed = [dict(r) for r in c.execute("SELECT * FROM trades WHERE closed_at IS NOT NULL")]
        n_signals = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        n_alerts = c.execute("SELECT COUNT(*) FROM signals WHERE alerted=1").fetchone()[0]
    wins = [t for t in closed if t["pnl_usd"] > 0]
    losses = [t for t in closed if t["pnl_usd"] <= 0]
    total = sum(t["pnl_usd"] for t in closed)
    return {
        "analises": n_signals,
        "alertas": n_alerts,
        "trades_fechados": len(closed),
        "ganhos": len(wins),
        "perdas": len(losses),
        "taxa_acerto": (len(wins) / len(closed) * 100) if closed else 0.0,
        "pnl_total_usd": total,
        "ganho_medio_usd": (sum(t["pnl_usd"] for t in wins) / len(wins)) if wins else 0.0,
        "perda_media_usd": (sum(t["pnl_usd"] for t in losses) / len(losses)) if losses else 0.0,
    }
