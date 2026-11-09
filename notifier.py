"""Envio e recebimento de mensagens do Telegram (API HTTP direta)."""
import html
import logging

import requests

import config

log = logging.getLogger(__name__)
API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


def send(text: str) -> None:
    try:
        r = requests.post(f"{API}/sendMessage", timeout=15, data={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
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


def fmt_price(p: float) -> str:
    return f"{p:,.2f}" if p >= 1 else f"{p:.6f}"


def format_decision(symbol: str, price: float, decision: dict, candidate: dict | None) -> str:
    icon = {"COMPRA": "🟢", "VENDA": "🔴", "AGUARDAR": "⚪"}[decision["acao"]]
    lines = [
        f"{icon} <b>{decision['acao']} {esc(symbol)}</b>  ·  confiança <b>{esc(decision['confianca'])}</b>",
        f"Preço: <code>{fmt_price(price)}</code>  ·  {esc(config.TIMEFRAME)}",
        "",
        esc(decision["resumo"]),
    ]
    if candidate:
        lines += ["", "<b>Gatilhos técnicos:</b> " + esc("; ".join(candidate["gatilhos"]))]
    if decision["motivos"]:
        lines += ["", "<b>Motivos:</b>"] + [f"• {esc(m)}" for m in decision["motivos"]]
    if decision["riscos"]:
        lines += ["", "<b>Riscos:</b>"] + [f"• {esc(r)}" for r in decision["riscos"]]
    if decision["noticias"]:
        lines += ["", "<b>Notícias:</b>"] + [f"• {esc(n)}" for n in decision["noticias"][:4]]
    if decision["acao"] == "COMPRA" and decision["stop_loss"]:
        risk = (price - decision["stop_loss"]) / price * 100
        lines += ["", f"🛑 Stop: <code>{fmt_price(decision['stop_loss'])}</code> (-{risk:.1f}%)"]
        if decision["alvo"]:
            gain = (decision["alvo"] - price) / price * 100
            lines.append(f"🎯 Alvo: <code>{fmt_price(decision['alvo'])}</code> (+{gain:.1f}%)")
    lines += ["", "<i>Sinal automatizado, não é recomendação financeira.</i>"]
    return "\n".join(lines)
