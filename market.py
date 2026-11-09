"""Dados de mercado (via ccxt) e indicadores técnicos."""
import ccxt
import pandas as pd
import requests

import config

_exchange = getattr(ccxt, config.EXCHANGE)({"enableRateLimit": True})


def fetch_candles(symbol: str, timeframe: str = config.TIMEFRAME, limit: int = 250) -> pd.DataFrame:
    """Busca velas e descarta a última (ainda aberta), usando só velas fechadas."""
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
    candles = _exchange.fetch_ohlcv(symbol, "1h", since=ts_ms, limit=1)
    if candles and abs(candles[0][0] - ts_ms) <= 2 * 3600 * 1000:
        return float(candles[0][1])
    return None


def last_price(symbol: str) -> float:
    return float(_exchange.fetch_ticker(symbol)["last"])


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
