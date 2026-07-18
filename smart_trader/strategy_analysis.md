
Strategy Review
Your system has a solid foundation: defense-in-depth risk management, multi-source signal generation, and clean separation of concerns. The architecture is well-thought-out. My recommendations focus on areas where the strategy logic could be tightened or where there are structural gaps that could cost you money in practice.

What's working well
Risk manager as absolute gatekeeper — this is the right design
Circuit breakers with the manual lock file — good intentional friction
Correlation checks preventing concentrated sector bets
Fire-and-forget Supabase sync that never blocks the trading loop
Provider health monitoring catching silent regressions
Recommendations

1. Conviction score formula is dominated by dollar volume, not signal quality

The formula n_sources × 2.0 + log₁₀(dollar_volume + 1) + recency_bonus means a single SEC Form 4 filing of $10M (log₁₀(10M) ≈ 7.0) scores higher than 3 independent sources with $50K each (6.0 + log₁₀(150K) ≈ 11.2 — okay, that works). But the real issue is that a single large insider sale-turned-buy of $1B scores 2.0 + 9.0 + 1.0 = 12.0 while 4 independent sources with $100K total score 8.0 + 5.0 + 1.0 = 14.0. The log scale compresses volume differences so much that a $100K trade and a $10M trade only differ by 2 points. Consider normalizing dollar volume relative to the company's market cap or average daily volume — a $1M insider buy in a $500M company is far more meaningful than $1M in AAPL.

2. Entry orders use limit prices but the market may never come to you

The entry is a limit order at entry_price (the current last close). In a rising market, this fills immediately at market open. In a falling market, it might not fill at all. But the real problem is that entry_price is fetched via get_historical_bars("1 M", "1 day") — the last daily close — not the current bid/ask. By the time the order reaches IBKR, the price could be significantly different. Consider using market_data.get_snapshot() for real-time quotes, or submitting as a market order with a price collar.

3. Ladder-in uses fixed share counts, not risk-adjusted sizing

The DCA engine adds a flat 10 or 20 shares regardless of stock price or portfolio size. Adding 10 shares of a $500 stock ($5,000) is very different from 10 shares of a $20 stock ($200). This should be dollar-based or risk-based — e.g., "add 0.5% of portfolio at -15%" — so the DCA adds are proportional to your account size and the position's risk.

4. No exit strategy beyond stops

The system has entries (smart money, Path A/B/Overlap, ladder-in) and mechanical exits (hard stop at 8%, trailing stop at 5%, take profit at 20%). But there's no signal-driven exit. If a smart-money source files a SELL, or if a stock drops out of the top-N portfolio, or if the conviction score drops below threshold — the system doesn't act on that information. The position just rides until a stop or take-profit hits. Consider generating SELL signals when:

A held stock falls out of the top-N set
A smart-money source files a sell for a held position
Conviction score drops below min_conviction_score for a held position
5. The 5% trailing stop is tight for the holding period implied by the strategy

Smart-money signals (13F filings, congressional trades) are inherently medium-to-long-term signals — these are quarterly disclosures. A 5% trailing stop will get shaken out by normal daily volatility on most stocks. The average daily range for a mid-cap stock is 2-3%, so a 5% trail can trigger in 2 bad days. Consider either:

Widening the trail to 8-12% to match the signal timeframe
Making the trail adaptive based on the stock's ATR (Average True Range)
Using a time-based ratchet: start at 8%, tighten to 5% after 2 weeks of profit
6. The 20% take-profit may be leaving money on the table

A fixed 20% take-profit combined with a 5% trailing stop creates an asymmetric payoff where winners are capped at 20% but the trail can let losers run to -5% (or -8% on a gap). For a smart-money strategy where the thesis is "follow people with better information," you want to let winners run. Consider removing the take-profit entirely and relying on the trailing stop, or setting it much higher (40-50%).

7. Overlap signal's 2× risk multiplier doubles down on correlated risk

When Path A and Path B both fire for the same symbol, the system doubles the position size via risk_multiplier=2.0. This means 2% risk per trade instead of 1%. But the overlap signal is for the same stock — you're not diversifying, you're concentrating. If the thesis is wrong, you lose 2× on a single name. Consider capping the overlap boost at 1.5× or adding a requirement that the stock must have low correlation with existing positions before allowing the boost.

8. Sector map is hardcoded and incomplete

DEFAULT_SECTOR_MAP has only 20 stocks. The _resolve_sector() method falls back to yfinance for unknown symbols, which is good, but the resolved sectors aren't persisted back to the config — they're only cached in memory for the current session. After a restart, every new symbol triggers a yfinance lookup again. Consider persisting resolved sectors to SQLite or the disk cache.

9. No market-hours awareness

The 5-minute cycle runs 24/7 but US equities only trade 9:30-16:00 ET. Cycles outside market hours will fetch stale prices, generate signals that can't fill, and burn API rate limits on yfinance/EDGAR. Consider adding a market-hours check that skips the cycle (or runs a reduced version) outside RTH.

10. Ladder-in state is lost on restart

LadderInEngine._triggered is in-memory only. If the bot restarts, it forgets which ladder levels have already been triggered, potentially re-adding shares at the same level. This should be persisted to SQLite alongside the portfolio state.

11. The momentum and relative_strength weights are zeroed out

The composite score currently uses
0.00
 — momentum and RS contribute nothing. This means the portfolio is purely overlap + holding weight + historical performance, with no forward-looking momentum signal. If you're going to keep these at zero, remove them to simplify. If you want to use them, even a small weight (5-10%) on momentum could help avoid catching falling knives — stocks that smart money bought but have since broken down.

Priority ranking
If I had to pick the top 3 changes that would most improve risk-adjusted returns:

Widen the trailing stop (or make it ATR-based) — the 5% trail is almost certainly causing premature exits on good positions
Add signal-driven exits — the system is blind to sell signals from the same sources it trusts for buys
Make ladder-in risk-adjusted — fixed share counts create wildly different dollar exposures across positions
