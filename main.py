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
from profiles import PROFILES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("crypto_alerts")

TF_SECONDS = ccxt.Exchange.parse_timeframe(config.TIMEFRAME)
EVAL_EVERY_MIN = 30
_last_eval = 0.0


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


def evaluate(symbol: str, df: pd.DataFrame, candidate: dict | None, source: str) -> dict | None:
    """Consulta a IA, envia o alerta, atualiza o paper trading, salva o sinal e os gatilhos.

    source: 'auto' (ciclo normal), 'gatilho' (preço cruzou um gatilho) ou 'manual' (/analise).
    """
    perfil = paper.get_profile()
    position = paper.open_position(symbol)
    snap = market.snapshot(df)
    price = snap["preco"]
    decision = agent.analyze(symbol, config.TIMEFRAME, snap, market.fear_greed(), candidate, position, perfil)
    if not decision:
        if source == "manual":
            notifier.send("A IA não conseguiu concluir a análise agora. Veja os logs.")
        else:
            paper.save_signal(symbol, df.iloc[-1]["ts"].isoformat(), candidate, None, price, False, source)
        return None

    log.info("%s [%s]: IA decidiu %s (confiança %s, viés %+d)", symbol, source,
             decision["acao"], decision["confianca"], decision["vies"])
    trigger = paper.set_trigger(symbol, price, decision["gatilho_alta"], decision["gatilho_baixa"])

    actionable = (source != "manual" and decision["acao"] in ("COMPRA", "VENDA")
                  and paper.confidence_ok(decision["confianca"]))
    alerted = source in ("manual", "gatilho") or actionable or config.SEND_REJECTED
    if alerted:
        notifier.send(notifier.format_decision(symbol, price, decision, candidate, perfil, trigger))

    if actionable and decision["acao"] == "COMPRA" and not position:
        paper.open_trade(symbol, price, decision["stop_loss"], decision["alvo"])
    elif actionable and decision["acao"] == "VENDA" and position:
        closed = paper.close_trade(position, price, "sinal de venda")
        notifier.send(f"📤 Paper trade de {esc(symbol)} encerrado pelo sinal de venda: "
                      f"<b>{closed['pnl_usd']:+.2f} USD ({closed['pnl_pct']:+.2f}%)</b>")

    paper.save_signal(symbol, df.iloc[-1]["ts"].isoformat(), candidate, decision, price, alerted, source)
    return decision


def check_trigger(symbol: str) -> dict | None:
    """Se o preço cruzou um gatilho salvo, retorna o sinal candidato correspondente."""
    trig = paper.get_trigger(symbol)
    if not trig:
        return None
    price = market.last_price(symbol)
    if trig["up"] and price >= trig["up"]:
        level, direction, word = trig["up"], "COMPRA", "rompeu para cima"
    elif trig["down"] and price <= trig["down"]:
        level, direction, word = trig["down"], "VENDA", "perdeu"
    else:
        return None
    paper.clear_trigger(symbol)
    notifier.send(f"🔔 <b>{esc(symbol)} {word} {fmt_price(level)}</b> (agora {fmt_price(price)}), "
                  "nível marcado na análise anterior. Reanalisando...")
    return {"direcao": direction, "contrarios": [],
            "gatilhos": [f"Preço {word} o nível {fmt_price(level)} definido como gatilho na análise anterior"]}


def run_symbol(symbol: str) -> None:
    df = market.add_indicators(market.fetch_candles(symbol))
    check_positions(symbol, df)

    trigger_candidate = check_trigger(symbol)
    if trigger_candidate:
        log.info("%s: gatilho de preço atingido. Consultando IA...", symbol)
        evaluate(symbol, df, trigger_candidate, "gatilho")
        return

    if in_cooldown(symbol, df.iloc[-1]["ts"]):
        return
    candidate = triage.find_candidate(df)
    if not candidate:
        return
    if candidate["direcao"] == "COMPRA" and paper.open_position(symbol):
        return  # já está comprado na simulação

    log.info("%s: sinal candidato de %s (%s). Consultando IA...",
             symbol, candidate["direcao"], "; ".join(candidate["gatilhos"]))
    evaluate(symbol, df, candidate, "auto")


