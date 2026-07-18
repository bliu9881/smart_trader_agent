"""One-shot: fetch from all providers, score, and persist to SQLite + Supabase.

Usage:
    python3 -m smart_trader.scripts.fetch_and_store_all

What it does:
  Phase A — Smart-money scanner (5 original providers):
    1. Run SmartMoneyScanner.get_candidates() which fetches from all 5 providers
       (capitol_trades, sec_edgar, berkshire_13f, ark_invest, insider_cluster),
       caches to disk, computes conviction scores, and — when supabase_enabled=True —
       syncs to Supabase tables: smart_money_filings, smart_money_candidates.

  Phase B — Portfolio pipeline (all holdings providers):
    2. Fetch holdings from every enabled provider via HoldingsScraper.
    3. Upsert raw rows to fund_holdings_raw via PortfolioStore.
    4. Score the universe (fetches OHLCV → syncs to Supabase ohlcv_bars).
    5. Compute entry prices for top-N.
    6. Persist snapshot → syncs to Supabase portfolio_stocks.

Supabase tables populated (when credentials are set):
  - smart_money_filings      (Phase A)
  - smart_money_candidates   (Phase A)
  - ohlcv_bars               (Phase B, step 4)
  - portfolio_stocks         (Phase B, step 6)

Safe to run repeatedly — all writes are idempotent or append-only.
No IBKR connection required.
"""
from __future__ import annotations

import logging
import sys
import time
from collections import Counter

from smart_trader.core.entry_calculator import EntryCalculator
from smart_trader.core.holdings_scraper import HoldingsScraper
from smart_trader.core.portfolio_scorer import PortfolioScorer
from smart_trader.core.smart_money import SmartMoneyScanner
from smart_trader.data.ohlcv_store import OHLCVStore
from smart_trader.data.portfolio_store import PortfolioStore
from smart_trader.settings.config import load_config


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("fetch_and_store_all")

    cfg = load_config()

    # ------------------------------------------------------------------ Phase A
    log.info("=" * 60)
    log.info("Phase A — Smart-money scanner (5 original providers)")
    log.info("=" * 60)
    try:
        scanner = SmartMoneyScanner(cfg.smart_money, cfg.risk)
        candidates = scanner.get_candidates(vol_rank=cfg.trader.scanner_vol_rank)
        log.info(f"Scanner returned {len(candidates)} candidates")
        for c in candidates[:10]:
            log.info(
                f"  {c.symbol:6s}  conviction={c.conviction_score:.2f}  "
                f"sources={c.sources}"
            )
        if len(candidates) > 10:
            log.info(f"  ... and {len(candidates) - 10} more")
        # Give background Supabase sync threads time to finish
        time.sleep(3)
    except Exception as e:
        log.warning(f"Scanner phase failed (continuing to portfolio): {e}")

    # ------------------------------------------------------------------ Phase B
    log.info("=" * 60)
    log.info("Phase B — Portfolio pipeline (all holdings providers)")
    log.info("=" * 60)

    scraper = HoldingsScraper(cfg.smart_money)
    ohlcv = OHLCVStore(
        db_path=cfg.data.db_path,
        supabase_sync_enabled=cfg.data.supabase_sync_enabled,
    )
    scorer = PortfolioScorer(cfg.smart_money, ohlcv)
    entry_calc = EntryCalculator(cfg.smart_money, ohlcv)
    store = PortfolioStore(
        db_path=cfg.smart_money.portfolio_db_path,
        retention_days=cfg.smart_money.portfolio_retention_days,
        supabase_sync_enabled=cfg.data.supabase_sync_enabled,
    )

    log.info("-" * 40)
    log.info("Step 1/5 — fetch holdings from every enabled provider")
    log.info("-" * 40)
    holdings = scraper.fetch_all()
    if not holdings:
        log.error("No holdings from any provider — aborting portfolio phase")
        return 1

    per_provider = Counter(h.provider_name for h in holdings)
    log.info("Per-provider row counts:")
    for prov, count in sorted(per_provider.items(), key=lambda x: -x[1]):
        log.info(f"  {prov:<28s} {count:>5d}")
    log.info(f"  TOTAL                          {len(holdings):>5d}")

    log.info("-" * 40)
    log.info("Step 2/5 — upsert raw holdings into fund_holdings_raw")
    log.info("-" * 40)
    inserted = store.store_raw_holdings(holdings)
    log.info(f"Upserted {inserted} rows")

    log.info("-" * 40)
    log.info("Step 3/5 — score universe (may fetch OHLCV for new tickers)")
    log.info("-" * 40)
    scored = scorer.score(holdings, n_providers=scraper.n_enabled_providers)
    log.info(f"Scored {len(scored)} unique tickers")

    log.info("-" * 40)
    log.info("Step 4/5 — compute entry prices for top-N")
    log.info("-" * 40)
    top_n = cfg.smart_money.top_n_size
    top_syms = [s.symbol for s in scored[:top_n] if s.composite_score > 0]
    entry_prices = entry_calc.compute(top_syms)
    log.info(f"Computed entry prices for {len(entry_prices)} top-N stocks")

    log.info("-" * 40)
    log.info("Step 5/5 — persist snapshot (SQLite + Supabase)")
    log.info("-" * 40)
    snap_id = store.save_snapshot(scored, top_n, entry_prices)
    log.info(f"snapshot_id={snap_id}, universe={len(scored)}, top_n={top_n}")

    # Give background Supabase sync threads time to finish
    time.sleep(3)

    log.info("=" * 60)
    log.info("Done. Summary:")
    log.info(f"  scanner candidates: {len(candidates) if 'candidates' in dir() else 'skipped'}")
    log.info(f"  holdings providers: {scraper.n_enabled_providers}")
    log.info(f"  raw holdings:       {inserted} rows")
    log.info(f"  scored universe:    {len(scored)} tickers")
    log.info(f"  snapshot_id:        {snap_id}")
    log.info(f"  supabase_enabled:   {cfg.smart_money.supabase_enabled}")
    log.info(f"  supabase_sync:      {cfg.data.supabase_sync_enabled}")
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
