"""Agente de IA (OpenAI) que valida ou descarta o sinal candidato."""
import json
import logging

import openai

import config
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
- Seja honesto sobre a incerteza. Nunca prometa resultado.
- Escreva em português do Brasil, de forma direta e curta (é uma mensagem de celular).

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
            candidate: dict | None, open_position: dict | None, perfil: str = DEFAULT_PROFILE) -> dict | None:
    payload = {
        "ativo": symbol,
        "timeframe": timeframe,
        "sinal_candidato": candidate or "nenhum (análise solicitada manualmente)",
        "posicao_simulada_aberta": open_position or "nenhuma",
        "fear_and_greed": sentiment or "indisponível",
        "dados_tecnicos": snapshot,
    }
    params = {
        "model": config.OPENAI_MODEL,
        "instructions": SYSTEM_PROMPT.format(perfil=perfil, regras=PROFILES[perfil]["rules"]),
        "input": "Analise o ativo abaixo e dê sua decisão.\n\n"
                 + json.dumps(payload, ensure_ascii=False, indent=1),
        "text": {"format": {"type": "json_schema", "name": "decisao",
                            "schema": DECISION_SCHEMA, "strict": True}},
        "max_output_tokens": 16000,
    }
    if config.AI_WEB_SEARCH:
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

    if resp.status != "completed":
        log.error("Resposta incompleta da IA para %s: %s", symbol, resp.incomplete_details)
        return None
    try:
        decision = json.loads(resp.output_text)
        decision["vies"] = max(-100, min(100, int(decision.get("vies", 0))))
        return decision
    except json.JSONDecodeError:
        log.error("IA não retornou JSON válido para %s (possível recusa): %r", symbol, resp.output_text[:300])
        return None
