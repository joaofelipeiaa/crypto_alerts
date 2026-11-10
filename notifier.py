"""Envio e recebimento de mensagens do Telegram (API HTTP direta)."""
import html
import json
import re
import logging

from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import config

TZ = ZoneInfo(config.TIMEZONE)

log = logging.getLogger(__name__)
API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


# Página de negociação de cada corretora ({base}/{quote} vêm do par, ex.: BTC/USD)
TRADE_URLS = {
    "coinbase": "https://www.coinbase.com/advanced-trade/spot/{base}-{quote}",  # só se o slug falhar
    "binance": "https://www.binance.com/en/trade/{base}_{quote}",
    "kraken": "https://pro.kraken.com/app/trade/{base}-{quote}",
}
_coinbase_slugs: dict[str, str] = {}


def _coinbase_slug(base: str) -> str | None:
    """Nome da moeda na URL da Coinbase normal (BTC → bitcoin, POL → polygon-pol)."""
    if base not in _coinbase_slugs:
        try:
            r = requests.get("https://api.coinbase.com/v2/assets/search", timeout=8,
                             params={"base": "USD", "query": base})
            matches = [a for a in r.json().get("data", []) if a.get("symbol", "").upper() == base]
            matches.sort(key=lambda a: not a.get("listed", False))  # listadas primeiro
            if matches and matches[0].get("slug"):
                _coinbase_slugs[base] = matches[0]["slug"]
        except (requests.RequestException, ValueError):
            pass
    return _coinbase_slugs.get(base)


def trade_url(symbol: str) -> str | None:
    if "/" not in symbol:
        return None
    base, quote = symbol.upper().split("/", 1)
    if config.EXCHANGE == "coinbase":
        slug = _coinbase_slug(base)
        if slug:
            return f"https://www.coinbase.com/price/{slug}"
    template = TRADE_URLS.get(config.EXCHANGE)
    return template.format(base=base, quote=quote) if template else None


def trade_button(symbol: str) -> dict | None:
    """Botão do Telegram que abre o par na corretora."""
    url = trade_url(symbol)
    if not url:
        return None
    label = f"📲 Abrir {symbol} na {config.EXCHANGE.capitalize()}"
    return {"inline_keyboard": [[{"text": label, "url": url}]]}


def send(text: str, chat_id: str | int | None = None, buttons: dict | None = None) -> None:
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    extra = {"reply_markup": json.dumps(buttons)} if buttons else {}
    try:
        r = requests.post(f"{API}/sendMessage", timeout=15, data={
            "chat_id": chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            **extra,
        })
        if not r.ok and "parse" in r.text.lower():
            # HTML inválido (ex.: mensagem cortada no meio de uma tag): reenvia como texto puro
            plain = html.unescape(re.sub(r"<[^>]+>", "", text))[:4000]
            r = requests.post(f"{API}/sendMessage", timeout=15, data={
                "chat_id": chat_id, "text": plain, "disable_web_page_preview": "true", **extra,
            })
        if not r.ok:
            log.error("Falha ao enviar Telegram: %s", r.text[:300])
    except requests.RequestException as e:
        log.error("Erro de rede no Telegram: %s", e)


def get_updates(offset: int | None, timeout: int = 30) -> list[dict]:
    try:
        r = requests.get(f"{API}/getUpdates", timeout=timeout + 10,
                         params={"offset": offset, "timeout": timeout})
        return r.json().get("result", []) if r.ok else []
    except requests.RequestException:
        return []


def esc(s) -> str:
    return html.escape(str(s))


_MD_LINK = re.compile(r"\(?\[([^\]]+)\]\((https?://[^)\s]+)\)\)?")


def rich(s) -> str:
    """Escapa o texto e converte links Markdown ([site](url)) em links clicáveis do Telegram."""
    def repl(m: re.Match) -> str:
        url = re.sub(r"[?&]utm_source=[^&]*$", "", m.group(2))
        return f'(<a href="{html.escape(url, quote=True)}">{html.escape(m.group(1))}</a>)'

    out, last = [], 0
    for m in _MD_LINK.finditer(str(s)):
        out.append(esc(str(s)[last:m.start()]))
        out.append(repl(m))
        last = m.end()
    out.append(esc(str(s)[last:]))
    return "".join(out)


