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
# (início, fim) do último período em que o bot ficou parado (PC suspenso/desligado), ou None
OFFLINE: tuple[pd.Timestamp, pd.Timestamp] | None = None


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
            f"Resultado: <b>{closed['pnl_usd']:+.2f} USD ({closed['pnl_pct']:+.2f}%)</b>",
            buttons=notifier.trade_button(symbol),
        )


def in_cooldown(symbol: str, candle_ts: pd.Timestamp) -> bool:
    last = paper.last_signal_candle(symbol)
    if not last:
        return False
    return (candle_ts - pd.Timestamp(last)).total_seconds() < config.COOLDOWN_CANDLES * TF_SECONDS


def history_for_ai(symbol: str, price: float) -> list[dict]:
    """Análises anteriores do ativo, com quanto o preço andou desde cada uma."""
    out = []
    for r in paper.recent_analyses(symbol):
        item = {
            "quando": notifier.fmt_time(pd.Timestamp(r["created_at"])),
            "origem": r["source"], "acao": r["acao"], "confianca": r["confianca"], "vies": r["vies"],
            "preco_na_epoca": r["preco"],
            "variacao_desde_entao_pct": round((price / r["preco"] - 1) * 100, 2) if r["preco"] else None,
            "resumo": (r["resumo"] or "")[:300],
        }
        if r["gatilho_alta"] or r["gatilho_baixa"]:
            item["gatilhos_marcados"] = {"alta": r["gatilho_alta"], "baixa": r["gatilho_baixa"]}
        out.append(item)
    return out


def evaluate(symbol: str, df: pd.DataFrame, candidate: dict | None, source: str) -> dict | None:
    """Consulta a IA, envia o alerta, atualiza o paper trading, salva o sinal e os gatilhos.

    source: 'auto' (ciclo normal), 'gatilho' (preço cruzou um gatilho) ou 'manual' (/analise).
    """
    perfil = paper.get_profile()
    position = paper.open_position(symbol)
    snap = market.snapshot(df)
    snap["tendencia_maior"] = market.higher_tf_context(symbol) or "indisponível"
    if paper.get_dex(symbol):
        try:
            snap["token_de_dex"] = market.dex_context(symbol)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: sem dados de liquidez da DEX (%s)", symbol, e)
    try:
        # preço ao vivo: a vela atual ainda está aberta e pode ter andado bastante desde o fechamento
        price = market.last_price(symbol)
        snap["preco_agora"] = price
        snap["variacao_desde_fechamento_pct"] = round((price / snap["preco"] - 1) * 100, 2)
    except Exception as e:  # noqa: BLE001
        log.warning("%s: sem preço ao vivo (%s), usando o fechamento", symbol, e)
        price = snap["preco"]
    # alvo precisa pagar as taxas de ida e volta com folga
    fee_rt = 2 * paper.fee_pct(symbol)
    min_target_pct = round(config.MIN_TARGET_FEE_MULT * fee_rt, 2)
    snap["custos_operacao"] = {"taxa_ida_e_volta_pct": fee_rt, "alvo_minimo_pct": min_target_pct}
    holders = paper.holders(symbol)
    carteira = [{"nome": h["name"], "preco_medio": h["avg_price"],
                 "resultado_atual_pct": round((price / h["avg_price"] - 1) * 100, 2)} for h in holders]
    decision = agent.analyze(symbol, config.TIMEFRAME, snap, market.fear_greed(), candidate, position, perfil,
                             historico=history_for_ai(symbol, price), carteira=carteira,
                             source=source)
    if not decision:
        if source == "manual":
            notifier.send("A IA não conseguiu concluir a análise agora. Veja os logs.")
        else:
            paper.save_signal(symbol, df.iloc[-1]["ts"].isoformat(), candidate, None, price, False, source)
        return None

    log.info("%s [%s]: IA decidiu %s (confiança %s, viés %+d)", symbol, source,
             decision["acao"], decision["confianca"], decision["vies"])
    if decision["acao"] == "COMPRA":
        alvo_pct = (decision["alvo"] / price - 1) * 100 if decision["alvo"] else 0.0
        if alvo_pct < min_target_pct:
            log.info("%s: COMPRA descartada, alvo +%.2f%% abaixo do mínimo +%.2f%%", symbol, alvo_pct, min_target_pct)
            decision["acao"] = "AGUARDAR"
            decision["resumo"] = (f"Compra descartada: alvo de {alvo_pct:+.1f}% não cobre as taxas "
                                  f"(mínimo +{min_target_pct:.1f}%). " + decision["resumo"])

    # gatilho colado no preço dispara com ruído; exige distância mínima de meio ATR (e 0,5%)
    min_dist = max(0.5 * snap["atr"], 0.005 * price)
    # com compra aberta, o stop é o plano: o gatilho de baixa não pode ficar acima dele
    opens = (source != "manual" and decision["acao"] == "COMPRA" and not position
             and paper.confidence_ok(decision["confianca"]))
    stop = decision["stop_loss"] if opens else (position or {}).get("stop_loss")
    down = decision["gatilho_baixa"]
    if stop and down and down > stop:
        log.info("%s: gatilho de baixa %s acima do stop %s; usando o stop", symbol, down, stop)
        down = stop
    trigger = paper.set_trigger(symbol, price, decision["gatilho_alta"], down, min_dist)

    actionable = (source != "manual" and decision["acao"] in ("COMPRA", "VENDA")
                  and paper.confidence_ok(decision["confianca"]))
    # VENDA só interessa a quem tem a moeda: sem ninguém na /carteira, não manda mensagem
    worth_alert = actionable and (decision["acao"] == "COMPRA" or bool(holders))
    alerted = source == "manual" or worth_alert or config.SEND_REJECTED
    if alerted:
        notifier.send(notifier.format_decision(symbol, price, decision, candidate, perfil, trigger,
                                               dex=bool(paper.get_dex(symbol)), holders=holders),
                      buttons=notifier.trade_button(symbol))

    if actionable and decision["acao"] == "COMPRA" and not position:
        paper.open_trade(symbol, price, decision["stop_loss"], decision["alvo"])
    elif actionable and decision["acao"] == "VENDA" and position:
        closed = paper.close_trade(position, price, "sinal de venda")
        notifier.send(f"📤 Paper trade de {esc(symbol)} encerrado pelo sinal de venda: "
                      f"<b>{closed['pnl_usd']:+.2f} USD ({closed['pnl_pct']:+.2f}%)</b>",
                      buttons=notifier.trade_button(symbol))

    paper.save_signal(symbol, df.iloc[-1]["ts"].isoformat(), candidate, decision, price, alerted, source)
    return decision


