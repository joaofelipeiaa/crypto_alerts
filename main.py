"""Loop principal: analisa os ativos periodicamente e responde comandos do Telegram."""
import logging
import time
from datetime import datetime, timedelta

import ccxt
import pandas as pd

import agent
import config
import market
import notifier
import paper
import triage
from notifier import esc, fmt_price

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("crypto_alerts")

TF_SECONDS = ccxt.Exchange.parse_timeframe(config.TIMEFRAME)


def check_positions(symbol: str, df: pd.DataFrame) -> None:
    """Fecha posições simuladas que bateram stop ou alvo e avisa no Telegram."""
    trade = paper.open_position(symbol)
    if not trade:
        return
    opened = pd.Timestamp(trade["opened_at"])
    closed = None
    for row in df[df["ts"] >= opened].itertuples():  # só velas inteiras depois da entrada
        closed = paper.check_stops(symbol, row.low, row.high)
        if closed:
            break
    if not closed:
        price = market.last_price(symbol)
        closed = paper.check_stops(symbol, price, price)
    if closed:
        icon = "✅" if closed["pnl_usd"] > 0 else "❌"
        notifier.send(
            f"{icon} <b>Paper trade fechado: {esc(symbol)}</b> ({esc(closed['exit_reason'])})\n"
            f"Entrada {fmt_price(closed['entry'])} → Saída {fmt_price(closed['exit'])}\n"
            f"Resultado: <b>{closed['pnl_usd']:+.2f} USD ({closed['pnl_pct']:+.2f}%)</b>"
        )


def in_cooldown(symbol: str, candle_ts: pd.Timestamp) -> bool:
    last = paper.last_signal_candle(symbol)
    if not last:
        return False
    return (candle_ts - pd.Timestamp(last)).total_seconds() < config.COOLDOWN_CANDLES * TF_SECONDS


def run_symbol(symbol: str) -> None:
    df = market.add_indicators(market.fetch_candles(symbol))
    check_positions(symbol, df)

    candle_ts = df.iloc[-1]["ts"]
    if in_cooldown(symbol, candle_ts):
        return

    candidate = triage.find_candidate(df)
    if not candidate:
        return

    position = paper.open_position(symbol)
    if candidate["direcao"] == "COMPRA" and position:
        return  # já está comprado na simulação

    log.info("%s: sinal candidato de %s (%s). Consultando IA...",
             symbol, candidate["direcao"], "; ".join(candidate["gatilhos"]))
    snap = market.snapshot(df)
    decision = agent.analyze(symbol, config.TIMEFRAME, snap, market.fear_greed(), candidate, position)
    price = snap["preco"]

    alerted = False
    if decision:
        log.info("%s: IA decidiu %s (confiança %s)", symbol, decision["acao"], decision["confianca"])
        actionable = decision["acao"] in ("COMPRA", "VENDA") and paper.confidence_ok(decision["confianca"])
        if actionable or config.SEND_REJECTED:
            notifier.send(notifier.format_decision(symbol, price, decision, candidate))
            alerted = True
        if actionable and decision["acao"] == "COMPRA" and not position:
            paper.open_trade(symbol, price, decision["stop_loss"], decision["alvo"])
        elif actionable and decision["acao"] == "VENDA" and position:
            closed = paper.close_trade(position, price, "sinal de venda")
            notifier.send(f"📤 Paper trade de {esc(symbol)} encerrado pelo sinal de venda: "
                          f"<b>{closed['pnl_usd']:+.2f} USD ({closed['pnl_pct']:+.2f}%)</b>")

    paper.save_signal(symbol, candle_ts.isoformat(), candidate, decision, price, alerted)


def run_cycle() -> None:
    for symbol in config.SYMBOLS:
        try:
            run_symbol(symbol)
        except ccxt.BaseError as e:
            log.warning("%s: erro na corretora: %s", symbol, e)
        except Exception:
            log.exception("%s: erro inesperado", symbol)


# ---------------- comandos do Telegram ----------------

HELP = (
    "<b>Comandos</b>\n"
    "/status — posições simuladas abertas\n"
    "/desempenho — resultado do paper trading\n"
    "/analise BTC — análise da IA agora (não abre operação)\n"
    "/moedas — ativos monitorados"
)


