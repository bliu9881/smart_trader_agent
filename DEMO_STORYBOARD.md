# Demo Video Storyboard (~3 minutes)

Record with screen capture (QuickTime/OBS). Show browser + terminal side by side.

---

## Recording setup (read first)

The live system runs an **hourly** cycle and, by default, **skips cycles when the
US market is closed** (nights, weekends, NYSE holidays) to save API credits. For
recording you need a cycle to actually run on camera, so launch the container in
demo mode:

```bash
docker run -d --name smart-trader --restart unless-stopped \
  -e "DASHSCOPE_API_KEY=$KEY" \
  -e "DEMO_SEED=1" \
  -e "MARKET_HOURS_GATE=0" \      # run around the clock for the recording
  -p 8000:8000 \
  smart-trader-agent python3 -m smart_trader.main dry-run
```

- `MARKET_HOURS_GATE=0` disables the market-hours gate so a cycle runs regardless
  of time — otherwise off-hours logs just show `Market closed — skipping cycle`.
- Alternatively, record **Mon–Fri 9:30–16:00 ET** with the gate on.
- **Honesty note:** with `DEMO_SEED=1`, the **Positions** and **Signals** panels
  show *sample* data to illustrate the UI (the strategy rarely enters a trade in a
  short window). **Regime, smart-money candidates, and AI commentary are real.**
  Narrate accordingly, or say "sample positions shown to illustrate the interface."

---

## Segment 1: Introduction (0:00–0:20)

**Show:** Dashboard in browser (http://47.251.3.72:8000)

**Script:**
> "Smart Trader Agent is an autonomous trading agent powered by Qwen Cloud. It aggregates public smart-money disclosures — SEC insider filings, Berkshire Hathaway 13F, ARK Invest holdings, and Morningstar Wide Moat ETF — then uses Qwen to reason about news catalysts, rank competing signals, and explain its decisions."

---

## Segment 2: Architecture Overview (0:20–0:50)

**Show:** Architecture diagram (from architecture.md, rendered as image)

**Script:**
> "The architecture has three layers. The Rule Engine scrapes public data and scores conviction. The Qwen Agent Layer enhances this with AI-powered catalyst classification, signal ranking, and commentary. The Risk Manager has absolute veto — Qwen can never override safety controls. This is what we call 'gated mode' — AI reasons, rules enforce."

**Highlight:** "The system tries Interactive Brokers first for real paper trading. If unavailable, it falls back to a mock broker — visible right here in the dashboard as 'Mock Broker'."

---

## Segment 3: Live System Running (0:50–1:40)

**Show:** Terminal with `docker logs -f smart-trader` showing a live cycle
(recorded with `MARKET_HOURS_GATE=0` so a cycle runs on camera — see Recording setup)

**Script:**
> "Here's the system running an hourly cycle — gated to US market hours in production to save credits. You can see it scanning 4 data providers, finding candidates, resolving sectors..."

**Wait for Qwen log line to appear:**
> "And here — Qwen Cloud is called via DashScope API. The Signal Arbitrator ranks candidates, and the Commentary Generator produces a plain-English summary of what happened."

**Switch to browser — show the Agent Commentary card updating:**
> "The dashboard shows the AI commentary in real time. It explains entries made, positions held, and why candidates were skipped."

---

## Segment 4: Dashboard Features (1:40–2:20)

**Show:** Walk through dashboard panels

1. **System Status panel** — point out:
   - "Mock Broker" indicator (yellow dot)
   - Catalyst AI: succeeded (green)
   - Arbitration AI: succeeded (green)
   - Commentary AI: succeeded (green)

2. **Regime panel** — point out (this is real live data):
   - Market zone (bull/ambiguous/bear) from SPY vs its 200-SMA
   - "Entries on/off" gate

3. **Smart-Money panel** — real scanned candidates with conviction scores + the
   fund/actor behind each (e.g. Berkshire → GOOGL)

4. **Signal Feed** — point out:
   - Arbitration reasoning below signals
   - Smart money conviction badges

5. **Agent Commentary Card** — show the latest cycle summary (real Qwen output)

6. **Alerts panel** — recent event history

7. **Portfolio Card** — show simulated equity

**Script:**
> "Every component's health is visible. Judges can immediately see which AI components are active and the broker mode. The signal feed includes Qwen's reasoning for why each signal was ranked where it was."

---

## Segment 5: Proof of Alibaba Cloud (2:20–2:40)

**Show:** Terminal SSH session to ECS instance

```bash
# Show it's running on Alibaba Cloud
curl http://100.100.100.200/latest/meta-data/instance-id
# Returns: i-abc123... (ECS instance ID)

# Show the container
docker ps

# Show DashScope API being called
docker logs smart-trader | grep "dashscope-intl"
```

**Script:**
> "The backend runs entirely on Alibaba Cloud ECS. Here's the instance metadata proving it's an Alibaba Cloud machine, the Docker container serving on port 8000, and the logs showing API calls to Qwen Cloud's DashScope endpoint."

---

## Segment 6: Key Innovation (2:40–3:00)

**Show:** Return to dashboard

**Script:**
> "The key innovation is the gated-mode architecture. Qwen handles the nuanced reasoning — catalyst classification, signal prioritization, plain-English explanations — while deterministic risk controls ensure safety. If Qwen goes down, the system continues trading with its rule-based engine. No single point of failure, production-ready from day one."

> "Smart Trader Agent — from smart-money filings to executed trades, fully autonomous, powered by Qwen Cloud."

---

## Recording Tips

- Use 1920x1080 resolution
- Dark terminal theme matches the dashboard's dark mode
- Keep the browser at ~80% zoom so text is readable
- Upload to YouTube as "unlisted" or "public"
- Total length: aim for 2:30–3:00 (judges appreciate conciseness)
