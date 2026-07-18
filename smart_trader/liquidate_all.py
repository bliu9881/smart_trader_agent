#!/usr/bin/env python3
"""
Liquidate all positions in the IBKR paper trading account and reset local state.

Usage:
    python3 -m smart_trader.liquidate_all          # dry-run (default)
    python3 -m smart_trader.liquidate_all --execute # actually send orders
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from smart_trader.broker.ibkr_client import IBKRClient
from smart_trader.broker.order_executor import OrderExecutor
from smart_trader.settings.config import AppConfig, load_config
from smart_trader.settings.credentials import load_credentials

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Liquidate all paper trading positions")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit liquidation orders (default is dry-run)",
    )
    args = parser.parse_args()

    config = load_config()
    creds = load_credentials()
    config.broker.host = creds["ibkr_host"]
    config.broker.port = creds["ibkr_port"]
    # Use a separate client ID to avoid conflicts with the running main app
    config.broker.client_id = 99

    # --- Connect ---
    client = IBKRClient(config.broker)
    if not client.connect():
        logger.critical("Failed to connect to IBKR. Is TWS/Gateway running?")
        return 1

    if not client.is_paper:
        logger.critical("Safety check: connected to LIVE account. Aborting.")
        client.disconnect()
        return 1

    equity = client.get_portfolio_value()
    logger.info(f"Account: {client.account_id} | Equity: ${equity:,.2f}")

    # --- Show current positions ---
    positions = client.ib.positions(client.account_id)
    if not positions:
        logger.info("No open positions. Nothing to liquidate.")
        client.disconnect()
        return 0

    logger.info(f"Found {len(positions)} open position(s):")
    for pos in positions:
        symbol = pos.contract.symbol
        qty = pos.position
        avg_cost = pos.avgCost
        logger.info(f"  {symbol:>6s}  qty={qty:>8.0f}  avg_cost=${avg_cost:>10.2f}")

    if not args.execute:
        logger.info("")
        logger.info("DRY RUN — no orders submitted.")
        logger.info("Re-run with --execute to liquidate all positions.")
        client.disconnect()
        return 0

    # --- Cancel all open orders first ---
    executor = OrderExecutor(client)
    logger.info("Cancelling all open orders...")
    executor.cancel_all_orders()
    time.sleep(2)  # give IBKR a moment to process cancellations

    # --- Liquidate all positions ---
    logger.info("Submitting liquidation orders...")
    trades = executor.close_all_positions()
    logger.info(f"Submitted {len(trades)} liquidation order(s)")

    # Wait for fills
    logger.info("Waiting for fills...")
    client.ib.sleep(5)

    for trade in trades:
        status = trade.orderStatus.status
        symbol = trade.contract.symbol
        logger.info(f"  {symbol}: {status}")

    # --- Reset local state ---
    snapshot_path = Path(config.monitoring.state_snapshot_path)
    snapshot_path.write_text(json.dumps({
        "saved_at": datetime.now().isoformat(),
        "peak_equity": None,
        "positions": [],
    }, indent=2))
    logger.info(f"Reset {snapshot_path}")

    # Clear candidate cache
    cache_dir = Path(config.smart_money.disk_cache_dir)
    candidates_path = cache_dir / "candidates.json"
    if candidates_path.exists():
        candidates_path.write_text(json.dumps({
            "generated_at": None,
            "candidates": [],
        }, indent=2))
        logger.info(f"Cleared {candidates_path}")

    client.disconnect()
    logger.info("Done. Account is ready for a fresh start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
