# AI Portfolio Manager

> Autonomous trading system powered by Claude Sonnet 4.6 + Alpaca Markets.  
> Python acts as a dumb courier. Claude does all the thinking.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
- [Project Structure](#project-structure)
- [Dashboard](#dashboard)
- [Logging](#logging)
- [Risk Management](#risk-management)
- [Deployment](#deployment)
- [Updating](#updating)
- [Paper Trading vs Live](#paper-trading-vs-live)
- [Known Limitations](#known-limitations)

---

## Overview

This system runs an hourly trading loop on a GCP VM. Each cycle, Python collects live market data from Alpaca and passes it to Claude as a structured briefing. Claude decides whether to buy, sell, hold, or adjust positions. Python executes the decision blindly — no interpretation, no additional logic.

**What this is:**
- A fully autonomous portfolio manager with configurable risk constraints
- Designed for US equities (long/short) and crypto (long only) via Alpaca
- Observable via a read-only web dashboard
- Notifies every trade via Telegram

**What this is not:**
- A backtesting framework
- A signal service
- A system with hardcoded trading logic

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Python (main.py)                  │
│  Collects data → Builds prompt → Executes decision  │
│  Zero trading logic. Zero interpretation.            │
└──────────┬──────────────────────────┬───────────────┘
           │                          │
           ▼                          ▼
  ┌────────────────┐        ┌──────────────────────┐
  │  Alpaca API    │        │  Claude Sonnet 4.6   │
  │  - Portfolio   │        │  - Market analysis   │
  │  - Orders      │        │  - Decision making   │
  │  - News feed   │        │  - Self-validation   │
  │  - Market data │        │  - Risk assessment   │
  └────────────────┘        └──────────────────────┘
```

**Core principle:** Python is a smart postman. It collects, assembles, delivers, and executes. Claude is the only brain. All trading decisions, position sizing, risk assessment, and strategy happen inside Claude's reasoning.

---

## Prerequisites

- GCP VM — `e2-micro` (free tier eligible), Rocky Linux 10.1
- [Alpaca Markets account](https://alpaca.markets) — paper or live
- [Anthropic API key](https://console.anthropic.com)
- Telegram bot token + chat ID (for trade notifications)
- Git repository (GitHub / GitLab)

---

## Installation

Run once on the VM as root. Provide your git repo URL as argument.

```bash
sudo bash install.sh https://github.com/your-username/portfolio-manager
```

The script will:
1. Install Python 3.12, git, cronie
2. Clone the repo into `/opt/portfolio_manager`
3. Create a Python virtualenv and install dependencies
4. Configure SELinux contexts
5. Register and enable `portfolio_manager.service` (systemd)
6. Create log files with correct permissions
7. Set up monthly log rotation via cron

After installation, populate the `.env` file:

```bash
nano /opt/portfolio_manager/.env
sudo systemctl start portfolio_manager
sudo systemctl start dashboard
```

---

## Configuration

All configuration lives in `/opt/portfolio_manager/.env` on the VM. **This file is never committed to git.**

```env
# BROKER — ALPACA
ALPACA_KEY=PKxxxxxxxxxxxxxxxxxxxxx
ALPACA_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ALPACA_PAPER=true          # true = paper trading | false = LIVE

# AI BRAIN
ANTHROPIC_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# TELEGRAM NOTIFICATIONS
TELEGRAM_BOT_TOKEN=xxxxxxxxxx:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=123456789

# OPERATIONAL PARAMETERS
MAX_ORDER_QTY=500          # hard physical cap on order quantity
KILL_SWITCH_DRAWDOWN=0.15  # auto-halt if monthly drawdown exceeds 15%
PRE_MARKET_TIME=08:00      # ET — single pre-market cycle
AFTER_HOURS_TIME=17:00     # ET — single after-hours cycle

# DASHBOARD
ALPACA_DATA_KEY=AKxxxxxxxxxxxxxxxxxxxxx   # read-only Alpaca key
DASHBOARD_PORT=8080
```

### Paper account calibration

To simulate with a specific budget (e.g. $6,000), set the initial equity of your Alpaca paper account to that amount when creating it. Alpaca tracks gains, losses, and open positions correctly — no code required.

---

## How It Works

### Session detection

Python checks the Alpaca clock (`/v1/clock`) every minute when the market is closed. When the market is open, it runs hourly cycles. Two additional cycles fire at fixed ET times using `pytz` (handles US daylight saving automatically regardless of VM timezone):

| Session | When (ET) | Frequency |
|---|---|---|
| PRE_MARKET | 08:00 | Once |
| REGULAR | 09:30 – 16:00 | Every hour |
| AFTER_HOURS | 17:00 | Once |
| CLOSED | All other times | No Claude calls |

### Each cycle

1. Fetch portfolio state from Alpaca (account, positions, filled orders)
2. Fetch market data (snapshots, top movers, news via `/v1beta1/news`)
3. Fetch Fear & Greed Index (crypto context, `api.alternative.me`)
4. Read `decisions.log` (last 14 days) and full `trades.log`
5. Assemble a structured 5-block briefing and send to Claude
6. Claude responds with a JSON decision
7. If Claude requests more context (`NEED_MORE_CONTEXT`), extend the log window and retry — max 2 retries, then force HOLD
8. Append reasoning to `decisions.log`; append trade events to `trades.log`
9. Apply physical safety checks (qty cap, action allowlist)
10. Execute order on Alpaca with bracket stop/take-profit
11. Send Telegram notification (BUY/SELL only)

### Claude's briefing structure

| Block | Content | Changes each cycle? |
|---|---|---|
| 1 | System prompt — identity, philosophy, hard constraints | Never |
| 2 | Live portfolio state from Alpaca | Yes |
| 3 | Market data — macro, movers, news, crypto | Yes |
| 4A | `decisions.log` — last 14 days of reasoning | Yes |
| 4B | `trades.log` — full trade history | Yes |
| 5 | Current cycle instruction + active session | Yes |

### Hard constraints (enforced by Claude, self-validated)

- Max 5 open positions at any time (stock + crypto combined)
- No leverage if confidence < 0.75
- Stop loss required on every open position
- No new positions 48h before CPI, Fed meetings, or earnings on held tickers
- Extended hours: limit orders only
- No day trades if `daytrade_count >= 3` and equity < $25,000

### Physical safety checks (enforced by Python)

- `qty > MAX_ORDER_QTY` → block and log
- `action not in ALLOWED_ACTIONS` → block and log
- Monthly drawdown > 15% → kill switch, halt all new orders

---

## Project Structure

```
/opt/portfolio_manager/
├── main.py                      # Portfolio manager (all trading logic)
├── dashboard.py                 # Read-only web dashboard
├── system_prompt.txt            # Claude's permanent identity
├── requirements.txt
├── portfolio_manager.service    # systemd unit
├── dashboard.service            # systemd unit
├── install.sh                   # One-time VM setup script
│
├── .env                         # VM only — never in git
├── decisions.log                # VM only — Claude's market diary (rotates monthly)
├── trades.log                   # VM only — permanent trade register
└── app.log                      # VM only — Python application log (rotates monthly)
```

**Never committed to git:** `.env`, `decisions.log`, `trades.log`, `app.log`, `venv/`

---

## Dashboard

A lightweight read-only Flask/FastAPI web app on port `8080`. Runs as a separate systemd service — if it crashes, trading continues uninterrupted.

**Sections:**
- **HOME** — 6 KPI cards (equity, cash, P&L day/total, open positions), positions table, order history
- **LOG** — unified timeline merging `decisions.log` + `trades.log`, filterable by action and source
- **APP LOG** — live stream of `app.log` with INFO/WARNING/ERROR badges, 30s auto-refresh

Access: `http://VM-IP:8080`  
Restrict port 8080 in GCP firewall to your static IP only.

---

## Logging

Three separate log files with distinct lifecycles:

| File | Content | Rotation | Passed to Claude |
|---|---|---|---|
| `decisions.log` | Claude's reasoning every cycle | Monthly | Yes — last 14 days |
| `trades.log` | Every trade from open to close | Never | Yes — always full |
| `app.log` | Python activity (sessions, API calls, orders, errors) | Monthly | Never |

`app.log` uses Python's stdlib `logging` module with two handlers: file (for dashboard) and stdout (captured by journald).

```
2026-02-21 09:30:01 | INFO     | === Start cycle | session: REGULAR ===
2026-02-21 09:30:07 | INFO     | Claude response: action=HOLD | tokens=5102+198
2026-02-21 14:30:01 | WARNING  | ORDER BLOCKED — qty 750 exceeds MAX_ORDER_QTY 500
2026-02-21 17:00:05 | INFO     | === End cycle | next in 6h ===
```

---

## Risk Management

Five protection layers:

| Level | Type | Managed by |
|---|---|---|
| L1 — Soft | System prompt constraints | Claude (self-validates) |
| L2 — Hard | Python physical checks (qty, action allowlist) | Python |
| L3 — Broker | Margin calls, circuit breakers | Alpaca |
| L4 — Kill Switch | Monthly drawdown > 15% halts all new orders | Python |
| L5 — Human | Telegram alerts on every trade | You |

---

## Deployment

### Initial setup

```bash
# Local
git init
git add .gitignore main.py dashboard.py system_prompt.txt \
        requirements.txt *.service install.sh
git commit -m "Initial commit"
git push origin main

# On VM
sudo bash install.sh https://github.com/your-username/portfolio-manager
nano /opt/portfolio_manager/.env   # fill in all keys
sudo systemctl start portfolio_manager
sudo systemctl start dashboard
```

### Useful commands

```bash
# Status
sudo systemctl status portfolio_manager
sudo systemctl status dashboard

# Live logs
sudo journalctl -u portfolio_manager -f
sudo journalctl -u dashboard -f

# Test dashboard endpoint
curl http://localhost:8080/api/portfolio
```

---

## Updating

```bash
# Local — commit and push changes
git add . && git commit -m "your message" && git push

# On VM
cd /opt/portfolio_manager
sudo  git pull
sudo systemctl restart portfolio_manager
sudo systemctl restart dashboard   # only if dashboard.py changed
```

To update only the system prompt (no restart needed if using file reload):
```bash
nano /opt/portfolio_manager/system_prompt.txt
sudo systemctl reload-or-restart portfolio_manager
```

---

## Paper Trading vs Live

The only difference between paper and live is one line in `.env`:

```env
ALPACA_PAPER=false   # was: true
```

Then restart:
```bash
sudo systemctl restart portfolio_manager
```

Python code does not change. Claude does not know the difference. The service restarts in under 5 seconds.

---

## Known Limitations

- **No backtesting** — the system operates forward only; historical performance cannot be simulated without separate tooling
- **US market hours** — designed around NYSE/NASDAQ sessions; non-US equities are not supported by Alpaca
- **Single-instance** — no concurrency handling; running two instances against the same Alpaca account will produce undefined behavior
- **Claude context window** — very long `trades.log` files (years of trading) may eventually approach token limits; monitor usage over time
- **Extended hours liquidity** — pre-market and after-hours sessions have wider spreads and lower liquidity; Claude is instructed to use limit orders and conservative sizing, but execution risk remains

---

## Dependencies

| Package | Purpose |
|---|---|
| `anthropic` | Claude API client |
| `alpaca-trade-api` | Broker — portfolio, orders, market data, news |
| `python-dotenv` | `.env` file loading |
| `requests` | Fear & Greed Index API call |
| `pytz` | ET timezone with automatic US daylight saving handling |
| `flask` or `fastapi` | Dashboard web server |

All stdlib — `logging`, `json`, `time`, `os`, `datetime` — zero additional dependencies.