def resolve_symbol(arg: str) -> str:
    arg = arg.upper()
    if "/" in arg:
        return arg
    for s in config.SYMBOLS:
        if s.split("/")[0] == arg:
            return s
    return f"{arg}/USD"


def handle_command(text: str) -> None:
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower()

    if cmd in ("/start", "/ajuda", "/help"):
        notifier.send(HELP)

    elif cmd == "/moedas":
        notifier.send(f"Monitorando ({esc(config.EXCHANGE)}, {esc(config.TIMEFRAME)}): "
                      + esc(", ".join(config.SYMBOLS)))

    elif cmd == "/status":
        trades = paper.all_open()
        if not trades:
            notifier.send("Nenhuma posição simulada aberta.")
            return
        lines = ["<b>Posições simuladas abertas</b>"]
        for t in trades:
            price = market.last_price(t["symbol"])
            pct = (price / t["entry"] - 1) * 100
            lines.append(f"• {esc(t['symbol'])}: entrada {fmt_price(t['entry'])}, agora {fmt_price(price)} "
                         f"(<b>{pct:+.2f}%</b>) | stop {fmt_price(t['stop_loss'] or 0)} | "
                         f"alvo {fmt_price(t['alvo'] or 0)}")
        notifier.send("\n".join(lines))

    elif cmd == "/desempenho":
        p = paper.performance()
        notifier.send(
            "<b>📊 Paper trading</b>\n"
            f"Análises da IA: {p['analises']} · Alertas enviados: {p['alertas']}\n"
            f"Trades fechados: {p['trades_fechados']} ({p['ganhos']} ganhos / {p['perdas']} perdas)\n"
            f"Taxa de acerto: {p['taxa_acerto']:.0f}%\n"
            f"Ganho médio: {p['ganho_medio_usd']:+.2f} USD · Perda média: {p['perda_media_usd']:+.2f} USD\n"
            f"<b>Resultado total: {p['pnl_total_usd']:+.2f} USD</b> "
            f"(com {config.PAPER_TRADE_USD:.0f} USD por operação, taxas incluídas)"
        )

    elif cmd == "/analise":
        if len(parts) < 2:
            notifier.send("Uso: /analise BTC")
            return
        symbol = resolve_symbol(parts[1])
        notifier.send(f"🔎 Analisando {esc(symbol)}... (pode levar até 1 minuto)")
        try:
            df = market.add_indicators(market.fetch_candles(symbol))
        except ccxt.BaseError as e:
            notifier.send(f"Não consegui buscar {esc(symbol)}: {esc(e)}")
            return
        snap = market.snapshot(df)
        decision = agent.analyze(symbol, config.TIMEFRAME, snap, market.fear_greed(),
                                 triage.find_candidate(df), paper.open_position(symbol))
        if decision:
            notifier.send(notifier.format_decision(symbol, snap["preco"], decision, None))
        else:
            notifier.send("A IA não conseguiu concluir a análise agora. Veja os logs.")

    else:
        notifier.send(HELP)


def poll_commands(offset: int | None, until: datetime) -> int | None:
    while datetime.now() < until:
        remaining = int((until - datetime.now()).total_seconds())
        for upd in notifier.get_updates(offset, timeout=max(1, min(30, remaining))):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            # Só obedece ao seu próprio chat
            if str(msg.get("chat", {}).get("id")) != str(config.TELEGRAM_CHAT_ID):
                continue
            if msg.get("text", "").startswith("/"):
                try:
                    handle_command(msg["text"])
                except Exception:
                    log.exception("Erro no comando %s", msg.get("text"))
                    notifier.send("Erro ao executar o comando. Veja os logs.")
    return offset


def main() -> None:
    log.info("Iniciando: %s %s %s, a cada %d min",
             config.EXCHANGE, config.TIMEFRAME, config.SYMBOLS, config.CHECK_INTERVAL_MIN)
    notifier.send(f"🤖 Monitor de cripto iniciado\n{esc(', '.join(config.SYMBOLS))} · {esc(config.TIMEFRAME)}\n"
                  "Envie /ajuda para ver os comandos.")
    offset = None
    for upd in notifier.get_updates(None, timeout=0):  # ignora comandos antigos
        offset = upd["update_id"] + 1
    while True:
        started = datetime.now()
        run_cycle()
        offset = poll_commands(offset, started + timedelta(minutes=config.CHECK_INTERVAL_MIN))


if __name__ == "__main__":
    main()
