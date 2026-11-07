"""Salary normalisation to USD per month.

FX rates are approximate constants so that ranking is deterministic and offline. They are
good enough to compare offers, not to quote numbers; refresh them when they drift > ~5%.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jobscout.domain import Salary

FX_RATES_AS_OF = "2026-11"
# 1 unit of currency = N USD
USD_PER_UNIT: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "CHF": 1.13,
    "PLN": 0.25,
    "SEK": 0.095,
    "CZK": 0.043,
    "KZT": 1 / 510,
    "RUB": 1 / 85,
    "UZS": 1 / 12_700,
    "KGS": 1 / 87,
    "JPY": 1 / 150,
    "SGD": 0.75,
    "AED": 0.2723,
    "SAR": 0.2667,
    "QAR": 0.2747,
    "CAD": 0.72,
    "AUD": 0.66,
    "INR": 0.012,
    "GEL": 0.37,
    "AMD": 0.0026,
    "TRY": 0.029,
}
_ALIASES = {
    "RUR": "RUB",
    "₸": "KZT",
    "ТГ": "KZT",
    "TENGE": "KZT",
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "円": "JPY",
}

_PER_MONTH = {"year": 1 / 12, "month": 1.0, "week": 52 / 12, "day": 21.0, "hour": 168.0}


def canonical_currency(code: str) -> str:
    code = code.strip().upper()
    return _ALIASES.get(code, code)


def to_monthly_usd(salary: Salary) -> tuple[float | None, float | None]:
    rate = USD_PER_UNIT.get(canonical_currency(salary.currency))
    factor = _PER_MONTH.get(salary.period)
    if rate is None or factor is None:
        return None, None

    def conv(value: float | None) -> float | None:
        return None if value is None else round(value * rate * factor, 0)

    return conv(salary.min), conv(salary.max)
