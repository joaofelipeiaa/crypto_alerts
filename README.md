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

Os alertas de análise, gatilho e paper trade vêm com um botão **📲 Abrir na Coinbase** que leva direto
à página da moeda na Coinbase normal (a mesma do app, com o botão Comprar/Vender). O bot nunca envia ordens: a decisão e o clique são seus.

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
| `/placar` | o que o preço fez 24h/72h depois das análises (aprovadas × descartadas, viés) |
| `/gatilhos` | preços que disparam uma nova análise |
| `/carteira` | suas moedas reais; `/carteira ZCAT 0.0185` registra (preço médio), `/carteira remover ZCAT` tira |
| `/perfil` | perfil de risco atual; `/perfil moderado` troca (admin) |
| `/analise BTC` | análise da IA na hora (não abre operação; admin) |
| `/moedas` | ativos monitorados |
| `/adicionar XRP DOGE` | passa a monitorar uma ou mais moedas (valida na corretora) |
| `/remover XRP` | para de monitorar |

O bot só responde ao `TELEGRAM_CHAT_ID` configurado.

A lista de moedas fica salva no banco (`crypto_alerts.db`). O `SYMBOLS` do `.env` só define a
lista inicial, usada na primeira execução; depois disso, gerencie pelo bot. Máximo de 30 moedas.

## Viés, gatilhos, placar e perfis

- **Carteira** (`/carteira`): cada membro registra as moedas que tem e o preço médio. VENDA só é avisado
  para moedas que alguém tem (a mensagem marca quem), e a IA considera o preço médio de cada um.
  Para quem não tem a moeda, VENDA não serve para nada e fica em silêncio.
- **Compra na reversão**: além dos sinais comuns, a triagem procura a "compra no fundo" com
  confirmação: sobrevenda recente e uma vela de alta forte, que fecha acima da máxima anterior e da
  EMA9, sem fazer nova mínima. A IA decide, com stop logo abaixo da mínima recente. Em tendência de baixa
  forte essas compras falham com frequência: o `/placar` e o `/desempenho` mostram se estão valendo a pena.
- **Limite de gatilhos**: no máximo uma análise por gatilho por moeda a cada
  `TRIGGER_MIN_INTERVAL_MIN` (60 min), para não perseguir uma queda contínua.

- **Viés** (-100 a +100): toda análise diz para que lado o ativo está inclinado, mesmo quando a decisão é AGUARDAR.
- **Gatilhos**: a IA marca um preço acima (confirmação de alta) e outro abaixo (piora do cenário).
  Se o preço cruzar um deles, o bot reanalisa na hora. Só avisa se a nova análise der COMPRA ou VENDA
  (AGUARDAR fica registrado em silêncio). Valem por `TRIGGER_TTL_HOURS`.
- **Memória**: cada análise recebe as análises anteriores da IA para o mesmo ativo (até 4, das últimas
  72h) e quanto o preço andou desde cada uma, para manter coerência e corrigir leituras que erraram.
- **Gatilhos automáticos** (`AUTO_TRIGGERS`): moedas sem gatilho da IA ganham, sem custo de IA, uma faixa
  na máxima/mínima das últimas 30 velas do tempo gráfico maior (~2,5 dias com velas de 30m). Romper a faixa
  dispara uma análise. Depois disso valem os níveis da IA. No `/gatilhos`: 🧠 = IA, 📏 = automático.
- **Placar** (`/placar`): 24h e 72h depois de cada análise, o bot confere o preço. Compara os sinais de
  compra que a IA aprovou com os que ela descartou. Se os descartados sobem tanto quanto os aprovados,
  a IA está cautelosa demais; se sobem menos, o filtro está funcionando.
- **Perfis** (`/perfil`):
  - `conservador`: só compra a favor da tendência maior; poucos alertas.
  - `moderado`: aceita repiques/reversões contra a tendência, com stop mais curto.
  - `agressivo`: aceita confirmação parcial e momentum; mais alertas e mais erros (confiança mínima "baixa").

  Cada análise e trade simulado guarda o perfil usado: `/placar moderado` e `/desempenho` comparam os perfis.

## Tokens de DEX

Alguns tokens aparecem no app da Coinbase mas não na corretora (ex.: memecoins da Solana como ZCAT): o app
compra por uma DEX. Para esses, o `/adicionar` busca o token no [GeckoTerminal](https://www.geckoterminal.com)
(gratuito) e usa as velas da pool com mais volume. Travas:

- só aceita tokens com volume de 24h acima de `DEX_MIN_VOLUME_USD` (padrão US$ 500 mil);
- o paper trading usa `DEX_FEE_PCT` (padrão 2%) por lado, por causa da taxa do app e do slippage;
- os alertas vêm marcados com ⚠️ DEX e a IA recebe volume, liquidez e valor de mercado para pesar o risco;
- o bot mostra o endereço do contrato ao adicionar: confira no app que é o mesmo token.

## Usar em um grupo

1. Crie o grupo no Telegram e adicione o bot como membro.
2. No grupo, mande `/id`. O bot responde com o ID do grupo (começa com `-`) e com o seu ID de usuário.
3. No `.env`: `TELEGRAM_CHAT_ID=<ID do grupo>` e `TELEGRAM_ADMIN_IDS=<seu ID>`.
4. Reinicie: `systemctl --user restart crypto-alerts`.
5. No @BotFather, use `/setjoingroups` → **Disable** para ninguém adicionar o bot em outros grupos.

Todos do grupo recebem os alertas e podem usar `/status`, `/desempenho` e `/moedas`. Só os IDs em
`TELEGRAM_ADMIN_IDS` podem usar `/analise` (que gasta créditos da OpenAI), `/adicionar` e `/remover`.
Se o grupo virar supergrupo, o ID muda: o novo aparece nos logs e no `/id`.

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
| `paper.py` | SQLite: sinais, placar, gatilhos, perfil e paper trading |
| `profiles.py` | regras de cada perfil de risco |
| `notifier.py` | envio/recebimento no Telegram |
| `testar.py` | verificação da instalação |