def evaluate_pending() -> None:
    """Placar: confere o preço 24h e 72h depois de cada análise."""
    global _last_eval
    if time.time() - _last_eval < EVAL_EVERY_MIN * 60:
        return
    _last_eval = time.time()
    for hours in (24, 72):
        for s in paper.pending_evaluations(hours):
            target = pd.Timestamp(s["created_at"]) + pd.Timedelta(hours=hours)
            try:
                later = market.price_at(s["symbol"], int(target.timestamp() * 1000))
            except ccxt.BaseError as e:
                log.warning("Placar: erro ao buscar %s: %s", s["symbol"], e)
                continue
            if later is None and pd.Timestamp.now(tz="UTC") - target < pd.Timedelta(hours=3):
                continue  # vela ainda não disponível; tenta de novo no próximo ciclo
            ret = (later / s["preco"] - 1) * 100 if later else None
            paper.set_evaluation(s["id"], hours, ret)


def run_cycle() -> None:
    for symbol in paper.get_watchlist():
        try:
            run_symbol(symbol)
        except ccxt.BaseError as e:
            log.warning("%s: erro na corretora: %s", symbol, e)
        except Exception:
            log.exception("%s: erro inesperado", symbol)
    try:
        evaluate_pending()
    except Exception:
        log.exception("Erro ao atualizar o placar")


# ---------------- comandos do Telegram ----------------

HELP = (
    "<b>Comandos</b>\n"
    "/status — posições simuladas abertas\n"
    "/desempenho — resultado do paper trading\n"
    "/placar — o que o preço fez depois das análises da IA\n"
    "/gatilhos — preços que disparam nova análise\n"
    "/perfil — perfil de risco atual\n"
    "🔒 /perfil moderado — troca o perfil (conservador, moderado, agressivo)\n"
    "🔒 /analise BTC — análise da IA agora (não abre operação)\n"
    "/moedas — ativos monitorados\n"
    "🔒 /adicionar XRP DOGE — passa a monitorar (pode mais de uma)\n"
    "🔒 /remover XRP — para de monitorar\n"
    "/id — mostra o ID do chat e o seu\n\n🔒 = só administradores"
)

MAX_SYMBOLS = 30  # limite para não estourar o rate limit da corretora


def resolve_symbol(arg: str) -> str:
    """'btc' -> 'BTC/USD' (ou o par já monitorado com essa moeda, ex.: BTC/USDC)."""
    arg = arg.upper().replace("-", "/")
    if "/" in arg:
        return arg
    for s in paper.get_watchlist():
        if s.split("/")[0] == arg:
            return s
    return f"{arg}/USD"


def cmd_add(args: list[str]) -> None:
    if not args:
        notifier.send("Uso: /adicionar XRP  (ou vários: /adicionar XRP DOGE ADA)")
        return
    added, existing, invalid = [], [], []
    for arg in args:
        symbol = resolve_symbol(arg)
        if symbol in paper.get_watchlist():
            existing.append(symbol)
        elif not market.symbol_exists(symbol):
            invalid.append(symbol)
        elif len(paper.get_watchlist()) >= MAX_SYMBOLS:
            notifier.send(f"Limite de {MAX_SYMBOLS} moedas atingido. Remova alguma antes.")
            break
        else:
            paper.add_symbol(symbol)
            added.append(symbol)
    lines = []
    if added:
        lines.append("✅ Adicionado: " + esc(", ".join(added)))
    if existing:
        lines.append("Já monitorado: " + esc(", ".join(existing)))
    if invalid:
        lines.append(f"❌ Não encontrado na {esc(config.EXCHANGE)}: " + esc(", ".join(invalid))
                     + "\nConfira o código da moeda ou tente outro par, ex.: /adicionar XRP/USDC")
    if lines:
        notifier.send("\n".join(lines))


def cmd_remove(args: list[str]) -> None:
    if not args:
        notifier.send("Uso: /remover XRP")
        return
    for arg in args:
        symbol = resolve_symbol(arg)
        if paper.open_position(symbol):
            notifier.send(f"⚠️ {esc(symbol)} tem uma posição simulada aberta. Ela será encerrada "
                          "quando bater stop/alvo; remova depois disso.")
        elif paper.remove_symbol(symbol):
            notifier.send(f"🗑️ Removido: {esc(symbol)}")
        else:
            notifier.send(f"{esc(symbol)} não estava na lista. Veja /moedas")


