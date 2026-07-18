"""CLI entry point for the backtest engine.

Usage:
    # Quick run — 3 months, explicit symbols
    python3 -m smart_trader.backtest \\
        --symbols AAPL,MSFT,NVDA \\
        --start 2024-01-01 --end 2024-03-31

    # Full-year with all improvement levers
    python3 -m smart_trader.backtest \\
        --preset broad \\
        --start 2024-01-01 --end 2024-12-31 \\
        --gap-threshold 0.02 \\
        --trail-pct 0.08 \\
        --regime-filter \\
        --output trades.csv

Presets:
    tech    20 large-cap tech / growth names (original default universe)
    broad   30 names across 8 sectors (reduces tech concentration)
    growth  20 high-beta momentum names
    tsx     30 Canadian large caps (TSX, .TO suffix)
    na      26 North American names (top US + top TSX)

Regime gating is per-country: with --regime-filter on, US symbols are gated by
--regime-symbol (default SPY) and Canadian (.TO/.V) symbols by --regime-symbol-ca
(default ^GSPTSE) automatically. A mixed 'na' run gates each sleeve correctly:
    python3 -m smart_trader.backtest --preset na \\
        --start 2024-01-01 --end 2024-12-31 \\
        --gap-threshold 0.02 --trail-pct 0.08 --regime-filter
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from typing import List

from smart_trader.backtest.engine import BacktestEngine
from smart_trader.settings.config import load_config

# ---------------------------------------------------------------------------
# Curated symbol presets
# ---------------------------------------------------------------------------

PRESETS: dict = {
    "tech": [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
        "JPM", "UNH", "V", "NFLX", "AMD", "CRM", "COST", "LLY",
        "AVGO", "MA", "ORCL", "NOW", "ADBE",
    ],
    "broad": [
        # Tech / growth
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
        # Financials
        "JPM", "BAC", "GS", "V", "MA",
        # Healthcare
        "UNH", "JNJ", "LLY", "ABBV",
        # Energy
        "XOM", "CVX", "COP",
        # Consumer
        "HD", "COST", "WMT", "MCD",
        # Industrials
        "BA", "CAT", "GE",
        # Defensives
        "BRK-B", "PG", "KO",
    ],
    "growth": [
        "NVDA", "META", "NFLX", "TSLA", "AMD", "AVGO", "NOW",
        "CRWD", "MSTR", "PLTR", "SNOW", "DDOG", "ZS", "PANW",
        "SMCI", "ARM", "ANET", "AXON", "TTD", "MNDY",
    ],
    "tsx": [
        # Canadian large caps (yfinance .TO suffix). Pair with
        # --regime-symbol ^GSPTSE for a Canada-native regime gate.
        # Banks
        "RY.TO", "TD.TO", "BNS.TO", "BMO.TO", "CM.TO",
        # Energy
        "ENB.TO", "CNQ.TO", "SU.TO", "TRP.TO",
        # Tech
        "SHOP.TO", "CSU.TO", "GIB-A.TO",
        # Materials / mining
        "ABX.TO", "AEM.TO", "WPM.TO", "FNV.TO", "NTR.TO", "TECK-B.TO",
        # Rails / industrials
        "CNR.TO", "CP.TO", "WCN.TO",
        # Telecom
        "BCE.TO", "T.TO",
        # Consumer
        "ATD.TO", "L.TO", "DOL.TO", "QSR.TO",
        # Insurance / utilities
        "MFC.TO", "SLF.TO", "FTS.TO",
    ],
    "na": [
        # North American blend — top US names + top TSX names.
        # US
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
        "JPM", "V", "UNH", "LLY", "XOM", "COST", "HD",
        # Canada
        "RY.TO", "TD.TO", "ENB.TO", "CNQ.TO", "SHOP.TO", "CSU.TO",
        "ABX.TO", "CNR.TO", "BCE.TO", "ATD.TO", "MFC.TO", "FTS.TO",
    ],
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smart Trader — historical backtest of the technical strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Universe
    universe = parser.add_mutually_exclusive_group(required=True)
    universe.add_argument(
        "--symbols",
        help="Comma-separated tickers, e.g. AAPL,MSFT,NVDA",
    )
    universe.add_argument(
        "--preset",
        choices=list(PRESETS),
        help="Named symbol universe: tech (20), broad (30), growth (20), "
             "tsx (30 Canadian), na (26 US+Canada)",
    )

    # Date range & sizing
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument(
        "--initial-equity", type=float, default=100_000.0,
        help="Starting account equity (default: 100000)",
    )

    # Lever 1 — gap-up detection
    parser.add_argument(
        "--gap-threshold", type=float, default=None,
        metavar="FLOAT",
        help="Gap-up minimum overnight gap (default: 0.03). Try 0.02 for more signals.",
    )

    # Lever 2 — trailing stop width
    parser.add_argument(
        "--trail-pct", type=float, default=None,
        metavar="FLOAT",
        help="Trailing stop percentage (default: 0.05). Try 0.08 to let winners run.",
    )

    # Lever 3 — market regime filter
    parser.add_argument(
        "--regime-filter", action="store_true", default=False,
        help="Block new entries when a symbol's country regime proxy is below its SMA.",
    )
    parser.add_argument(
        "--regime-symbol", default="SPY",
        metavar="TICKER",
        help="Regime proxy for US symbols (default: SPY).",
    )
    parser.add_argument(
        "--regime-symbol-ca", default="^GSPTSE",
        metavar="TICKER",
        help="Regime proxy for Canadian (.TO/.V) symbols (default: ^GSPTSE).",
    )
    parser.add_argument(
        "--regime-sma", type=int, default=50,
        metavar="PERIOD",
        help="SMA period for regime check (default: 50).",
    )
    parser.add_argument(
        "--regime-strict", action="store_true", default=False,
        help="Require the regime proxy above BOTH its 50- and 200-day SMA "
             "(stricter; catches bears on the fast average, confirms re-entry "
             "on the slow one). Overrides --regime-sma.",
    )
    parser.add_argument(
        "--regime-adaptive", action="store_true", default=False,
        help="Adaptive 3-state detector (bull/recovery/bear): above 50-SMA AND "
             "(above 200-SMA OR 200-SMA rising). Captures recoveries, blocks "
             "bear rallies. Overrides --regime-sma / --regime-strict.",
    )
    parser.add_argument(
        "--regime-slope-lookback", type=int, default=20,
        metavar="DAYS",
        help="Trading-day lookback for the adaptive 200-SMA slope (default: 20).",
    )

    # Transaction costs (realistic by default — net-of-cost results)
    parser.add_argument(
        "--slippage-bps", type=float, default=5.0, metavar="BPS",
        help="Adverse slippage in basis points on entries + market exits (default: 5).",
    )
    parser.add_argument(
        "--commission-per-share", type=float, default=0.005, metavar="USD",
        help="Per-share commission, IBKR-style (default: 0.005).",
    )
    parser.add_argument(
        "--min-commission", type=float, default=1.0, metavar="USD",
        help="Per-order commission floor (default: 1.0).",
    )
    parser.add_argument(
        "--no-costs", action="store_true", default=False,
        help="Zero out slippage + commissions for a gross/frictionless run.",
    )

    # Output
    parser.add_argument("--output", default="", help="Optional CSV path for per-trade log")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Resolve symbol list
    if args.preset:
        symbols: List[str] = PRESETS[args.preset]
        print(f"Using preset '{args.preset}': {len(symbols)} symbols")
    else:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if not symbols:
        print("Error: no symbols specified.", file=sys.stderr)
        sys.exit(1)

    config = load_config()
    engine = BacktestEngine(config)

    try:
        result = engine.run(
            symbols=symbols,
            start=args.start,
            end=args.end,
            initial_equity=args.initial_equity,
            gap_threshold=args.gap_threshold,
            trail_pct=args.trail_pct,
            regime_filter=args.regime_filter,
            regime_symbol=args.regime_symbol,
            regime_symbol_ca=args.regime_symbol_ca,
            regime_sma_period=args.regime_sma,
            regime_sma_periods=[50, 200] if args.regime_strict else None,
            regime_adaptive=args.regime_adaptive,
            regime_slope_lookback=args.regime_slope_lookback,
            slippage_bps=0.0 if args.no_costs else args.slippage_bps,
            commission_per_share=0.0 if args.no_costs else args.commission_per_share,
            min_commission=0.0 if args.no_costs else args.min_commission,
        )
    except Exception as e:
        print(f"Backtest failed: {e}", file=sys.stderr)
        sys.exit(1)

    result.print_summary()

    if args.output:
        _write_csv(result.trades, args.output)
        print(f"Trade log → {args.output}")


def _write_csv(trades, path: str) -> None:
    fieldnames = [
        "symbol", "signal_type", "entry_date", "entry_price", "shares",
        "stop_loss", "take_profit", "trail_pct",
        "exit_date", "exit_price", "exit_reason", "pnl",
        "entry_commission", "exit_commission", "confidence",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in trades:
            writer.writerow({
                "symbol": t.symbol,
                "signal_type": t.signal_type,
                "entry_date": t.entry_date,
                "entry_price": f"{t.entry_price:.4f}",
                "shares": t.shares,
                "stop_loss": f"{t.stop_loss:.4f}",
                "take_profit": f"{t.take_profit:.4f}",
                "trail_pct": f"{t.trail_pct:.4f}",
                "exit_date": t.exit_date or "",
                "exit_price": f"{t.exit_price:.4f}" if t.exit_price is not None else "",
                "exit_reason": t.exit_reason or "",
                "pnl": f"{t.pnl:.2f}" if t.pnl is not None else "",
                "entry_commission": f"{t.entry_commission:.2f}",
                "exit_commission": f"{t.exit_commission:.2f}",
                "confidence": f"{t.confidence:.2f}",
            })


if __name__ == "__main__":
    main()