def ensure_auto_trigger(symbol: str, df: pd.DataFrame) -> None:
    """Moeda sem gatilho e sem análise recente ganha níveis na máxima/mínima do tempo gráfico maior.

    Não usa IA: ela só é chamada se o preço romper a faixa. Depois de uma análise, valem os níveis
    da IA; se ela não deu nenhum, a faixa automática volta após AUTO_TRIGGER_HOURS sem análise.
    """
    if not config.AUTO_TRIGGERS or paper.get_trigger(symbol):
        return
    last = paper.last_analysis_at(symbol)
    if last and pd.Timestamp.now(tz="UTC") - pd.Timestamp(last) < pd.Timedelta(hours=config.AUTO_TRIGGER_HOURS):
        return
    ctx = market.higher_tf_context(symbol)
    if not ctx:
        return
    price = float(df.iloc[-1]["close"])
    trig = paper.set_trigger(symbol, price, ctx["maxima_30_velas"], ctx["minima_30_velas"],
                             min_dist=0.005 * price, origin="auto", ttl_hours=config.AUTO_TRIGGER_HOURS)
    if trig:
        log.info("%s: gatilho automático (30 velas de %s): acima de %s / abaixo de %s",
                 symbol, ctx["timeframe"], trig["up"], trig["down"])


def cross_time(df: pd.DataFrame, since: str, level: float, up: bool) -> pd.Timestamp | None:
    """Abertura da primeira vela (depois de 'since') que tocou o nível. None = só na vela atual."""
    after = df[df["ts"] >= pd.Timestamp(since)]
    hit = after[after["high"] >= level] if up else after[after["low"] <= level]
    return hit.iloc[0]["ts"] if len(hit) else None


