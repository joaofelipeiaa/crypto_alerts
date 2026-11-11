"""Agente de IA (OpenAI) que valida ou descarta o sinal candidato."""
import json
import logging
from datetime import datetime, timedelta, timezone

import openai

import config
import paper
from profiles import DEFAULT_PROFILE, PROFILES

log = logging.getLogger(__name__)
client = openai.OpenAI()  # lê OPENAI_API_KEY do ambiente

SYSTEM_PROMPT = """Você é um analista de criptomoedas cauteloso que ajuda um investidor pessoa física.
Você recebe dados técnicos de um ativo (indicadores calculados sobre velas fechadas), o índice
Fear & Greed e, opcionalmente, um sinal candidato gerado por regras simples de indicadores.

Seu trabalho:
1. Avaliar se o sinal candidato faz sentido no contexto completo: tendência maior (EMA50/EMA200),
   força do movimento, volume, suportes/resistências recentes e sentimento de mercado.
2. Quando tiver a ferramenta de busca, pesquisar notícias RECENTES (últimos dias) do ativo e do
   mercado cripto: hacks, regulação, ETFs, listagens/deslistagens, dados macro (Fed, CPI). Seja
   breve: 1 a 3 buscas.
3. Decidir: COMPRA, VENDA ou AGUARDAR, seguindo o perfil de risco abaixo.
4. Mesmo quando a decisão for AGUARDAR, dizer para que lado o ativo está inclinado (vies) e quais
   preços mudariam a análise (gatilho_alta e gatilho_baixa).

Regras gerais:
- As operações são apenas à vista (spot). VENDA significa sair/realizar uma posição, não vender a descoberto.
- Para COMPRA, defina stop_loss (abaixo de um suporte ou alguns ATR abaixo do preço) e alvo com relação
  risco/retorno de pelo menos 1,5. Para VENDA ou AGUARDAR use 0 nos dois campos.
- vies: inteiro de -100 (forte inclinação de baixa) a +100 (forte inclinação de alta); perto de 0 = neutro.
  Seja coerente com os motivos: não dê vies neutro quando a análise aponta claramente para um lado.
- dados_tecnicos.tendencia_maior resume o tempo gráfico acima (EMA50/200, RSI, máximas e mínimas).
  Use-o como a "tendência maior" das regras do perfil; os suportes/resistências dele pesam mais.
- dados_tecnicos.preco é o fechamento da última vela fechada; preco_agora é o preço ao vivo (a vela
  atual ainda está aberta). Considere os dois: se o preço já andou desde o fechamento, leve isso em conta.
- gatilho_alta: preço ACIMA do atual cujo rompimento confirmaria alta e justificaria reanalisar para
  compra (ex.: resistência). gatilho_baixa: preço ABAIXO do atual cuja perda pioraria o cenário (ex.:
  suporte). Use níveis concretos dos dados, a pelo menos ~1 ATR do preco_agora (níveis colados no preço
  disparam com ruído e são ignorados); 0 se não houver nível relevante.
- Se o sinal candidato veio de um gatilho rompido, diga explicitamente se o rompimento está se
  confirmando (preco_agora vs. nível) ou se parece falso.
- Se houver dados_tecnicos.token_de_dex, é um token pequeno negociado em DEX: liquidez baixa, fácil de
  manipular, risco de golpe e custo de ~{dex_fee}% por lado. Exija confirmação mais forte, cite esses
  riscos e lembre que o alvo precisa cobrir as taxas. Desconfie de picos de volume isolados.
- Se o sinal candidato tiver "aviso_atraso", o rompimento aconteceu há horas (o bot estava parado): não
  reaja ao rompimento em si; avalie o cenário de AGORA (o preço pode já ter se estabilizado ou revertido)
  e diga isso no resumo.
- suas_analises_anteriores traz o que você mesmo disse sobre este ativo e o que o preço fez depois
  (variacao_desde_entao_pct). Mantenha coerência: se mudar de opinião, diga o que mudou. Se uma leitura
  anterior errou (ex.: viés de alta e o preço caiu forte), reconheça e ajuste, sem repetir o erro.
- carteira_dos_membros diz quem do grupo tem o ativo de verdade, com preço médio e resultado atual.
  VENDA só faz sentido para quem tem o ativo: se ninguém tem, nunca responda VENDA (use AGUARDAR
  com viés negativo). Se alguém tem, considere o preço médio: proteger lucro, limitar prejuízo ou
  segurar se a queda for ruído dentro do plano. Não recomende vender só porque caiu: diga se o
  cenário piorou de verdade (suporte importante perdido com volume, tendência maior virando).
- Sinal candidato com tipo "reversao_sobrevenda" é a compra "no fundo" depois que a queda mostrou
  perda de força. Pode ser COMPRA mesmo contra a tendência maior (no perfil conservador, só se o
  tempo gráfico maior também mostrar a queda perdendo força), com stop logo abaixo de minima_recente
  (no máximo ~1,5 ATR do preço) e alvo na EMA21/EMA50 ou resistência próxima, risco/retorno >= 1,5.
  Recuse se a vela de reversão for fraca, sem volume, ou se houver notícia muito negativa. Em queda
  forte no tempo gráfico maior, essas compras falham na maioria das vezes: só aprove com assimetria clara.
- dados_tecnicos.custos_operacao traz a taxa de ida e volta e o alvo_minimo_pct. COMPRA só com alvo
  pelo menos alvo_minimo_pct acima do preço; se o alvo realista (resistência próxima) for menor, AGUARDAR.
- Se posicao_simulada_aberta tiver stop_loss, esse é o plano da compra: não responda VENDA com o preço
  acima do stop, a não ser por fato novo e forte (notícia grave, hack, queda brusca com volume muito alto);
  oscilação contra a posição é esperada. gatilho_baixa nunca acima desse stop.
- Seja honesto sobre a incerteza. Nunca prometa resultado.
- Escreva em português do Brasil, de forma direta e curta (é uma mensagem de celular).
  Traduza tudo que vier das notícias: nada em outro idioma ou alfabeto (ex.: "US$ 30 milhões", nunca "млн" ou "M").

Perfil de risco atual: {perfil}
{regras}"""

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "acao": {"type": "string", "enum": ["COMPRA", "VENDA", "AGUARDAR"]},
        "confianca": {"type": "string", "enum": ["baixa", "media", "alta"]},
        "resumo": {"type": "string", "description": "Uma ou duas frases com a conclusão."},
        "motivos": {"type": "array", "items": {"type": "string"}},
        "riscos": {"type": "array", "items": {"type": "string"}},
        "noticias": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Notícias recentes relevantes encontradas (pode ser vazio).",
        },
        "stop_loss": {"type": "number", "description": "Preço de stop. 0 se não se aplica."},
        "alvo": {"type": "number", "description": "Preço alvo. 0 se não se aplica."},
        "vies": {"type": "integer", "description": "-100 (forte baixa) a +100 (forte alta)."},
        "gatilho_alta": {"type": "number", "description": "Preço acima do atual que justifica reanalisar. 0 se não houver."},
        "gatilho_baixa": {"type": "number", "description": "Preço abaixo do atual que piora o cenário. 0 se não houver."},
    },
    "required": ["acao", "confianca", "resumo", "motivos", "riscos", "noticias", "stop_loss", "alvo",
                 "vies", "gatilho_alta", "gatilho_baixa"],
    "additionalProperties": False,
}


