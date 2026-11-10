"""Triagem barata por indicadores: só chama a IA quando há um sinal candidato."""
import pandas as pd


def oversold_reversal(df: pd.DataFrame, lookback: int = 8) -> dict | None:
    """'Comprar baixo' com confirmação: houve sobrevenda nas últimas velas e a vela atual reagiu forte.

    Em vez de comprar enquanto o preço ainda cai (faca caindo), espera a queda mostrar perda de força:
    vela de alta que fecha acima da máxima da anterior e da EMA9, RSI de volta acima de 40, volume
    na média ou acima, e sem nova mínima. Regra propositalmente rígida: no teste de nov/2026 (5 dias
    de queda), a versão frouxa dava ~300 sinais com 69% de quedas depois.
    """
    cur, prev = df.iloc[-1], df.iloc[-2]
    recent = df.iloc[-1 - lookback:-1]
    swing_low = float(recent["low"].min())
    ok = (recent["rsi"].min() < 30 and cur["close"] > cur["open"] and cur["close"] > prev["high"]
          and cur["close"] > cur["ema9"] and cur["rsi"] >= 40 and cur["vol_ratio"] >= 1.0
          and cur["low"] > swing_low)
    if not ok:
        return None
    gatilhos = [f"Reversão de sobrevenda: RSI saiu de {recent['rsi'].min():.1f} para {cur['rsi']:.1f}, "
                "vela de alta fechando acima da máxima anterior e da EMA9, sem nova mínima",
                f"Mínima recente (referência para o stop): {swing_low:.6g}",
                f"Volume da vela: {cur['vol_ratio']:.1f}x a média"]
    return {"direcao": "COMPRA", "tipo": "reversao_sobrevenda", "gatilhos": gatilhos,
            "contrarios": [], "minima_recente": swing_low}


def find_candidate(df: pd.DataFrame) -> dict | None:
    """Retorna {'direcao': 'COMPRA'|'VENDA', 'gatilhos': [...]} ou None."""
    reversal = oversold_reversal(df)
    if reversal:
        return reversal
    cur, prev = df.iloc[-1], df.iloc[-2]
    buy, sell = [], []

    # RSI em extremos
    if cur["rsi"] < 30:
        buy.append(f"RSI em sobrevenda ({cur['rsi']:.1f})")
    elif cur["rsi"] > 70:
        sell.append(f"RSI em sobrecompra ({cur['rsi']:.1f})")

    # Cruzamento EMA 9 x 21
    if prev["ema9"] <= prev["ema21"] and cur["ema9"] > cur["ema21"]:
        buy.append("EMA9 cruzou acima da EMA21")
    elif prev["ema9"] >= prev["ema21"] and cur["ema9"] < cur["ema21"]:
        sell.append("EMA9 cruzou abaixo da EMA21")

    # Cruzamento MACD
    if prev["macd_hist"] <= 0 < cur["macd_hist"]:
        buy.append("MACD cruzou para cima da linha de sinal")
    elif prev["macd_hist"] >= 0 > cur["macd_hist"]:
        sell.append("MACD cruzou para baixo da linha de sinal")

    # Rompimento das Bandas de Bollinger
    if cur["close"] < cur["bb_lower"]:
        buy.append("Preço fechou abaixo da Banda de Bollinger inferior")
    elif cur["close"] > cur["bb_upper"]:
        sell.append("Preço fechou acima da Banda de Bollinger superior")

    if not buy and not sell:
        return None

    extra = []
    if cur["vol_ratio"] > 2:
        extra.append(f"Volume {cur['vol_ratio']:.1f}x acima da média")

    if len(buy) > len(sell):
        return {"direcao": "COMPRA", "gatilhos": buy + extra, "contrarios": sell}
    if len(sell) > len(buy):
        return {"direcao": "VENDA", "gatilhos": sell + extra, "contrarios": buy}
    return None  # sinais conflitantes empatados: ignora
