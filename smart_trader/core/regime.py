"""Market regime classifier.

Classifies a regime proxy (e.g. SPY) into one of three states from its price
relative to the 50- and 200-day SMAs, and decides whether NEW entries are
allowed given a risk *posture*. Existing positions and exits are never gated by
this module — it only governs opening new risk.

The three states map to what is knowable in real time:

    BULL       close > 200-SMA                      established uptrend
    AMBIGUOUS  50-SMA < close < 200-SMA             recovery OR bear rally —
                                                    indistinguishable in real time
    BEAR       close < 50-SMA                        clear downtrend

The AMBIGUOUS zone is the whole decision. A sustainable recovery and a
bear-market rally look identical there (both reclaim the 50-SMA), so we don't
pretend to predict it — we expose the state and let a *posture* choose:

    defensive  block entries in AMBIGUOUS  → equivalent to the strict
               "above 50 AND 200" gate (protects capital, misses early
               recoveries off a bottom)
    aggressive allow entries in AMBIGUOUS  → equivalent to the plain "above
               50-SMA" gate (captures recoveries, eats bear-rally losses)

`sma200_rising` is reported as context (the slow trend turning up is a *hint*
of recovery) but deliberately does NOT drive the gate: backtests showed the
200-SMA slope lags too much to flag a recovery while it's still actionable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

BULL = "bull"
AMBIGUOUS = "ambiguous"
BEAR = "bear"

DEFENSIVE = "defensive"
AGGRESSIVE = "aggressive"


@dataclass
class RegimeState:
    zone: str               # "bull" | "ambiguous" | "bear"
    posture: str            # "defensive" | "aggressive"
    entries_allowed: bool   # whether NEW entries are permitted this regime
    close: float
    sma_50: float
    sma_200: float
    above_50: bool
    above_200: bool
    sma200_rising: bool     # context only — does not drive the gate
    reason: str

    def to_dict(self) -> dict:
        return {
            "zone": self.zone,
            "posture": self.posture,
            "entries_allowed": self.entries_allowed,
            "close": round(self.close, 2),
            "sma_50": round(self.sma_50, 2),
            "sma_200": round(self.sma_200, 2),
            "above_50": self.above_50,
            "above_200": self.above_200,
            "sma200_rising": self.sma200_rising,
            "reason": self.reason,
        }


def classify_regime(
    close: pd.Series,
    posture: str = DEFENSIVE,
    slope_lookback: int = 20,
) -> Optional[RegimeState]:
    """Classify the current regime from a daily close series.

    Returns None when there isn't enough history to compute a 200-SMA plus the
    slope lookback — callers should treat None as "fail open" (allow entries),
    never as "block", so a data gap can't silently halt trading.
    """
    if close is None or len(close) < 200 + slope_lookback:
        return None

    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()

    last_close = float(close.iloc[-1])
    last_50 = float(sma50.iloc[-1])
    last_200 = float(sma200.iloc[-1])
    if pd.isna(last_50) or pd.isna(last_200):
        return None

    prior_200 = float(sma200.iloc[-1 - slope_lookback])
    sma200_rising = (not pd.isna(prior_200)) and last_200 >= prior_200

    above_50 = last_close > last_50
    above_200 = last_close > last_200

    if above_200:
        zone = BULL
    elif above_50:
        zone = AMBIGUOUS
    else:
        zone = BEAR

    posture = posture if posture in (DEFENSIVE, AGGRESSIVE) else DEFENSIVE

    if zone == BULL:
        entries_allowed = True
        reason = "Bull: above 200-SMA — entries allowed."
    elif zone == BEAR:
        entries_allowed = False
        reason = "Bear: below 50-SMA — entries blocked."
    else:  # AMBIGUOUS
        entries_allowed = posture == AGGRESSIVE
        trend = "rising" if sma200_rising else "falling"
        if entries_allowed:
            reason = (
                f"Ambiguous (below 200-SMA, 200-SMA {trend}); aggressive "
                f"posture — entries allowed (recovery bet)."
            )
        else:
            reason = (
                f"Ambiguous (below 200-SMA, 200-SMA {trend}); defensive "
                f"posture — entries blocked (strict 50+200)."
            )

    return RegimeState(
        zone=zone,
        posture=posture,
        entries_allowed=entries_allowed,
        close=last_close,
        sma_50=last_50,
        sma_200=last_200,
        above_50=above_50,
        above_200=above_200,
        sma200_rising=sma200_rising,
        reason=reason,
    )
