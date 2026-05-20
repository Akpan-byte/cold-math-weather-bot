# ❄️ Cold Math Weather Bot

**Prediction market arbitrage on weather — powered by NWS physics, not vibes.**

## The Thesis

NWS temperature forecasts have a known RMSE (~1.5°C for 24h forecasts). When a Polymarket weather market's threshold is 3.5°C+ away from the NWS forecast, we can compute a **≥99% confidence** that the market will resolve correctly. We buy the undervalued side and let physics do the rest.

## Architecture

```
Scanner → NWS API → Confidence Model → Trade Filter → Kelly Sizing → Executor
   ↓           ↓            ↓               ↓              ↓            ↓
Polymarket  Forecast    Gaussian CDF    7-gate filter   Quarter-Kelly   CLOB API
 Markets    + Margin    → P(correct)    (conf, price,   (25% of full    (py-clob-client)
            vs NWS                      edge, time,     Kelly for
                                        liquidity,      safety)
                                        drawdown)
```

## Results — Real Data Backtest

**161 verified Polymarket weather markets with known outcomes.**

| Confidence ≥ | Trades | Win Rate | P&L | Return | Max DD | Sharpe |
|---|---|---|---|---|---|---|
| 80% | 111 | 98.2% | $42.20 | 168.8% | 25.0% | 3.0 |
| 90% | 98 | 98.0% | $33.45 | 133.8% | 25.0% | 2.8 |
| 95% | 87 | 98.9% | $32.11 | 128.4% | 25.0% | 3.0 |
| **99%** | **63** | **98.4%** | **$17.12** | **68.5%** | **25.0%** | **2.5** |
| 99.5% | 54 | 98.2% | $12.48 | 49.9% | 25.0% | 2.2 |
| 99.9% | 36 | 97.2% | $4.64 | 18.6% | 25.0% | 1.2 |

**Monte Carlo Bootstrap (50 runs, 99% threshold):**
- Win Rate: 98.3% ± 1.2% (worst run: 97.1%)
- Return: 38.7% ± 20.9%
- Max Drawdown: 17.0% mean, 25.0% worst

**Only 1 false positive** in 63 trades at 99% confidence: a Denver 78°F market where the margin was 5.8°C but still exceeded the threshold — a 1-in-1000+ event (multiple SD outlier).

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run real-data backtest
python run.py backtest --archive

# Run synthetic backtest
python run.py backtest --scenarios 1000

# Run Monte Carlo
python run.py monte-carlo --runs 100

# Paper trading
python run.py paper --interval 300

# Live trading (requires confirmation)
python run.py live --confirm
```

## Project Structure

```
coldmath/
├── core/
│   ├── config.py          # All configuration (thresholds, limits, keys)
│   ├── engine.py          # Brain: filter pipeline + Kelly sizing
│   └── nws.py             # NWS API client + confidence scorer
├── backtest/
│   ├── engine.py          # Synthetic backtest engine
│   ├── real_data.py       # Real-data backtest (v1, uses live filter)
│   └── real_data_v2.py    # Real-data backtest (v2, direct computation)
├── paper/
│   └── trader.py          # Paper trading with SQLite state
├── live/
│   └── executor.py        # Live CLOB execution + safety guard
├── alerts/
│   └── telegram.py        # Telegram alerts + alert manager
├── dashboard/
│   └── server.py          # Real-time HTTP dashboard (port 8199)
├── data/                  # Backtest results, market archives
├── run.py                 # CLI entry point
└── README.md              # This file
```

## The 7-Gate Filter

Every market must pass ALL 7 gates before we trade:

1. **NWS Confidence** — ≥99% (Gaussian CDF from margin/RMSE)
2. **Entry Price** — Between 5¢ and 96¢ (avoid near-certain + illiquid)
3. **Edge** — NWS confidence - market implied ≥ 2% (enough mispricing)
4. **Resolution Time** — ≤72 hours (avoid distant, uncertain markets)
5. **Liquidity** — ≥$500 available (can actually fill the order)
6. **Daily Loss Limit** — ≤5% of bankroll per day (capital preservation)
7. **Consecutive Losses** — ≤3 in a row (pause after streak)

## Kelly Sizing

We use **quarter-Kelly** (25% of full Kelly) for safety:

```
f = (b*p - q) / b    # Full Kelly fraction
position = bankroll × f × 0.25    # Quarter-Kelly
```

Where `b` = net odds, `p` = NWS confidence, `q` = 1-p.

This means we bet ~2-6% of bankroll per trade, scaling down after losses.

## Safety Systems

- **Kill switch**: Global on/off — immediately cancels all orders
- **Daily loss limit**: 5% of bankroll → auto-pause for 24h
- **Consecutive loss pause**: 3 losses → pause for 6h
- **Max position**: 25% of bankroll in any single trade
- **Paper mode**: Full pipeline, no real execution — default for testing
- **Live confirmation**: Requires explicit `--confirm` flag

## Configuration

All settings in `core/config.py`:

```python
min_nws_confidence = 0.99      # 99% confidence required
min_entry_price = 0.05          # Don't buy below 5¢
max_entry_price = 0.96          # Don't buy above 96¢
min_edge_pct = 0.02             # 2% edge required
max_resolution_hours = 72       # Max time to resolution
min_liquidity_usd = 500         # Min liquidity to trade
daily_loss_limit_pct = 0.05     # 5% daily loss limit
max_consecutive_losses = 3      # Pause after 3 losses
kelly_fraction = 0.25           # Quarter-Kelly
starting_bankroll = 25.0        # Starting capital
scan_interval_seconds = 300     # 5-minute scan cycle
```

## Dashboard

**Live Dashboard:** [coldmath-dashboard.vercel.app](https://coldmath-dashboard.vercel.app)

Real-time HTTP dashboard:

- Bankroll + P&L tracking + equity curve
- Sensitivity analysis across confidence thresholds
- Monte Carlo bootstrap statistics
- NWS confidence distribution
- Full trade log (63 qualifying markets)
- Also available locally at `http://localhost:8199`

## Key Insight: Why This Works

The edge comes from **structural mispricing**: Polymarket weather markets are priced by crowd intuition, not Gaussian error propagation. When NWS says "high will be 45°F" with σ=2.7°F, and the market asks "Will it be 55°F+?", the crowd prices this at ~5¢ when the true probability is <0.1¢. We buy the NO side at 5¢ and it resolves to $1.00 — a 20x return on a near-certain outcome.

The 1 loss in 63 trades at 99% confidence is expected: at 99% confidence, we expect ~1% of trades to lose, and we observed 1.6% (1/63). This is within statistical expectations.

## License

MIT — Build your own weather arbitrage bot.

## Disclaimer

This is research software, not financial advice. Past performance (even on real data) does not guarantee future results. Weather markets can be thin, and NWS forecasts can be wrong in extreme events. Use at your own risk.
