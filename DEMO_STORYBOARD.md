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

## Segment 1: Introduction & Trading Thesis (0:00–0:40)

**Show:** Dashboard in browser (http://47.251.3.72:8000)

**Trading facts to hit (the "why this works"):**
- **Insider buys are public.** Corporate insiders — officers, directors, 10%+ owners — must report their own trades to the SEC on **Form 4 within 2 business days**. Insiders sell for many reasons, but they *buy* for essentially one: they expect the stock to go up.
- **Institutions must show their hand.** Managers with **$100M+** must file a **13F within 45 days of each quarter**, revealing what Berkshire Hathaway and other "superinvestors" actually hold.
- **Some funds disclose daily.** ARK Invest publishes full ETF holdings every day; Morningstar's Wide-Moat ETF tracks companies with durable competitive advantages.
- **The gap:** this "smart money" is legally public but scattered across thousands of filings and slow to aggregate — so most people never act on it systematically.
- **Following it isn't enough — timing + risk are the edge:** enter only with technical confirmation (price above the **200-day SMA**, buying **8-EMA pullbacks** or confirmed **gap-ups**), only when the broad market (S&P 500) is in an uptrend, and with strict risk limits.

**Script:**
> "Every quarter, the world's best investors are legally required to show their hand. Corporate insiders report their own trades to the SEC within two business days. Institutions managing over a hundred million dollars disclose their holdings every quarter. Funds like ARK publish theirs daily. This is 'smart money' — and it's all public. The problem is it's buried across thousands of filings and slow to piece together.
>
> Smart Trader Agent aggregates it — SEC Form 4 insider buys, Berkshire Hathaway's 13F, ARK Invest, and Morningstar's Wide-Moat ETF — and scores conviction by how many independent sources agree, weighted by how recent each filing is.
>
> But following smart money isn't enough — timing and risk decide outcomes. So the agent only buys when the technicals confirm: price above its 200-day moving average, on pullbacks to the 8-day EMA or confirmed gap-ups. A market-regime filter blocks new entries unless the S&P 500 is in a healthy uptrend, and a deterministic risk manager caps risk per trade, enforces sector and correlation limits, and halts trading on a drawdown breach. On top of all of that, Qwen Cloud reasons about news catalysts, ranks competing signals, and explains every decision in plain English."

> **Timeline (total ~3:10):** S1 0:00–0:40 · S2 0:40–1:05 · S3 1:05–1:55 · S4 1:55–2:35 · S5 2:35–2:55 · S6 2:55–3:10. Trim within any segment to hold ~3:00.

---

## Segment 2: Architecture — "Gated Mode" (0:40–1:05)

**Show:** Architecture diagram from `architecture.md` (renders inline on GitHub, or export a PNG from https://mermaid.live)

**Script (builds on the intro — don't re-list the sources):**
> "That whole pipeline maps onto three layers. The rule engine turns those public filings into conviction-scored candidates. The Qwen layer adds the judgment — classifying catalysts, ranking competing signals, writing the commentary. And underneath sits the risk manager with absolute veto. We call this **gated mode**: the AI can reason and recommend, but it can never place a trade the rules reject or change a risk limit. AI reasons — rules enforce."

**Highlight (broker fallback):**
> "It connects to Interactive Brokers for paper trading and falls back to a built-in mock broker when IBKR isn't reachable — that's the 'Mock Broker' badge on the dashboard."

---

## Segment 3: Live System Running (1:05–1:55)

**Show:** Terminal with `docker logs -f smart-trader` showing a live cycle
(recorded with `MARKET_HOURS_GATE=0` so a cycle runs on camera — see Recording setup)

**Script:**
> "Here it is running a live cycle. Watch the log — it pulls the smart-money filings, scores conviction across sources, checks the market regime, and applies the technical entry gate."

**Wait for the Qwen log line to appear:**
> "Now Qwen Cloud is called over the DashScope API — classifying the news catalysts and ranking candidates against the current portfolio. Then the commentary generator writes a plain-English summary of the cycle."

**Switch to browser — show the Agent Commentary card:**
> "That summary lands right here on the Agent Commentary card — the agent explaining, in its own words, what it did this cycle and why."

---

## Segment 4: Dashboard Features (1:55–2:35)

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

**Script (tie each panel back to the thesis):**
> "The dashboard makes the whole decision chain observable. Up top, every AI component's health — all green. The Regime panel is the live market gate we mentioned — the S&P above its 200-day average, so entries are on. Smart-Money shows the real scanned candidates with their conviction scores and the fund behind each — like Berkshire's stake in Google. The Signal Feed carries Qwen's ranking reasoning on every signal, and the risk panel shows the circuit breakers standing guard over the portfolio."

> **Say the honesty line here:** "Positions and signals shown are illustrative sample data — the regime, smart-money candidates, and AI commentary are live."

---

## Segment 5: Proof of Alibaba Cloud (2:35–2:55)

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
> "And it all runs on Alibaba Cloud ECS. Here's the instance metadata confirming the Alibaba Cloud machine, the Docker container serving on port 8000, and the logs calling Qwen Cloud's DashScope endpoint."

---

## Segment 6: Key Innovation & Close (2:55–3:10)

**Show:** Return to dashboard

**Script (tie back to the thesis, not a new pitch):**
> "The edge here isn't a black box — it's disciplined execution on public information: aggregate smart money, demand multi-source agreement, confirm with technicals and market regime, and give deterministic risk controls the final say. Qwen adds the reasoning and the transparency — and if Qwen ever goes down, the rule engine keeps running. No single point of failure."

> "Smart Trader Agent — turning public smart-money disclosures into disciplined, explainable, risk-managed decisions. Powered by Qwen Cloud."

---

## Recording Tips

- Use 1920x1080 resolution
- Dark terminal theme matches the dashboard's dark mode
- Keep the browser at ~80% zoom so text is readable
- Upload to YouTube as "unlisted" or "public"
- Total length: ~3:10 with the current script (see the timeline under Segment 1); trim within segments if you need to hold under 3:00
