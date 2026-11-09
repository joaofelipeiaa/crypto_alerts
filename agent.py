"""Agente de IA (OpenAI) que valida ou descarta o sinal candidato."""
import json
import logging

import openai

import config

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
3. Decidir: COMPRA, VENDA ou AGUARDAR.

Regras:
- As operações são apenas à vista (spot). VENDA significa sair/realizar uma posição, não vender a descoberto.
- Na dúvida, AGUARDAR. Um sinal ruim custa dinheiro; um sinal perdido não custa nada.
- Sinais contra a tendência maior exigem justificativa forte e no máximo confiança "baixa" ou "media".
- Para COMPRA, defina stop_loss (normalmente 1,5 a 2,5 ATR abaixo do preço, ou abaixo de um suporte)
  e alvo com relação risco/retorno de pelo menos 1,5. Para VENDA ou AGUARDAR use 0 nos dois campos.
- Seja honesto sobre a incerteza. Nunca prometa resultado.
- Escreva em português do Brasil, de forma direta e curta (é uma mensagem de celular)."""

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
    },
    "required": ["acao", "confianca", "resumo", "motivos", "riscos", "noticias", "stop_loss", "alvo"],
    "additionalProperties": False,
}


def analyze(symbol: str, timeframe: str, snapshot: dict, sentiment: dict | None,
            candidate: dict | None, open_position: dict | None) -> dict | None:
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
        "instructions": SYSTEM_PROMPT,
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
        return json.loads(resp.output_text)
    except json.JSONDecodeError:
        log.error("IA não retornou JSON válido para %s (possível recusa): %r", symbol, resp.output_text[:300])
        return None