def cmd_profile(args: list[str]) -> None:
    current = paper.get_profile()
    if args:
        name = args[0].lower()
        if name not in PROFILES:
            notifier.send("Perfis disponíveis: " + ", ".join(PROFILES))
            return
        paper.set_profile(name)
        notifier.send(f"✅ Perfil alterado: <b>{esc(current)}</b> → <b>{esc(name)}</b>\n"
                      f"{esc(PROFILES[name]['desc'])}\nConfiança mínima para alertar: "
                      f"{esc(paper.min_confidence())}")
        return
    lines = [f"<b>Perfil atual: {esc(current)}</b> (confiança mínima: {esc(paper.min_confidence())})", ""]
    for name, p in PROFILES.items():
        mark = "👉 " if name == current else "• "
        lines.append(f"{mark}<b>{esc(name)}</b>: {esc(p['desc'])}")
    lines += ["", "Trocar: /perfil moderado"]
    if config.MIN_CONFIDENCE:
        lines.append(f"<i>Obs.: MIN_CONFIDENCE={esc(config.MIN_CONFIDENCE)} no .env sobrepõe o padrão dos perfis.</i>")
    notifier.send("\n".join(lines))


def _fmt_stats(s: dict | None) -> str:
    if not s:
        return "sem dados ainda"
    return f"{s['media']:+.2f}% em média · {s['lucro_pct']:.0f}% com lucro (n={s['n']})"


def cmd_scoreboard(args: list[str]) -> None:
    perfil = args[0].lower() if args and args[0].lower() in PROFILES else None
    sb = paper.scoreboard(perfil)
    title = f"📋 Placar da IA{' · perfil ' + perfil if perfil else ''}"
    if not sb["avaliados_24h"]:
        notifier.send(f"<b>{esc(title)}</b>\n{sb['total']} análises registradas. O placar começa a aparecer "
                      "24h depois das primeiras análises.")
        return
    a, d, va, vb = sb["aprovadas"], sb["descartadas"], sb["vies_alta"], sb["vies_baixa"]
    acerto = lambda x: f"{x:.0f}%" if x is not None else "-"  # noqa: E731
    notifier.send("\n".join([
        f"<b>{esc(title)}</b>",
        f"{sb['total']} análises · {sb['avaliados_24h']} já conferidas em 24h",
        f"<i>\"Com lucro\" = subiu mais que as taxas ({sb['fee']:.1f}%).</i>",
        "",
        "<b>Sinais de compra que a IA APROVOU</b>",
        f"24h: {_fmt_stats(a[24])}",
        f"72h: {_fmt_stats(a[72])}",
        "",
        "<b>Sinais de compra que a IA DESCARTOU</b>",
        f"24h: {_fmt_stats(d[24])}",
        f"72h: {_fmt_stats(d[72])}",
        "",
        f"<b>Viés de alta (≥ +30)</b>: acertou a direção em {acerto(va['acerto'])} (24h)",
        f"24h: {_fmt_stats(va[24])}",
        f"<b>Viés de baixa (≤ −30)</b>: acertou a direção em {acerto(vb['acerto'])} (24h)",
        f"24h: {_fmt_stats(vb[24])}",
        "",
        "<i>Se as descartadas sobem tanto quanto as aprovadas, a IA está cautelosa demais. "
        "Se sobem menos, o filtro está funcionando. Com poucos dados (n pequeno), não conclua nada.</i>",
    ]))


def cmd_triggers() -> None:
    trigs = paper.all_triggers()
    if not trigs:
        notifier.send("Nenhum gatilho de preço ativo.")
        return
    lines = ["<b>🔔 Gatilhos ativos</b> (disparam nova análise)"]
    for t in trigs:
        parts = []
        if t["up"]:
            parts.append(f"acima de {fmt_price(t['up'])}")
        if t["down"]:
            parts.append(f"abaixo de {fmt_price(t['down'])}")
        lines.append(f"• {esc(t['symbol'])}: {' ou '.join(parts)}")
    notifier.send("\n".join(lines))


ADMIN_COMMANDS = {"/analise", "/adicionar", "/remover"}


def is_admin(user_id) -> bool:
    return not config.TELEGRAM_ADMIN_IDS or str(user_id) in config.TELEGRAM_ADMIN_IDS


