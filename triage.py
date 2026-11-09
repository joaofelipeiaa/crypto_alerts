"""Triagem barata por indicadores: só chama a IA quando há um sinal candidato."""
import pandas as pd


def find_candidate(df: pd.DataFrame) -> dict | None:
    """Retorna {'direcao': 'COMPRA'|'VENDA', 'gatilhos': [...]} ou None."""
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
