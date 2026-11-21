"""The stocks the morning scan looks at: about 100 of the most heavily traded US shares.

A fixed list on purpose. Prices come from the IEX exchange (via Tiingo), where only very
actively traded names print a trade in most 5-minute windows, and the data plan counts the
symbols used per month. No funds here: the three ETFs have their own bots.
"""

from __future__ import annotations

IN_PLAY: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "INTC", "MU", "QCOM", "TXN", "AMAT", "LRCX", "KLAC", "MRVL", "ON", "SMCI",
    "ORCL", "CRM", "ADBE", "NOW", "PLTR", "SNOW", "PANW", "CRWD", "NET", "SHOP",
    "UBER", "ABNB", "DASH", "COIN", "HOOD", "SOFI", "PYPL", "XYZ", "ROKU", "RBLX",
    "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "V", "MA", "AXP",
    "XOM", "CVX", "COP", "OXY", "SLB", "HAL", "DVN", "FCX", "NEM", "CLF",
    "LLY", "UNH", "JNJ", "PFE", "MRK", "ABBV", "BMY", "AMGN", "GILD", "MRNA",
    "WMT", "COST", "TGT", "HD", "LOW", "NKE", "SBUX", "MCD", "DIS", "CMCSA",
    "BA", "CAT", "DE", "GE", "F", "GM", "RIVN", "LCID", "NIO", "DAL",
    "UAL", "AAL", "CCL", "T", "VZ", "KO", "PEP", "CSCO", "IBM", "DELL",
)