def handle_command(text: str, user_id=None) -> None:
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower()

    needs_admin = cmd in ADMIN_COMMANDS or (cmd == "/perfil" and len(parts) > 1)
    if needs_admin and not is_admin(user_id):
        notifier.send("🔒 Apenas administradores podem usar esse comando.")
        return

    if cmd in ("/start", "/ajuda", "/help"):
        notifier.send(HELP)

    elif cmd == "/moedas":
        symbols = paper.get_watchlist()
        notifier.send(f"<b>Monitorando {len(symbols)} moedas</b> ({esc(config.EXCHANGE)}, "
                      f"{esc(config.TIMEFRAME)}):\n" + esc(", ".join(symbols) or "nenhuma")
                      + "\n\n/adicionar XRP · /remover XRP")

    elif cmd == "/adicionar":
        cmd_add(parts[1:])

    elif cmd == "/remover":
        cmd_remove(parts[1:])

    elif cmd == "/perfil":
        cmd_profile(parts[1:])

    elif cmd == "/placar":
        cmd_scoreboard(parts[1:])

    elif cmd == "/gatilhos":
        cmd_triggers()

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
        lines = [
            "<b>📊 Paper trading</b>",
            f"Perfil atual: {esc(paper.get_profile())}",
            f"Análises da IA: {p['analises']} · Alertas enviados: {p['alertas']}",
            f"Trades fechados: {p['trades_fechados']} ({p['ganhos']} ganhos / {p['perdas']} perdas)",
            f"Taxa de acerto: {p['taxa_acerto']:.0f}%",
            f"Ganho médio: {p['ganho_medio_usd']:+.2f} USD · Perda média: {p['perda_media_usd']:+.2f} USD",
            f"<b>Resultado total: {p['pnl_total_usd']:+.2f} USD</b> "
            f"(com {config.PAPER_TRADE_USD:.0f} USD por operação, taxas incluídas)",
        ]
        if len(p["por_perfil"]) > 1:
            lines += ["", "<b>Por perfil:</b>"] + [
                f"• {esc(k)}: {v['n']} trades, {v['pnl']:+.2f} USD" for k, v in p["por_perfil"].items()]
        lines += ["", "Veja também /placar"]
        notifier.send("\n".join(lines))

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
        evaluate(symbol, df, triage.find_candidate(df), "manual")

    else:
        notifier.send(HELP)


def poll_commands(offset: int | None, until: datetime) -> int | None:
    while datetime.now() < until:
        remaining = int((until - datetime.now()).total_seconds())
        for upd in notifier.get_updates(offset, timeout=max(1, min(30, remaining))):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            text = msg.get("text", "")
            chat_id = msg.get("chat", {}).get("id")
            user_id = msg.get("from", {}).get("id")
            # /id funciona em qualquer chat, para descobrir o ID de um grupo novo
            if text.split("@")[0].strip().lower() == "/id":
                notifier.send(f"ID deste chat: <code>{chat_id}</code>\nSeu ID de usuário: <code>{user_id}</code>",
                              chat_id=chat_id)
                continue
            if msg.get("migrate_to_chat_id"):
                log.warning("O grupo virou supergrupo. Novo TELEGRAM_CHAT_ID: %s", msg["migrate_to_chat_id"])
            # Fora isso, só obedece ao chat configurado
            if str(chat_id) != str(config.TELEGRAM_CHAT_ID):
                continue
            if text.startswith("/"):
                try:
                    handle_command(text, user_id)
                except Exception:
                    log.exception("Erro no comando %s", msg.get("text"))
                    notifier.send("Erro ao executar o comando. Veja os logs.")
    return offset


def main() -> None:
    symbols = paper.get_watchlist()
    log.info("Iniciando: %s %s %s, a cada %d min, perfil %s",
             config.EXCHANGE, config.TIMEFRAME, symbols, config.CHECK_INTERVAL_MIN, paper.get_profile())
    notifier.send(f"🤖 Monitor de cripto iniciado\n{esc(', '.join(symbols))} · {esc(config.TIMEFRAME)} · "
                  f"perfil {esc(paper.get_profile())}\nEnvie /ajuda para ver os comandos.")
    offset = None
    for upd in notifier.get_updates(None, timeout=0):  # ignora comandos antigos
        offset = upd["update_id"] + 1
    while True:
        started = datetime.now()
        run_cycle()
        offset = poll_commands(offset, started + timedelta(minutes=config.CHECK_INTERVAL_MIN))


if __name__ == "__main__":
    main()
