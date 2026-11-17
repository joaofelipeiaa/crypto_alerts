"""Classe de cada ativo monitorado: cripto (corretora/DEX) ou B3 (ações, FIIs e ETFs)."""
import re

# Ticker da B3: 4 letras + 1 ou 2 dígitos (PETR4, VALE3, BOVA11, KNRI11). Cripto sempre tem "/" (BTC/USD).
_B3 = re.compile(r"^[A-Z]{4}\d{1,2}$")


def is_b3(symbol: str) -> bool:
    return bool(_B3.match(symbol.upper()))