def check_trigger(symbol: str, df: pd.DataFrame) -> dict | None:
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
    # numa queda (ou alta) contínua, cada análise marca um nível novo que é cruzado logo depois:
    # no máximo uma análise por gatilho por moeda a cada TRIGGER_MIN_INTERVAL_MIN (o gatilho fica guardado)
    last = paper.last_analysis_at(symbol, source="gatilho")
    if last and pd.Timestamp.now(tz="UTC") - pd.Timestamp(last) < pd.Timedelta(minutes=config.TRIGGER_MIN_INTERVAL_MIN):
        return None
    paper.clear_trigger(symbol)
    if trig.get("origin") == "auto":
        extreme = "máxima" if direction == "COMPRA" else "mínima"
        origin_txt = f"{extreme} recente do tempo gráfico maior"
        gatilho = (f"Preço {word} a {extreme} das últimas 30 velas do tempo gráfico maior ({fmt_price(level)}): "
                   "rompimento da faixa de preço dos últimos dias")
    else:
        origin_txt = "nível marcado na análise anterior"
        gatilho = f"Preço {word} o nível {fmt_price(level)} definido como gatilho na análise anterior"
    # quando o nível foi cruzado: se faz tempo (bot parado), o alerta é sobre um movimento antigo
    crossed = cross_time(df, trig["created_at"], level, direction == "COMPRA")
    now = pd.Timestamp.now(tz="UTC")
    when, late = "", None
    if crossed is not None and now - crossed > pd.Timedelta(seconds=2 * TF_SECONDS):
        hours = (now - crossed).total_seconds() / 3600
        when = f" · cruzou por volta de {notifier.fmt_time(crossed)} (há ~{hours:.0f}h)"
        late = f"Movimento de {notifier.fmt_time(crossed)}, há ~{hours:.0f}h"
        if OFFLINE and crossed < OFFLINE[1]:
            late += (f", enquanto o bot estava parado ({notifier.fmt_time(OFFLINE[0])} a "
                     f"{notifier.fmt_time(OFFLINE[1], with_date=False)})")
        late += ". O preço pode já ter se acomodado depois disso."
        gatilho += f" por volta de {notifier.fmt_time(crossed)} (horário local, há ~{hours:.0f}h)"
    log.info("%s %s %s (agora %s), %s%s", symbol, word, level, price, origin_txt, when)
    cand = {"direcao": direction, "contrarios": [], "gatilhos": [gatilho],
            "rompimento": f"{symbol} {word} {fmt_price(level)} ({origin_txt}){when}"}
    if late:
        cand["aviso_atraso"] = late
    return cand


def run_symbol(symbol: str) -> None:
    df = market.add_indicators(market.fetch_candles(symbol))
    check_positions(symbol, df)
    ensure_auto_trigger(symbol, df)

    trigger_candidate = check_trigger(symbol, df)
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


def detect_offline() -> None:
    """Percebe se o bot ficou parado (PC suspenso/desligado) desde o último ciclo e avisa uma vez."""
    global OFFLINE
    now = pd.Timestamp.now(tz="UTC")
    last = paper.get_meta("last_cycle")
    paper.set_meta("last_cycle", now.isoformat())
    gap_limit = pd.Timedelta(minutes=max(15, 3 * config.CHECK_INTERVAL_MIN))
    if not last or now - pd.Timestamp(last) <= gap_limit:
        OFFLINE = None
        return
    OFFLINE = (pd.Timestamp(last), now)
    hours = (now - OFFLINE[0]).total_seconds() / 3600
    log.warning("Bot ficou parado de %s a %s (%.1fh)", OFFLINE[0], now, hours)
    # sem mensagem própria: os alertas afetados já trazem o aviso ⏰ com o horário do movimento


def run_cycle() -> None:
    detect_offline()
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
    "/carteira — suas moedas (o VENDA só é avisado para quem tem a moeda)\n"
    "/carteira ZCAT 0.0185 — registra que você tem ZCAT, com preço médio 0.0185\n"
    "/carteira remover ZCAT — tira da sua carteira\n"
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
    added, existing, invalid, dex_lines = [], [], [], []
    for arg in args:
        symbol = resolve_symbol(arg)
        if symbol in paper.get_watchlist():
            existing.append(symbol)
            continue
        if len(paper.get_watchlist()) >= MAX_SYMBOLS:
            notifier.send(f"Limite de {MAX_SYMBOLS} moedas atingido. Remova alguma antes.")
            break
        if market.symbol_exists(symbol):
            paper.add_symbol(symbol)
            added.append(symbol)
            continue
        if not (config.DEX_ENABLED and symbol.endswith("/USD")):
            invalid.append(symbol)
            continue
        try:
            info, problem = market.find_dex(symbol.split("/")[0])
        except Exception as e:  # noqa: BLE001
            log.warning("Busca em DEX falhou para %s: %s", symbol, e)
            info, problem = None, "não encontrado em DEX"
        if not info:
            invalid.append(symbol)
        elif problem:
            dex_lines.append(f"⛔ {esc(symbol)} ({esc(info['name'])}, {esc(info['network'])}): {esc(problem)}.")
        else:
            paper.add_dex(info)
            paper.add_symbol(symbol)
            dex_lines.append(
                f"✅ Adicionado: {esc(symbol)}  ⚠️ <b>token de DEX</b>\n"
                f"{esc(info['name'])} na rede {esc(info['network'])} · volume 24h US$ {info['volume']:,.0f} · "
                f"valor de mercado US$ {info['market_cap']:,.0f}\n"
                f"Contrato: <code>{esc(info['token'])}</code> (confira no app antes de comprar)\n"
                f"Simulação com taxa de {config.DEX_FEE_PCT:.1f}% por lado. Moedas pequenas são mais "
                f"fáceis de manipular: trate os sinais com mais desconfiança.")
    lines = []
    if added:
        lines.append("✅ Adicionado: " + esc(", ".join(added)))
    lines += dex_lines
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
    lines = ["<b>🔔 Gatilhos ativos</b> (disparam nova análise)",
             "🧠 = nível definido pela IA · 📏 = faixa automática dos últimos dias"]
    for t in trigs:
        parts = []
        if t["up"]:
            parts.append(f"acima de {fmt_price(t['up'])}")
        if t["down"]:
            parts.append(f"abaixo de {fmt_price(t['down'])}")
        mark = "📏" if t.get("origin") == "auto" else "🧠"
        lines.append(f"{mark} {esc(t['symbol'])}: {' ou '.join(parts)}")
    notifier.send("\n".join(lines))


