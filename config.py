"""Configuração carregada do .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise SystemExit(f"[config] Variável obrigatória ausente no .env: {name}")
    return value


# Telegram
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN", required=True)
TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID", required=True)  # chat privado ou grupo (grupo começa com -)
# IDs de usuário que podem usar /analise, /adicionar e /remover. Vazio = todos do chat.
TELEGRAM_ADMIN_IDS = {s.strip() for s in _get("TELEGRAM_ADMIN_IDS", "").split(",") if s.strip()}

# OpenAI (a chave é lida automaticamente pelo SDK via OPENAI_API_KEY)
_get("OPENAI_API_KEY", required=True)
OPENAI_MODEL = _get("OPENAI_MODEL", "gpt-5.4-mini")
# none | minimal | low | medium | high  (vazio = não envia; use vazio para modelos sem raciocínio)
OPENAI_REASONING_EFFORT = _get("OPENAI_REASONING_EFFORT", "low")
AI_WEB_SEARCH = _get("AI_WEB_SEARCH", "true").lower() == "true"

# Mercado
EXCHANGE = _get("EXCHANGE", "coinbase")
SYMBOLS = [s.strip() for s in _get("SYMBOLS", "BTC/USD,ETH/USD,SOL/USD").split(",") if s.strip()]
TIMEFRAME = _get("TIMEFRAME", "1h")
CHECK_INTERVAL_MIN = int(_get("CHECK_INTERVAL_MIN", "5"))

# Regras de alerta
# Perfil inicial: conservador | moderado | agressivo (depois dá para trocar pelo bot com /perfil)
RISK_PROFILE = _get("RISK_PROFILE", "conservador").strip().lower()
# baixa | media | alta. Vazio = usa o padrão do perfil (agressivo aceita "baixa")
MIN_CONFIDENCE = _get("MIN_CONFIDENCE", "").strip().lower()
TRIGGER_TTL_HOURS = int(_get("TRIGGER_TTL_HOURS", "48"))  # validade dos gatilhos de preço
# Gatilhos automáticos (sem IA) na máxima/mínima recente do tempo gráfico maior, para moedas sem gatilho
AUTO_TRIGGERS = _get("AUTO_TRIGGERS", "true").lower() == "true"
AUTO_TRIGGER_HOURS = int(_get("AUTO_TRIGGER_HOURS", "6"))  # recalcula a faixa a cada N horas
COOLDOWN_CANDLES = int(_get("COOLDOWN_CANDLES", "4"))  # velas sem reanalisar o mesmo ativo
SEND_REJECTED = _get("SEND_REJECTED", "false").lower() == "true"  # avisar quando a IA descarta

# Tokens de DEX (ex.: Solana), com velas do GeckoTerminal quando o par não existe na corretora
DEX_ENABLED = _get("DEX_ENABLED", "true").lower() == "true"
DEX_MIN_VOLUME_USD = float(_get("DEX_MIN_VOLUME_USD", "500000"))  # volume mínimo em 24h para aceitar
DEX_FEE_PCT = float(_get("DEX_FEE_PCT", "2.0"))  # taxa + slippage por lado ao comprar pelo app

# Paper trading (simulado, sem dinheiro real)
PAPER_TRADE_USD = float(_get("PAPER_TRADE_USD", "100"))
PAPER_FEE_PCT = float(_get("PAPER_FEE_PCT", "0.6"))  # taxa por lado (Coinbase taker ~0.6%)

TIMEZONE = _get("TIMEZONE", "America/Sao_Paulo")  # fuso dos horários nas mensagens

DB_PATH = BASE_DIR / _get("DB_FILE", "crypto_alerts.db")
