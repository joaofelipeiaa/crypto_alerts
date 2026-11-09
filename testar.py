"""Verifica a instalação passo a passo.

    python testar.py          # testa Coinbase, indicadores e Telegram
    python testar.py --ia     # também faz uma análise real com a IA (consome créditos)
"""
import sys

import config
import market
import notifier
import triage

print(f"1) Buscando velas na {config.EXCHANGE}...")
for symbol in config.SYMBOLS:
    df = market.add_indicators(market.fetch_candles(symbol))
    last = df.iloc[-1]
    cand = triage.find_candidate(df)
    print(f"   {symbol}: preço {last['close']:.2f} | RSI {last['rsi']:.1f} | "
          f"candidato: {cand['direcao'] + ' - ' + '; '.join(cand['gatilhos']) if cand else 'nenhum'}")

print("2) Fear & Greed:", market.fear_greed())

print("3) Enviando mensagem de teste para o Telegram...")
notifier.send("✅ Teste do monitor de cripto: o Telegram está funcionando!")
print("   Confira seu Telegram.")

if "--ia" in sys.argv:
    import agent
    symbol = config.SYMBOLS[0]
    print(f"4) Pedindo análise da IA para {symbol} (pode levar até 1 minuto)...")
    df = market.add_indicators(market.fetch_candles(symbol))
    snap = market.snapshot(df)
    decision = agent.analyze(symbol, config.TIMEFRAME, snap, market.fear_greed(),
                             triage.find_candidate(df), None)
    print("   Decisão:", decision)
    if decision:
        notifier.send(notifier.format_decision(symbol, snap["preco"], decision, None))
        print("   Análise enviada para o Telegram.")
