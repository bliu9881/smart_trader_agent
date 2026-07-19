# Smart Trader Agent

> An AI agent that autonomously follows smart-money disclosures from research to execution — powered by Qwen Cloud.

[![Track 4: Autopilot Agent](https://img.shields.io/badge/Track%204-Autopilot%20Agent-blue)](https://qwencloud-hackathon.devpost.com/)
[![Powered by Qwen Cloud](https://img.shields.io/badge/Powered%20by-Qwen%20Cloud-orange)](https://www.qwencloud.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## What it does

Smart Trader Agent is an **autonomous trading agent** that aggregates public smart-money disclosures (SEC Form 4, hedge-fund 13F filings, transparent ETF holdings), reasons over them with Qwen AI, and executes risk-managed paper trades — all in an hourly automated loop that runs during US market hours.

**The Qwen agent operates in gated mode**: it can classify catalysts, rank competing signals, and explain decisions in plain English — but it cannot create trades independently or override the deterministic risk manager. AI reasons, rules enforce safety.

## Architecture

```
┌────────────────────────────────────────────────────┐
│  Rule Engine (Python)                               │
│  4 data providers → conviction scoring → signals    │
└───────────────┬────────────────────────────────────┘
                │ candidates
                ▼
┌────────────────────────────────────────────────────┐
│  Qwen Agent Layer (DashScope API)                   │
│  Catalyst Classifier → Signal Arbitrator            │
│  → Commentary Generator                             │
└───────────────┬────────────────────────────────────┘
                │ ranked + validated
                ▼
┌────────────────────────────────────────────────────┐
│  Risk Manager (absolute veto)                       │
│  Circuit breakers · Sector caps · Correlation       │
└───────────────┬────────────────────────────────────┘
                │ approved only
                ▼
┌────────────────────────────────────────────────────┐
│  Broker (IBKR Paper / Mock)                         │
│  Bracket + trailing stop orders                     │
└────────────────────────────────────────────────────┘
```

## Qwen Cloud Integration

| Component | What Qwen Does | Fallback |
|-----------|---------------|----------|
| **Catalyst Classifier** | Classifies news headlines with nuanced understanding (earnings beat vs. guidance raise vs. partnership) | Regex-based classification |
| **Signal Arbitrator** | Ranks multiple entry signals by priority given portfolio context, sector exposure, and catalyst quality | Pass-through in original order |
| **Commentary Generator** | Explains each cycle's decisions in plain English for the dashboard | "Commentary unavailable" message |

## Features

- **17→4 smart-money data sources** (demo config): SEC Form 4, Berkshire 13F, ARK Invest, Morningstar Wide Moat ETF
- **Conviction scoring**: multi-source agreement model with recency decay
- **Risk management**: 1% per-trade risk, sector caps, correlation checks, circuit breakers, peak-drawdown halt
- **Automatic broker fallback**: tries IBKR paper trading first, falls back to mock broker if unavailable
- **Real-time dashboard**: React 19 + Tailwind CSS with agent commentary card, status indicators, signal feed with AI reasoning
- **Single-container deployment**: Docker image serves both API and dashboard on port 8000

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 20+ (for dashboard build)
- `DASHSCOPE_API_KEY` from [Qwen Cloud](https://www.qwencloud.com/)

### Local Development

```bash
# Install Python deps
pip install -r requirements.txt

# Set your API key
echo "DASHSCOPE_API_KEY=sk-your-key" > .env

# Run (will fall back to mock broker without IBKR)
python3 -m smart_trader.main dry-run

# Dashboard (separate terminal)
cd dashboard-ui && npm install && npm run dev
```

### Docker Deployment

```bash
docker build -t smart-trader-agent .
docker run -e DASHSCOPE_API_KEY=sk-your-key -p 8000:8000 smart-trader-agent
```

Open http://localhost:8000 — dashboard + API on one port.

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Health check |
| `GET /api/portfolio` | Portfolio overview (equity, cash, P&L) |
| `GET /api/positions` | Open positions |
| `GET /api/signals` | Signal feed with AI reasoning |
| `GET /api/system` | System status + broker mode |
| `GET /api/agent-commentary` | Latest AI cycle commentary |
| `GET /api/agent-status` | Per-component Qwen status |
| `GET /api/risk` | Risk status + circuit breakers |
| `GET /api/smart-money` | Scanner candidates |

## Project Structure

```
smart_trader/
├── main.py                    # hourly trading loop orchestrator (market-hours gated)
├── qwen/                      # Qwen AI agent layer
│   ├── client.py              # DashScope HTTP client (retry, timeout, structured errors)
│   ├── catalyst_classifier.py # Qwen-enhanced headline classification
│   ├── signal_arbitrator.py   # Portfolio-aware signal ranking
│   └── commentary_generator.py# Async cycle commentary
├── core/                      # Business logic
│   ├── smart_money.py         # Scanner orchestrator
│   ├── smart_money_providers/ # Data source plugins (4 active in demo)
│   ├── risk_manager.py        # Absolute veto layer
│   ├── signal.py              # Signal dataclass
│   └── catalyst_analyzer.py   # News fetch + classification
├── broker/
│   ├── ibkr_client.py         # Real IBKR connection
│   └── mock_broker.py         # Simulated broker for demo
├── api/
│   └── server.py              # FastAPI + static file serving
└── settings/
    └── config.py              # All config as dataclasses

dashboard-ui/                  # React 19 + Vite + Tailwind v4
Dockerfile                     # Multi-stage build (Node + Python)
```

## Safety

- **Paper trading only** — refuses to start on a live account
- **Risk manager absolute veto** — Qwen cannot override safety controls
- **Gated mode** — AI can filter/rank/annotate but never create trades or modify risk params
- **Circuit breakers** — daily/weekly drawdown limits, peak drawdown halt (requires manual reset)
- **Graceful degradation** — if Qwen fails, system continues with deterministic logic

## Tech Stack

- **Backend**: Python 3.11, FastAPI, httpx
- **AI**: Qwen Cloud (qwen-plus model via DashScope API)
- **Frontend**: React 19, Vite, Tailwind CSS v4
- **Deployment**: Docker, Alibaba Cloud ECS
- **Data**: SEC EDGAR, yfinance, Supabase (optional)

## License

MIT — see [LICENSE](LICENSE)
