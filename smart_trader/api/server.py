"""
FastAPI server exposing trading bot state as REST endpoints.

Run standalone:  uvicorn smart_trader.api.server:app --port 8000
Or auto-started by main.py in a background thread.

No HMM regime endpoints. No backtest endpoints. Smart-trader is a live-only
system; use the regime_trader project if you need walk-forward backtesting.
"""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from smart_trader.api.state import store
from smart_trader.settings.config import load_config
from typing import Optional

config = load_config()

app = FastAPI(title="Smart Trader API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.monitoring.api_cors_origins + ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/portfolio")
def get_portfolio():
    """Portfolio overview."""
    s = store.snapshot()
    return {
        "equity": s["equity"],
        "cash": s["cash"],
        "buying_power": s["buying_power"],
        "daily_pnl": s["daily_pnl"],
        "daily_pnl_pct": s["daily_pnl_pct"],
        "allocation_pct": s["allocation_pct"],
        "leverage": s["leverage"],
    }


@app.get("/api/positions")
def get_positions():
    """Open positions."""
    return store.get("positions", [])


@app.get("/api/signals")
def get_signals():
    """Recent trading signals (last 50)."""
    return store.get("recent_signals", [])


@app.get("/api/risk")
def get_risk():
    """Risk status including circuit breakers."""
    return store.get("risk_status", {})


@app.get("/api/system")
def get_system():
    """System status."""
    s = store.snapshot()
    return {
        "ibkr_connected": s["ibkr_connected"],
        "ibkr_account": s["ibkr_account"],
        "uptime_seconds": s["uptime_seconds"],
        "last_cycle_seconds": s["last_cycle_seconds"],
        "next_cycle_time": s["next_cycle_time"],
        "started_at": s["started_at"],
        "broker_mode": s.get("broker_mode", "live-broker"),
    }


@app.get("/api/regime")
def get_regime():
    """Current market-regime snapshot (zone, posture, entries-allowed, SMAs).

    Returns null until the first cycle computes it.
    """
    return store.get("regime", None)


@app.get("/api/smart-money")
def get_smart_money():
    """Top smart-money candidates from the most recent scanner cycle."""
    return store.get("smart_money_candidates", [])


@app.get("/api/smart-money-portfolio")
def get_smart_money_portfolio(top_n_only: bool = False):
    """Smart_Money_Portfolio full-universe snapshot with flags.

    Snapshot is built by main.py each cycle and stored in api_state. The
    `currently_held` flag is derived per request from the live position set.
    Query param `top_n_only=true` returns only in_top_n=True stocks.
    """
    snapshot = store.get("portfolio_snapshot")
    positions = store.get("positions", []) or []
    held = {p.get("symbol") for p in positions if p.get("symbol")}
    cfg_top_n = config.smart_money.top_n_size

    if not snapshot:
        return {
            "generated_at": None,
            "top_n_size": cfg_top_n,
            "universe_size": 0,
            "stocks": [],
        }

    stocks = snapshot.get("stocks", [])
    if top_n_only:
        stocks = [s for s in stocks if s.get("in_top_n")]

    # Overlay currently_held derived from live positions + entry_triggered bool
    out_stocks = []
    for s in stocks:
        entry = s.get("optimal_entry_price")
        current = s.get("current_price")
        entry_triggered = bool(
            s.get("in_top_n")
            and entry is not None
            and current is not None
            and current <= entry
        )
        out_stocks.append({
            **s,
            "currently_held": s.get("symbol") in held,
            "entry_triggered": entry_triggered,
        })

    return {
        "generated_at": snapshot.get("generated_at"),
        "top_n_size": snapshot.get("top_n_size", cfg_top_n),
        "universe_size": snapshot.get("universe_size", len(snapshot.get("stocks", []))),
        "stocks": out_stocks,
    }


@app.get("/api/agent-commentary")
def get_agent_commentary():
    """Latest AI cycle commentary."""
    commentary = store.get("agent_commentary")
    if commentary is None:
        return {"content": None, "timestamp": None, "cycle_number": None, "status": "unavailable"}
    return commentary


@app.get("/api/agent-status")
def get_agent_status():
    """Per-component Qwen agent status."""
    status = store.get("agent_status")
    if status is None:
        return {"catalyst": "disabled", "arbitration": "disabled", "commentary": "disabled", "last_cycle_timestamp": None}
    return status


@app.get("/api/alerts")
def get_alerts():
    """Recent alert history."""
    return store.get("alert_history", [])


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Static file serving for React dashboard (single-port deployment)
# ---------------------------------------------------------------------------
# The static directory contains the Vite build output (copied during Docker build).
# Mount AFTER all /api/* routes so API takes precedence.

_static_dir = Path(__file__).resolve().parent.parent / "static"

if _static_dir.is_dir():
    # SPA catch-all: serve index.html for any non-API, non-file route
    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        """Serve React index.html for client-side routing (SPA fallback)."""
        # If the requested path maps to an actual file in static/, serve it
        file_path = _static_dir / full_path
        if full_path and file_path.is_file():
            return FileResponse(file_path)
        # Otherwise serve index.html for React Router to handle
        index_path = _static_dir / "index.html"
        if index_path.is_file():
            return FileResponse(index_path)
        return {"detail": "Not found"}

    # Mount static assets (JS, CSS, images) — this handles /assets/* etc.
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
