"""Perfis de risco: mudam as regras que a IA segue e a confiança mínima para alertar."""

PROFILES = {
    "conservador": {
        "min_conf": "media",
        "desc": "Só compra a favor da tendência maior. Na dúvida, aguarda. Poucos alertas.",
        "rules": """- Na dúvida, AGUARDAR. Um sinal ruim custa dinheiro; um sinal perdido não custa nada.
- Só recomende COMPRA a favor da tendência maior (preço acima da EMA200, ou EMA50 subindo e recuperada).
- Sinais contra a tendência maior: no máximo confiança "baixa".
- Exija confirmação (fechamento acima de resistência, volume acima da média) antes de comprar.""",
    },
    "moderado": {
        "min_conf": "media",
        "desc": "Aceita repiques e reversões contra a tendência, com stop mais curto.",
        "rules": """- Compras a favor da tendência maior são preferidas, mas repiques/reversões contra a tendência
  são aceitáveis quando há sobrevenda clara (RSI baixo, preço na banda inferior) E algum sinal de
  reversão (MACD virando, candle de rejeição, volume acima da média).
- Em compras contra a tendência, use stop mais curto (cerca de 1 a 1,5 ATR) e alvo realista
  (próxima resistência/EMA), com risco/retorno de pelo menos 1,5.
- Não exija confirmação perfeita: se a assimetria for boa, prefira agir com confiança "media".
- Ainda assim, AGUARDE quando os sinais forem contraditórios ou houver notícia muito negativa.""",
    },
    "agressivo": {
        "min_conf": "baixa",
        "desc": "Aceita sinais com confirmação parcial e de momentum. Mais alertas e mais erros.",
        "rules": """- Procure oportunidades: rompimentos com volume, momentum forte, repiques de sobrevenda e
  reversões, mesmo contra a tendência maior.
- Confirmação parcial basta. Use a confiança para indicar a qualidade do sinal ("baixa" é aceitável).
- Sempre defina stop (1 a 2 ATR) e alvo com risco/retorno de pelo menos 1,5: no perfil agressivo,
  o stop é o que limita o prejuízo dos sinais errados.
- AGUARDE apenas quando não houver assimetria nenhuma ou houver risco de evento extremo (hack, deslistagem).""",
    },
}

DEFAULT_PROFILE = "conservador"