ADMIN_COMMANDS = {"/analise", "/adicionar", "/remover"}


def is_admin(user_id) -> bool:
    return not config.TELEGRAM_ADMIN_IDS or str(user_id) in config.TELEGRAM_ADMIN_IDS


def parse_number(s: str) -> float:
    """Aceita 0.0185, 0,0185 e 1.234,56."""
    s = s.strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def cmd_wallet(args: list[str], user_id, name: str) -> None:
    if not args:
        items = paper.holdings_of(user_id)
        if not items:
            notifier.send("Sua carteira está vazia. Registre o que você tem, ex.: /carteira ZCAT 0.0185\n"
                          "O bot passa a avisar VENDA dessas moedas e a IA considera o seu preço médio.")
            return
        lines = [f"<b>💼 Carteira de {esc(name)}</b>"]
        for h in items:
            try:
                now = market.last_price(h["symbol"])
                res = f"agora {fmt_price(now)} (<b>{(now / h['avg_price'] - 1) * 100:+.1f}%</b>)"
            except Exception:  # noqa: BLE001
                res = "preço indisponível"
            lines.append(f"• {esc(h['symbol'])}: médio {fmt_price(h['avg_price'])}, {res}")
        notifier.send("\n".join(lines))
        return

    if args[0].lower() in ("remover", "tirar", "vendi") and len(args) > 1:
        symbol = resolve_symbol(args[1])
        ok = paper.remove_holding(user_id, symbol)
        notifier.send(f"🗑️ {esc(symbol)} saiu da sua carteira." if ok
                      else f"{esc(symbol)} não estava na sua carteira. Veja /carteira")
        return

    if len(args) < 2:
        notifier.send("Uso: /carteira ZCAT 0.0185  (moeda e preço médio de compra)")
        return
    symbol = resolve_symbol(args[0])
    try:
        avg = parse_number(args[1])
        qty = parse_number(args[2]) if len(args) > 2 else None
    except ValueError:
        notifier.send("Não entendi o preço. Ex.: /carteira ZCAT 0.0185")
        return
    if avg <= 0:
        notifier.send("O preço médio precisa ser maior que zero.")
        return
    if symbol not in paper.get_watchlist():
        cmd_add([symbol])  # quem tem a moeda precisa que ela seja monitorada
        if symbol not in paper.get_watchlist():
            return
    paper.set_holding(user_id, name, symbol, avg, qty)
    msg = f"💼 Registrado: {esc(name)} tem {esc(symbol)} com preço médio {fmt_price(avg)}."
    try:
        now = market.last_price(symbol)
        msg += f"\nAgora: {fmt_price(now)} ({(now / avg - 1) * 100:+.1f}%)."
        if not 0.2 < now / avg < 5:
            msg += "\n⚠️ O preço médio está muito longe do atual. Confira se não faltou ou sobrou um zero."
    except Exception:  # noqa: BLE001
        pass
    notifier.send(msg + "\nVENDA dessa moeda passa a ser avisado, e a IA considera o seu preço médio.")


def handle_command(text: str, user_id=None, name: str = "membro") -> None:
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

    elif cmd == "/carteira":
        cmd_wallet(parts[1:], user_id, name)

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
                    handle_command(text, user_id, msg.get("from", {}).get("first_name") or "membro")
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
    saved = paper.get_meta("telegram_offset")
    if saved:
        offset: int | None = int(saved)
    else:
        offset = None
        for upd in notifier.get_updates(None, timeout=0):  # ignora comandos antigos na primeira execução
            offset = upd["update_id"] + 1
    while True:
        started = datetime.now()
        run_cycle()
        offset = poll_commands(offset, started + timedelta(minutes=config.CHECK_INTERVAL_MIN))
        if offset is not None:
            paper.set_meta("telegram_offset", str(offset))


if __name__ == "__main__":
    main()