def fmt_time(dt: datetime | None = None, with_date: bool = True) -> str:
    """Horário local para as mensagens (ex.: 10/11 08:31)."""
    dt = (dt or datetime.now(TZ)).astimezone(TZ)
    return dt.strftime("%d/%m %H:%M" if with_date else "%H:%M")


def fmt_price(p: float) -> str:
    return f"{p:,.2f}" if p >= 1 else f"{p:.6f}"


def bias_label(v: int) -> str:
    if abs(v) < 15:
        return f"➖ neutro ({v:+d})"
    force = "fraca" if abs(v) < 40 else "moderada" if abs(v) < 70 else "forte"
    return f"📈 alta {force} ({v:+d})" if v > 0 else f"📉 baixa {force} ({v:+d})"


def format_decision(symbol: str, price: float, decision: dict, candidate: dict | None,
                    perfil: str | None = None, trigger: dict | None = None, dex: bool = False,
                    holders: list[dict] | None = None) -> str:
    icon = {"COMPRA": "🟢", "VENDA": "🔴", "AGUARDAR": "⚪"}[decision["acao"]]
    dex_tag = "  ·  ⚠️ DEX" if dex else ""
    lines = [
        f"{icon} <b>{decision['acao']} {esc(symbol)}</b>  ·  confiança <b>{esc(decision['confianca'])}</b>{dex_tag}",
        f"Preço: <code>{fmt_price(price)}</code>  ·  {esc(config.TIMEFRAME)}  ·  🕒 {fmt_time()}",
        f"Viés: <b>{bias_label(decision.get('vies', 0))}</b>",
        "",
        rich(decision["resumo"]),
    ]
    if candidate and candidate.get("aviso_atraso"):
        lines[3:3] = [f"⏰ <i>{esc(candidate['aviso_atraso'])}</i>"]
    if holders:
        who = ", ".join(
            f'<a href="tg://user?id={esc(h["user_id"])}">{esc(h["name"] or "membro")}</a> '
            f'(médio {fmt_price(h["avg_price"])}, {(price / h["avg_price"] - 1) * 100:+.1f}%)'
            for h in holders)
        lines[3:3] = [f"📌 Na carteira de: {who}"]
    if candidate and candidate.get("rompimento"):
        lines[0:0] = [f"🔔 {esc(candidate['rompimento'])}"]
    if candidate:
        if not candidate.get("rompimento"):  # no rompimento, a linha 🔔 já explica
            lines += ["", "<b>Gatilhos técnicos:</b> " + esc("; ".join(candidate["gatilhos"]))]
    if decision["motivos"]:
        lines += ["", "<b>Motivos:</b>"] + [f"• {rich(m)}" for m in decision["motivos"]]
    if decision["riscos"]:
        lines += ["", "<b>Riscos:</b>"] + [f"• {rich(r)}" for r in decision["riscos"]]
    if decision["noticias"]:
        lines += ["", "<b>Notícias:</b>"] + [f"• {rich(n)}" for n in decision["noticias"][:4]]
    if decision["acao"] == "COMPRA" and decision["stop_loss"]:
        risk = (price - decision["stop_loss"]) / price * 100
        lines += ["", f"🛑 Stop: <code>{fmt_price(decision['stop_loss'])}</code> (-{risk:.1f}%)"]
        if decision["alvo"]:
            gain = (decision["alvo"] - price) / price * 100
            lines.append(f"🎯 Alvo: <code>{fmt_price(decision['alvo'])}</code> (+{gain:.1f}%)")
    if trigger:
        parts = []
        if trigger.get("up"):
            parts.append(f"passar de <code>{fmt_price(trigger['up'])}</code> "
                         f"(+{(trigger['up'] / price - 1) * 100:.1f}%)")
        if trigger.get("down"):
            parts.append(f"perder <code>{fmt_price(trigger['down'])}</code> "
                         f"({(trigger['down'] / price - 1) * 100:.1f}%)")
        lines += ["", f"🔔 Reanaliso se {' ou '.join(parts)} (aviso se virar compra ou venda)"]
    footer = f"Perfil {perfil} · " if perfil else ""
    lines += ["", f"<i>{esc(footer)}Sinal automatizado, não é recomendação financeira.</i>"]
    return "\n".join(lines)