def analyze(symbol: str, timeframe: str, snapshot: dict, sentiment: dict | None,
            candidate: dict | None, open_position: dict | None, perfil: str = DEFAULT_PROFILE,
            historico: list[dict] | None = None, carteira: list[dict] | None = None,
            source: str = "auto") -> dict | None:
    manual = source == "manual"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    calls_key = f"ai_calls:{today}"
    calls = int(paper.get_meta(calls_key) or 0)
    if not manual and config.MAX_AI_CALLS_DAY and calls >= config.MAX_AI_CALLS_DAY:
        log.warning("Limite diário de %d análises da IA atingido; %s ignorado", config.MAX_AI_CALLS_DAY, symbol)
        return None

    # notícias: busca na web no máximo a cada NEWS_CACHE_HOURS por ativo (a busca é o que mais custa)
    news_key = f"news:{symbol}"
    cached = None
    try:
        cached = json.loads(paper.get_meta(news_key) or "null")
    except json.JSONDecodeError:
        pass
    fresh = bool(cached) and (datetime.now(timezone.utc) - datetime.fromisoformat(cached["at"])
                              < timedelta(hours=config.NEWS_CACHE_HOURS))
    search = config.AI_WEB_SEARCH and (manual or not fresh)
    payload = {
        "ativo": symbol,
        "timeframe": timeframe,
        "sinal_candidato": candidate or "nenhum (análise solicitada manualmente)",
        "posicao_simulada_aberta": open_position or "nenhuma",
        "fear_and_greed": sentiment or "indisponível",
        "dados_tecnicos": snapshot,
        "suas_analises_anteriores": historico or "nenhuma nas últimas 72h",
        "carteira_dos_membros": carteira or "ninguém do grupo tem este ativo",
    }
    if config.AI_WEB_SEARCH and not search:
        payload["noticias_ja_pesquisadas"] = {
            "pesquisadas_em": cached["at"], "itens": cached["noticias"] or ["nada relevante encontrado"],
            "obs": "busca na web desligada nesta análise para economizar; use estas notícias"}
    params = {
        "model": config.OPENAI_MODEL,
        "instructions": SYSTEM_PROMPT.format(perfil=perfil, regras=PROFILES[perfil]["rules"],
                                             dex_fee=config.DEX_FEE_PCT),
        "input": "Analise o ativo abaixo e dê sua decisão.\n\n"
                 + json.dumps(payload, ensure_ascii=False, indent=1),
        "text": {"format": {"type": "json_schema", "name": "decisao",
                            "schema": DECISION_SCHEMA, "strict": True}},
        "max_output_tokens": 16000,
    }
    if search:
        params["tools"] = [{"type": "web_search"}]
    if config.OPENAI_REASONING_EFFORT:
        params["reasoning"] = {"effort": config.OPENAI_REASONING_EFFORT}

    try:
        resp = client.responses.create(**params)
    except openai.RateLimitError:
        log.warning("Rate limit/sem créditos na OpenAI; análise de %s adiada", symbol)
        return None
    except openai.APIStatusError as e:
        log.error("Erro da API OpenAI (%s): %s", e.status_code, e.message)
        return None
    except openai.APIConnectionError:
        log.error("Sem conexão com a API da OpenAI")
        return None

    paper.set_meta(calls_key, str(calls + 1))
    u = resp.usage
    log.info("IA %s (%s): %d tokens entrada, %d saída, busca na web: %s, análises hoje: %d", symbol, source,
             u.input_tokens if u else 0, u.output_tokens if u else 0, "sim" if search else "cache", calls + 1)
    if resp.status != "completed":
        log.error("Resposta incompleta da IA para %s: %s", symbol, resp.incomplete_details)
        return None
    try:
        decision = json.loads(resp.output_text)
        decision["vies"] = max(-100, min(100, int(decision.get("vies", 0))))
        if search:
            paper.set_meta(news_key, json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                                 "noticias": decision.get("noticias", [])}, ensure_ascii=False))
        return decision
    except json.JSONDecodeError:
        log.error("IA não retornou JSON válido para %s (possível recusa): %r", symbol, resp.output_text[:300])
        return None
