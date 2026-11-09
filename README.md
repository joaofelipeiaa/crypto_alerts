# Crypto Alerts: monitor de criptomoedas com IA e alertas no Telegram

Monitora ativos na Coinbase (ou qualquer corretora suportada pelo [`ccxt`](https://github.com/ccxt/ccxt)),
filtra sinais por indicadores técnicos, pede para uma IA (OpenAI) validar cada sinal (gráfico +
notícias + sentimento) e envia alertas de **compra/venda** no Telegram. Cada alerta vira uma operação
**simulada** (paper trading) para você medir o desempenho real antes de arriscar dinheiro.

> ⚠️ Nenhuma estratégia, com ou sem IA, garante lucro. Os alertas são apoio à decisão, não
> recomendação financeira. O projeto **não executa ordens reais**.

## Como funciona

```
A cada 5 min: velas fechadas da corretora
   │
   ├─ Triagem por indicadores (RSI, EMA 9/21, MACD, Bollinger)
   │     sem sinal → não faz nada (custo zero)
   │
   └─ Sinal candidato → agente de IA (OpenAI)
         • analisa tendência (EMA50/200), volume e as últimas 30 velas
         • lê o Fear & Greed e pesquisa notícias recentes na web
         • decide COMPRA / VENDA / AGUARDAR, com confiança, motivos, riscos, stop e alvo
              │
              └─ confiança ≥ mínima → alerta no Telegram + operação simulada
```

As posições simuladas são encerradas no stop, no alvo ou em um sinal de venda, com taxas incluídas.

## Instalação

```bash
git clone https://github.com/<seu-usuario>/crypto_alerts.git ~/crypto_alerts
cd ~/crypto_alerts
python3 -m venv .venv          # ou: uv venv .venv
.venv/bin/pip install -r requirements.txt   # ou: uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env && nano .env
```

Requer Python 3.10+.

### Telegram
1. No Telegram, fale com **@BotFather** → `/newbot` → copie o token para `TELEGRAM_BOT_TOKEN`.
2. Mande qualquer mensagem para o seu bot.
3. Abra `https://api.telegram.org/bot<TOKEN>/getUpdates` e copie o número em `"chat":{"id": ...}`
   para `TELEGRAM_CHAT_ID`.

### OpenAI
Crie uma chave em https://platform.openai.com/api-keys, coloque em `OPENAI_API_KEY` e confira se a
conta tem créditos. Escolha o modelo em `OPENAI_MODEL`.

## Testar

```bash
.venv/bin/python testar.py        # corretora + indicadores + Telegram
.venv/bin/python testar.py --ia   # + uma análise real da IA (consome créditos)
```

## Rodar

```bash
.venv/bin/python main.py          # em primeiro plano (Ctrl+C para parar)
```

Como serviço systemd de usuário (inicia sozinho e reinicia em caso de erro). O arquivo assume o
projeto em `~/crypto_alerts`; ajuste `WorkingDirectory`/`ExecStart` se clonar em outro lugar.

```bash
mkdir -p ~/.config/systemd/user
cp crypto-alerts.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now crypto-alerts
loginctl enable-linger $USER          # continua rodando sem sessão aberta
journalctl --user -u crypto-alerts -f # logs
```

## Comandos no Telegram

| Comando | O que faz |
|---|---|
| `/status` | posições simuladas abertas, com lucro/prejuízo atual |
| `/desempenho` | taxa de acerto e resultado total do paper trading |
| `/analise BTC` | análise da IA na hora (não abre operação) |
| `/moedas` | ativos monitorados |

O bot só responde ao `TELEGRAM_CHAT_ID` configurado.

## Configuração

Todas as opções ficam no `.env` (veja os comentários em [`.env.example`](.env.example)): ativos,
timeframe, intervalo, confiança mínima, cooldown, valor por operação simulada, modelo da IA etc.

## Custos
- Dados da Coinbase (públicos), Telegram e Fear & Greed: grátis.
- IA: paga por análise, e só roda quando há sinal candidato. Para reduzir o custo, use um modelo
  menor, `OPENAI_REASONING_EFFORT=low`, `AI_WEB_SEARCH=false` ou aumente `COOLDOWN_CANDLES`.

## Segurança
- **Nunca** suba o `.env` (já está no `.gitignore`). Ele contém o token do bot e a chave da OpenAI.
- Se um segredo vazar, revogue na hora: `/revoke` no @BotFather e apague a chave no painel da OpenAI.
- O projeto só lê dados públicos da corretora; não precisa de chave da Coinbase.

## Arquivos
| Arquivo | Função |
|---|---|
| `main.py` | loop principal e comandos do Telegram |
| `market.py` | velas (ccxt), indicadores, Fear & Greed |
| `triage.py` | regras que geram o sinal candidato |
| `agent.py` | agente de IA (OpenAI Responses API + busca na web) |
| `paper.py` | SQLite: histórico de sinais e paper trading |
| `notifier.py` | envio/recebimento no Telegram |
| `testar.py` | verificação da instalação |
