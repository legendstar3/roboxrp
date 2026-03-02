# ROBOXRP PRO v13.2 — OKX Spot Trading System

Multi-timeframe EMA/RSI strategy with trailing stops, risk management, and live dashboard.

## Quick Start

### Windows (.bat)

```cmd
cd C:\Users\User\Desktop\ROBOXRP
start_bot.bat
```

### Windows (PowerShell)

```powershell
cd C:\Users\User\Desktop\ROBOXRP
.\start_bot.ps1
```

The system will install dependencies, connect to OKX, and open a live dashboard at **http://localhost:5001**.

## How It Works

1. **Signal Generation** — Scans all EUR spot pairs on OKX every 15–60s using 2 timeframes (5m entry + 1h trend). 6 safety layers filter noise.
2. **Entry Filters** — Wilder's RSI on 5m (with 1h fallback), EMA trend on 1h, EMA confirmation on 5m, ATR volatility regime, volume/liquidity guards, pattern recognition, MACD/BB/RSI divergence.
3. **Patient Order Execution** — Limit orders at mid-price, monitored and amended if price drifts. No market order slippage. Configurable timeout and amend threshold.
4. **Risk Management** — Spread guard, stale-price guard, SL cooldown, daily stop-loss, R:R filter (1.8:1 min), max trade duration (4h), max drawdown protection (20%), consecutive loss streak pause.
5. **Exit Management** — ATR-based TP1-TP4 and SL on exchange triggers. Trailing stop ladder: TP1 hit locks 30-50% profit (ATR-based, fee-aware), TP2 moves SL to TP1 and upgrades trigger to TP3, TP3 moves SL to TP2 and upgrades to TP4. Dead trigger auto-recovery.
6. **Safety Guards** — Fee-aware PnL (round-trip fees deducted), portfolio heat cap, duplicate signal prevention, compound position sizing with min/max bounds.
7. **Startup Recovery** — Cancels orphaned trigger orders and sells leftover tokens from crashed sessions automatically.
8. **Graceful Shutdown** — Closes active positions on Ctrl+C, cancels pending orders, exports trade journal CSV.
9. **Telegram Alerts** — Entry signals with R:R and confidence, exit notifications with PnL, ROI%, and duration.

## Project Structure

```text
ROBOXRP/
  .env                       Configuration (API keys, params)
  start_bot.bat              Launcher (Windows CMD)
  start_bot.ps1              Launcher (PowerShell)
  requirements.txt           Python dependencies
  README.md                  This file
  TradingBuddy/TradingBuddy/
    unified_trading_system_WORKING.py   Main system (single file)
    requirements.txt                    Dependencies (same as root)
    signal_id_state.json                Signal ID counter (auto-managed)
```

## Configuration

All settings are in `.env` at the project root. Key sections:

- **OKX API** — `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE`, `OKX_API_DOMAIN`
- **Telegram** — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ENABLED`
- **Trading Mode** — `OKX_ENABLE_LIVE_PRICES`, `OKX_ENABLE_TRADING`, `OKX_TRADING_MODE`
- **Position Sizing** — `OKX_POSITION_USD`, `MAX_CONCURRENT_TRADES`, warmup settings
- **Compound Growth** — `OKX_COMPOUND_POSITION_PCT` (% of balance per trade), `OKX_COMPOUND_MIN_USD`, `OKX_COMPOUND_MAX_USD`
- **Order Execution** — `OKX_STRICT_EXECUTION`, `OKX_LIMIT_OFFSET_PCT`, timeout, fallback
- **Safety Guards** — `OKX_MAX_SPREAD_PCT`, `OKX_MAX_ENTRY_DRIFT_PCT`, `DAILY_STOP_USD`, `OKX_SL_COOLDOWN_SEC`, `OKX_TAKER_FEE_PCT`, `MAX_CONSECUTIVE_LOSSES`, `MAX_PORTFOLIO_HEAT_USD`
- **Signal Generation** — `MIN_SIGNAL_CONFIDENCE`, `OKX_MIN_RR_RATIO`, RSI/EMA params
- **Universe** — `OKX_QUOTE_CCY`, `OKX_SYMBOLS_PER_CYCLE`, `OKX_ONLY_SYMBOLS`
- **Diagnostics** — Skip reason distribution per cycle, cycle timing stats, adaptive scan intervals

## Dependencies

```text
ccxt, flask, flask-cors, numpy, requests, python-dotenv, colorama
```

Install: `pip install -r requirements.txt`

## Dashboard

Live at **http://localhost:5001** (configurable via `DASHBOARD_PORT`).

- Live candlestick charts (1m–3h) via LightweightCharts
- **Equity curve** — real-time cumulative PnL chart
- **Performance panel** — avg win/loss, best/worst trade, profit factor, expectancy, max drawdown, loss streak, Sharpe ratio
- **Session bar** — uptime, balance, W/L counter, Sharpe ratio, avg trade duration, trading mode
- **Connection badge** — green pulse (connected), amber (stalled), red (offline)
- Active trades with fee-aware PnL color coding, progress bars to next TP, time in trade
- Executed signals with R:R ratio, confidence pills (green/amber/red) and mode tags
- Closed trade history with WIN/LOSS tags, pip count, and trade duration
- Empty state messages when no data available
- Health panel: OKX connectivity, balance, scan progress, heat, fees, cycle watchdog
- Dark scrollbar styling, informative footer

## Risk Warning

Trading involves substantial risk of loss. Never invest money you cannot afford to lose. This system provides automated signals — you are responsible for all trading decisions.

## Security

- Never share `.env` or commit it to version control
- Use IP-restricted API keys on OKX
- Monitor your OKX account activity regularly
