"""Banco SQLite: sinais, placar, gatilhos, perfil de risco e paper trading (operações simuladas)."""
import sqlite3
from datetime import datetime, timedelta, timezone

import config
from profiles import DEFAULT_PROFILE, PROFILES

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
CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS dex_tokens (
    symbol TEXT PRIMARY KEY,   -- ex.: ZCAT/USD
    network TEXT NOT NULL,     -- ex.: solana
    token TEXT NOT NULL,       -- endereço do contrato
    pool TEXT NOT NULL,        -- pool com mais volume (fonte das velas)
    name TEXT,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS triggers (
    symbol TEXT PRIMARY KEY,
    up REAL,
    down REAL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

# Colunas adicionadas depois da primeira versão (o banco existente é migrado automaticamente)
MIGRATIONS = {
    "signals": {
        "cand_dir": "TEXT",          # direção do sinal candidato (COMPRA/VENDA) ou NULL
        "source": "TEXT DEFAULT 'auto'",  # auto | gatilho | manual
        "perfil": "TEXT",
        "vies": "INTEGER",
        "gatilho_alta": "REAL",
        "gatilho_baixa": "REAL",
        "ret_24h": "REAL",           # variação % do preço 24h depois da análise
        "ret_72h": "REAL",
        "eval_24h": "INTEGER DEFAULT 0",  # 1 = já conferido (mesmo que sem dado)
        "eval_72h": "INTEGER DEFAULT 0",
    },
    "trades": {"perfil": "TEXT"},
    "triggers": {"origin": "TEXT DEFAULT 'ia'"},  # ia (níveis da análise) | auto (faixa calculada)
}

CONF_ORDER = {"baixa": 0, "media": 1, "alta": 2}
_migrated = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate(c: sqlite3.Connection) -> None:
    for table, cols in MIGRATIONS.items():
        existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if col not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    # Sinais antigos: recupera a direção a partir do texto salvo
    c.execute("UPDATE signals SET cand_dir='COMPRA' WHERE cand_dir IS NULL AND candidate LIKE '%''direcao'': ''COMPRA''%'")
    c.execute("UPDATE signals SET cand_dir='VENDA' WHERE cand_dir IS NULL AND candidate LIKE '%''direcao'': ''VENDA''%'")


def conn() -> sqlite3.Connection:
    global _migrated
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    if not _migrated:
        c.executescript(SCHEMA)
        with c:
            _migrate(c)
        _migrated = True
    return c


# ---------- meta (chave/valor) ----------

def get_meta(key: str) -> str | None:
    with conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


# ---------- tokens de DEX ----------

def add_dex(info: dict) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO dex_tokens (symbol, network, token, pool, name, added_at) "
                  "VALUES (?,?,?,?,?,?)",
                  (info["symbol"], info["network"], info["token"], info["pool"], info.get("name"), _now()))


def get_dex(symbol: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM dex_tokens WHERE symbol=?", (symbol,)).fetchone()
    return dict(row) if row else None


def fee_pct(symbol: str) -> float:
    """Taxa por lado: tokens de DEX custam bem mais (taxa do app + slippage)."""
    return config.DEX_FEE_PCT if get_dex(symbol) else config.PAPER_FEE_PCT


# ---------- perfil de risco ----------

def get_profile() -> str:
    with conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key='perfil'").fetchone()
    name = row["value"] if row else config.RISK_PROFILE
    return name if name in PROFILES else DEFAULT_PROFILE


def set_profile(name: str) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('perfil', ?)", (name,))


def min_confidence() -> str:
    """MIN_CONFIDENCE do .env, se definido; senão o padrão do perfil."""
    return config.MIN_CONFIDENCE or PROFILES[get_profile()]["min_conf"]


def confidence_ok(conf: str) -> bool:
    return CONF_ORDER.get(conf, 0) >= CONF_ORDER.get(min_confidence(), 1)


# ---------- lista de moedas monitoradas ----------

def get_watchlist() -> list[str]:
    """Moedas monitoradas. Na primeira execução, copia a lista SYMBOLS do .env para o banco."""
    with conn() as c:
        if not c.execute("SELECT 1 FROM meta WHERE key='watchlist_seeded'").fetchone():
            c.executemany("INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES (?, ?)",
                          [(s, _now()) for s in config.SYMBOLS])
            c.execute("INSERT INTO meta (key, value) VALUES ('watchlist_seeded', ?)", (_now(),))
        return [r["symbol"] for r in c.execute("SELECT symbol FROM watchlist ORDER BY added_at, symbol")]


def add_symbol(symbol: str) -> bool:
    """Adiciona; retorna False se já estava na lista."""
    get_watchlist()  # garante a lista inicial
    with conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES (?, ?)", (symbol, _now()))
        return cur.rowcount > 0


def remove_symbol(symbol: str) -> bool:
    """Remove; retorna False se não estava na lista."""
    get_watchlist()
    clear_trigger(symbol)
    with conn() as c:
        return c.execute("DELETE FROM watchlist WHERE symbol=?", (symbol,)).rowcount > 0


# ---------- sinais ----------

def last_signal_candle(symbol: str) -> str | None:
    """Vela da última análise automática (análises manuais não contam para o cooldown)."""
    with conn() as c:
        row = c.execute("SELECT candle_ts FROM signals WHERE symbol=? AND COALESCE(source,'auto') != 'manual'"
                        " ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
    return row["candle_ts"] if row else None


def recent_analyses(symbol: str, limit: int = 4, hours: int = 72) -> list[dict]:
    """Últimas análises da IA para o ativo (mais antiga primeiro), para dar memória à próxima."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT created_at, source, acao, confianca, vies, preco, resumo, gatilho_alta, gatilho_baixa"
            " FROM signals WHERE symbol=? AND acao IS NOT NULL AND created_at >= ?"
            " ORDER BY id DESC LIMIT ?", (symbol, since, limit))]
    return rows[::-1]


def save_signal(symbol: str, candle_ts: str, candidate: dict | None, decision: dict | None,
                price: float, alerted: bool, source: str = "auto") -> None:
    d = decision or {}
    with conn() as c:
        c.execute(
            "INSERT INTO signals (created_at, symbol, candle_ts, candidate, cand_dir, acao, confianca, resumo,"
            " preco, stop_loss, alvo, alerted, source, perfil, vies, gatilho_alta, gatilho_baixa)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), symbol, candle_ts, str(candidate) if candidate else None,
             candidate["direcao"] if candidate else None, d.get("acao"), d.get("confianca"),
             d.get("resumo"), price, d.get("stop_loss"), d.get("alvo"), int(alerted), source,
             get_profile(), d.get("vies"), d.get("gatilho_alta"), d.get("gatilho_baixa")),
        )


# ---------- gatilhos de preço ----------

def last_analysis_at(symbol: str) -> datetime | None:
    """Quando a IA analisou a moeda pela última vez (qualquer origem)."""
    with conn() as c:
        row = c.execute("SELECT created_at FROM signals WHERE symbol=? ORDER BY id DESC LIMIT 1",
                        (symbol,)).fetchone()
    return datetime.fromisoformat(row["created_at"]) if row else None


def set_trigger(symbol: str, price: float, up: float, down: float, min_dist: float = 0,
                origin: str = "ia", ttl_hours: int | None = None) -> dict | None:
    """Salva os níveis da última análise (substitui os anteriores).

    Ignora níveis do lado errado do preço, mais perto que min_dist (disparariam com ruído) ou
    absurdamente longe (a IA às vezes erra a escala em moedas de preço muito baixo).
    """
    up = up if up and price + min_dist <= up <= price * 1.5 else None
    down = down if down and price * 0.5 <= down <= price - min_dist else None
    with conn() as c:
        c.execute("DELETE FROM triggers WHERE symbol=?", (symbol,))
        if not up and not down:
            return None
        expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours or config.TRIGGER_TTL_HOURS)
        c.execute("INSERT INTO triggers (symbol, up, down, created_at, expires_at, origin) VALUES (?,?,?,?,?,?)",
                  (symbol, up, down, _now(), expires.isoformat(timespec="seconds"), origin))
    return {"up": up, "down": down, "origin": origin}


def get_trigger(symbol: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM triggers WHERE symbol=?", (symbol,)).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        clear_trigger(symbol)
        return None
    return dict(row)


def clear_trigger(symbol: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM triggers WHERE symbol=?", (symbol,))


def all_triggers() -> list[dict]:
    with conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM triggers ORDER BY symbol")]
    now = datetime.now(timezone.utc)
    return [r for r in rows if datetime.fromisoformat(r["expires_at"]) >= now]


# ---------- placar (o que o preço fez depois de cada análise) ----------

def pending_evaluations(hours: int, limit: int = 30) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with conn() as c:
        return [dict(r) for r in c.execute(
            f"SELECT id, symbol, created_at, preco FROM signals WHERE eval_{hours}h=0 AND acao IS NOT NULL"
            " AND preco > 0 AND created_at <= ? ORDER BY id LIMIT ?", (cutoff, limit))]


def set_evaluation(signal_id: int, hours: int, ret_pct: float | None) -> None:
    with conn() as c:
        c.execute(f"UPDATE signals SET ret_{hours}h=?, eval_{hours}h=1 WHERE id=?", (ret_pct, signal_id))


def _stats(rows: list, col: str, fee_round_trip: float) -> dict | None:
    vals = [r[col] for r in rows if r[col] is not None]
    if not vals:
        return None
    return {"n": len(vals), "media": sum(vals) / len(vals),
            "lucro_pct": sum(v > fee_round_trip for v in vals) / len(vals) * 100}


def scoreboard(perfil: str | None = None) -> dict:
    fee = 2 * config.PAPER_FEE_PCT  # ida e volta: só conta como lucro o que passa das taxas
    where, args = "WHERE acao IS NOT NULL", []
    if perfil:
        where += " AND perfil=?"
        args.append(perfil)
    with conn() as c:
        rows = [dict(r) for r in c.execute(f"SELECT * FROM signals {where}", args)]
    buy_cands = [r for r in rows if r["cand_dir"] == "COMPRA"]
    approved = [r for r in buy_cands if r["acao"] == "COMPRA"]
    rejected = [r for r in buy_cands if r["acao"] != "COMPRA"]
    bull = [r for r in rows if (r["vies"] or 0) >= 30]
    bear = [r for r in rows if (r["vies"] or 0) <= -30]

    def hit_rate(group: list, sign: int) -> float | None:
        vals = [r["ret_24h"] for r in group if r["ret_24h"] is not None]
        return (sum((v > 0) == (sign > 0) for v in vals) / len(vals) * 100) if vals else None

    return {
        "total": len(rows),
        "avaliados_24h": sum(r["ret_24h"] is not None for r in rows),
        "aprovadas": {h: _stats(approved, f"ret_{h}h", fee) for h in (24, 72)},
        "descartadas": {h: _stats(rejected, f"ret_{h}h", fee) for h in (24, 72)},
        "vies_alta": {**{h: _stats(bull, f"ret_{h}h", fee) for h in (24, 72)}, "acerto": hit_rate(bull, 1)},
        "vies_baixa": {**{h: _stats(bear, f"ret_{h}h", fee) for h in (24, 72)}, "acerto": hit_rate(bear, -1)},
        "fee": fee,
    }


# ---------- posições simuladas ----------

def open_position(symbol: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM trades WHERE symbol=? AND closed_at IS NULL", (symbol,)).fetchone()
    return dict(row) if row else None


def open_trade(symbol: str, price: float, stop: float, alvo: float) -> dict:
    qty = config.PAPER_TRADE_USD / price
    with conn() as c:
        c.execute("INSERT INTO trades (symbol, opened_at, entry, qty, stop_loss, alvo, perfil) VALUES (?,?,?,?,?,?,?)",
                  (symbol, _now(), price, qty, stop or None, alvo or None, get_profile()))
    return open_position(symbol)


def close_trade(trade: dict, price: float, reason: str) -> dict:
    fee = fee_pct(trade["symbol"]) / 100
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
    by_profile = {}
    for t in closed:
        p = by_profile.setdefault(t.get("perfil") or "conservador", {"n": 0, "pnl": 0.0})
        p["n"] += 1
        p["pnl"] += t["pnl_usd"]
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
        "por_perfil": by_profile,
    }
