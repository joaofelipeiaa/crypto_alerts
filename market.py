"""Dados de mercado (via ccxt) e indicadores técnicos."""
import logging
import time

import ccxt
import pandas as pd
import requests

import config
import paper

log = logging.getLogger(__name__)

_exchange = getattr(ccxt, config.EXCHANGE)({"enableRateLimit": True})


# ---------- tokens de DEX (GeckoTerminal, gratuito, ~30 chamadas/min) ----------

GT_API = "https://api.geckoterminal.com/api/v2"
_GT_NATIVE = {"1m": ("minute", 1), "5m": ("minute", 5), "15m": ("minute", 15),
              "1h": ("hour", 1), "4h": ("hour", 4), "12h": ("hour", 12), "1d": ("day", 1)}
_gt_last_call = 0.0


def _gt(path: str, **params) -> dict:
    global _gt_last_call
    for attempt in range(3):
        wait = 2.2 - (time.monotonic() - _gt_last_call)  # respeita o limite de ~30/min
        if wait > 0:
            time.sleep(wait)
        _gt_last_call = time.monotonic()
        r = requests.get(GT_API + path, params=params, timeout=20,
                         headers={"Accept": "application/json;version=20230302"})
        if r.status_code == 429:
            time.sleep(15 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("GeckoTerminal: limite de requisições")


def find_dex(base: str) -> tuple[dict | None, str]:
    """Procura um token pelo código nas DEX. Retorna (info, motivo_da_recusa)."""
    base = base.upper()
    d = _gt("/search/pools", query=base, include="base_token")
    tokens = {t["id"]: t["attributes"] for t in d.get("included", [])}
    by_token: dict[str, dict] = {}
    for p in d.get("data", []):
        tid = p["relationships"]["base_token"]["data"]["id"]
        if tokens.get(tid, {}).get("symbol", "").upper() != base:
            continue
        vol = float(p["attributes"]["volume_usd"]["h24"] or 0)
        entry = by_token.setdefault(tid, {"vol": 0.0, "pool": None, "pool_vol": -1.0})
        entry["vol"] += vol
        if vol > entry["pool_vol"]:
            entry["pool"], entry["pool_vol"] = p["attributes"]["address"], vol
    if not by_token:
        return None, "não encontrado em DEX"
    tid, best = max(by_token.items(), key=lambda kv: kv[1]["vol"])  # evita cópias com o mesmo código
    network, token = tid.rsplit("_", 1)
    attrs = _gt(f"/networks/{network}/tokens/{token}")["data"]["attributes"]
    volume = float(attrs["volume_usd"]["h24"] or 0)
    info = {"symbol": f"{base}/USD", "network": network, "token": token, "pool": best["pool"],
            "name": attrs.get("name") or base, "volume": volume,
            "market_cap": float(attrs.get("market_cap_usd") or attrs.get("fdv_usd") or 0)}
    if volume < config.DEX_MIN_VOLUME_USD:
        return info, (f"volume de 24h baixo demais (US$ {volume:,.0f}; mínimo US$ "
                      f"{config.DEX_MIN_VOLUME_USD:,.0f}), fácil de manipular")
    return info, ""


def _dex_plan(timeframe: str) -> tuple[str, int]:
    """Timeframe nativo do GeckoTerminal e quantas velas dele formam uma do timeframe pedido."""
    if timeframe in _GT_NATIVE:
        return timeframe, 1
    secs = ccxt.Exchange.parse_timeframe(timeframe)
    for tf in sorted(_GT_NATIVE, key=ccxt.Exchange.parse_timeframe, reverse=True):
        base = ccxt.Exchange.parse_timeframe(tf)
        if base <= secs and secs % base == 0:
            return tf, secs // base
    raise ValueError(f"timeframe {timeframe} sem equivalente no GeckoTerminal")


def _dex_candles(dex: dict, timeframe: str, limit: int) -> pd.DataFrame:
    base_tf, factor = _dex_plan(timeframe)
    unit, agg = _GT_NATIVE[base_tf]
    raw = _gt(f"/networks/{dex['network']}/pools/{dex['pool']}/ohlcv/{unit}", aggregate=agg,
              limit=min(1000, (limit + 1) * factor), currency="usd", token=dex["token"])
    rows = raw["data"]["attributes"]["ohlcv_list"]
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).sort_values("ts")
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.drop_duplicates("ts").set_index("ts")
    # intervalos sem negócio não vêm na resposta: preenche com o último preço e volume zero
    base_secs = ccxt.Exchange.parse_timeframe(base_tf)
    df = df.reindex(pd.date_range(df.index[0], df.index[-1], freq=f"{base_secs}s"))
    df["close"] = df["close"].ffill()
    for col in ("open", "high", "low"):
        df[col] = df[col].fillna(df["close"])
    df["volume"] = df["volume"].fillna(0)
    tf_secs = ccxt.Exchange.parse_timeframe(timeframe)
    if factor > 1:
        df = df.resample(f"{tf_secs}s", origin="epoch", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    df = df.rename_axis("ts").reset_index()
    # só velas fechadas
    df = df[df["ts"] + pd.Timedelta(seconds=tf_secs) <= pd.Timestamp.now(tz="UTC")]
    return df.tail(limit).reset_index(drop=True)


# ---------- dados de mercado (corretora ou DEX) ----------

def fetch_candles(symbol: str, timeframe: str = config.TIMEFRAME, limit: int = 250) -> pd.DataFrame:
    """Busca velas e descarta a última (ainda aberta), usando só velas fechadas."""
    dex = paper.get_dex(symbol)
    if dex:
        return _dex_candles(dex, timeframe, limit)
    raw = _exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.iloc[:-1].reset_index(drop=True)


def symbol_exists(symbol: str) -> bool:
    """Confere se o par existe e está ativo na corretora."""
    markets = _exchange.load_markets()  # o ccxt guarda em cache após a primeira chamada
    m = markets.get(symbol)
    return bool(m) and m.get("active") is not False


def price_at(symbol: str, ts_ms: int) -> float | None:
    """Preço (abertura da vela de 1h) no momento indicado. None se a corretora não tiver o dado."""
    dex = paper.get_dex(symbol)
    if dex:
        raw = _gt(f"/networks/{dex['network']}/pools/{dex['pool']}/ohlcv/hour", aggregate=1, limit=3,
                  before_timestamp=ts_ms // 1000 + 7200, currency="usd", token=dex["token"])
        rows = raw["data"]["attributes"]["ohlcv_list"]
        best = min(rows, key=lambda r: abs(r[0] * 1000 - ts_ms), default=None)
        return float(best[1]) if best and abs(best[0] * 1000 - ts_ms) <= 2 * 3600 * 1000 else None
    candles = _exchange.fetch_ohlcv(symbol, "1h", since=ts_ms, limit=1)
    if candles and abs(candles[0][0] - ts_ms) <= 2 * 3600 * 1000:
        return float(candles[0][1])
    return None


def last_price(symbol: str) -> float:
    dex = paper.get_dex(symbol)
    if dex:
        data = _gt(f"/networks/{dex['network']}/tokens/{dex['token']}")["data"]["attributes"]
        return float(data["price_usd"])
    return float(_exchange.fetch_ticker(symbol)["last"])


def dex_context(symbol: str) -> dict | None:
    """Liquidez e volume do token de DEX, para a IA pesar o risco."""
    dex = paper.get_dex(symbol)
    if not dex:
        return None
    a = _gt(f"/networks/{dex['network']}/tokens/{dex['token']}")["data"]["attributes"]
    return {"rede": dex["network"], "nome": dex["name"],
            "volume_24h_usd": round(float(a["volume_usd"]["h24"] or 0)),
            "liquidez_usd": round(float(a.get("total_reserve_in_usd") or 0)),
            "valor_de_mercado_usd": round(float(a.get("market_cap_usd") or a.get("fdv_usd") or 0))}


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]

    # RSI 14 (Wilder)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss)

    # Médias móveis
    df["ema9"] = close.ewm(span=9, adjust=False).mean()
    df["ema21"] = close.ewm(span=21, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    df["ema200"] = close.ewm(span=200, adjust=False).mean()

    # MACD 12/26/9
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    df["macd"] = macd
    df["macd_signal"] = macd.ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # Bandas de Bollinger 20/2
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    df["bb_upper"] = sma20 + 2 * std20
    df["bb_lower"] = sma20 - 2 * std20

    # ATR 14 (volatilidade, usado para stop)
    prev_close = close.shift()
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()

    # Volume relativo à média de 20 velas
    df["vol_ratio"] = df["volume"] / df["volume"].rolling(20).mean()
    return df


# Tempo gráfico de contexto para cada timeframe (o da Coinbase não tem 4h)
HIGHER_TF = {"1m": "15m", "5m": "30m", "15m": "1h", "30m": "2h", "1h": "6h", "2h": "1d", "6h": "1d"}


def higher_tf_context(symbol: str, timeframe: str = config.TIMEFRAME) -> dict | None:
    """Tendência no tempo gráfico maior, para a IA não operar contra ela sem saber."""
    htf = HIGHER_TF.get(timeframe)
    if not htf:
        return None
    try:
        df = add_indicators(fetch_candles(symbol, htf))
    except Exception:  # noqa: BLE001
        return None
    last = df.iloc[-1]
    close = float(last["close"])
    return {
        "timeframe": htf,
        "preco_fechamento": round(close, 6),
        "rsi": round(float(last["rsi"]), 1),
        "acima_ema50": bool(close > last["ema50"]),
        "acima_ema200": bool(close > last["ema200"]),
        "ema50": round(float(last["ema50"]), 6),
        "ema200": round(float(last["ema200"]), 6),
        "macd_hist": round(float(last["macd_hist"]), 6),
        "variacao_12_velas_pct": round((close / float(df.iloc[-13]["close"]) - 1) * 100, 2),
        "maxima_30_velas": round(float(df.tail(30)["high"].max()), 6),
        "minima_30_velas": round(float(df.tail(30)["low"].min()), 6),
    }


def fear_greed() -> dict | None:
    """Índice Fear & Greed (0 = medo extremo, 100 = ganância extrema)."""
    try:
        data = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()["data"][0]
        return {"valor": int(data["value"]), "classificacao": data["value_classification"]}
    except Exception:
        return None


def snapshot(df: pd.DataFrame, n_candles: int = 30) -> dict:
    """Resumo compacto para enviar à IA."""
    last = df.iloc[-1]
    recent = df.tail(n_candles)
    pct = lambda a, b: round((a / b - 1) * 100, 2)  # noqa: E731
    return {
        "preco": round(float(last["close"]), 6),
        "vela_fechada_em": last["ts"].isoformat(),
        "variacao_24_velas_pct": pct(last["close"], df.iloc[-25]["close"]),
        "rsi": round(float(last["rsi"]), 1),
        "ema9": round(float(last["ema9"]), 6),
        "ema21": round(float(last["ema21"]), 6),
        "ema50": round(float(last["ema50"]), 6),
        "ema200": round(float(last["ema200"]), 6),
        "macd_hist": round(float(last["macd_hist"]), 6),
        "bb_upper": round(float(last["bb_upper"]), 6),
        "bb_lower": round(float(last["bb_lower"]), 6),
        "atr": round(float(last["atr"]), 6),
        "volume_relativo": round(float(last["vol_ratio"]), 2),
        "maxima_periodo": round(float(recent["high"].max()), 6),
        "minima_periodo": round(float(recent["low"].min()), 6),
        "ultimas_velas_ohlcv": [
            [r.ts.strftime("%m-%d %H:%M"), round(r.open, 6), round(r.high, 6),
             round(r.low, 6), round(r.close, 6), round(r.volume, 2)]
            for r in recent.itertuples()
        ],
    }
