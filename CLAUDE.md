# CLAUDE.md — AI Portfolio Manager

> Context file for Claude Code. Keep updated after every structural change.

---

## What This Project Is

Autonomous trading bot: Python collects market data, sends it to Claude Sonnet 4.6, Claude decides, Python executes blindly on Alpaca Markets. Python has **zero trading logic** — Claude is the only brain.

**Runs on:** GCP `e2-micro` VM, Rocky Linux 10.1, Python 3.12, systemd.

---

## Project Structure

```
/opt/portfolio_manager/
├── main.py                  # Core trading loop — data collection, Claude orchestration, order execution
├── dashboard.py             # Read-only Flask web dashboard (separate systemd service)
├── system_prompt.txt        # Claude's permanent identity + hard constraints (loaded fresh each cycle)
├── requirements.txt         # Python dependencies
├── install.sh               # One-time VM setup script (Rocky Linux)
├── portfolio_manager.service  # systemd unit for main.py
├── dashboard.service          # systemd unit for dashboard.py (gunicorn)
├── .env.template            # Template for environment variables
├── static/
│   ├── style.css            # Dashboard styles
│   └── script.js            # Dashboard frontend JS
├── templates/
│   └── index.html           # Dashboard HTML template
│
│ # VM-only files (never committed):
├── .env                     # API keys and config
├── decisions.log            # Claude's reasoning diary (rotates monthly, 14 days passed to Claude)
├── trades.log               # Permanent trade register (always passed to Claude in full)
└── app.log                  # Python operational log (rotates monthly, never passed to Claude)
```

---

## Architecture Principle

**"Python is a dumb courier."** Every `if` statement in Python is an architectural defect — it means trading logic leaked out of Claude.

Python's only conditional branches:
1. Skip closed sessions
2. Block oversized orders (`qty > MAX_ORDER_QTY`)
3. Block unknown actions (`action not in ALLOWED_ACTIONS`)
4. Handle NEED_MORE_CONTEXT retries (max 2)
5. Handle REQUEST_DATA two-phase cycle (fetch market data, call Claude again)
6. Daily kill switch (drawdown > 15%)

---

## Key Components in main.py

| Function | Line | Purpose |
|---|---|---|
| `get_session()` | ~80 | Returns REGULAR / PRE_MARKET / AFTER_HOURS / CLOSED |
| `load_system_prompt()` | ~103 | Reads system_prompt.txt fresh each cycle |
| `is_crypto(ticker)` | ~108 | Alpaca asset class lookup with cache |
| `check_kill_switch()` | ~122 | Daily drawdown guard (flash crash protection) |
| `get_portfolio_state()` | ~137 | Account + positions + last 20 filled orders from Alpaca |
| `get_market_briefing()` | ~189 | SPY/QQQ/IWM snapshots, most actives, news, Fear & Greed |
| `_format_ticker_block()` | ~296 | Format one ticker's OHLCV + quote as a string block |
| `fetch_market_data()` | ~324 | Phase 2 fetch: real-time OHLCV + quotes for requested tickers |
| `read_log()` | ~393 | Read log files filtered by date |
| `collect_context()` | ~413 | Assembles the 5-block briefing for Claude Phase 1 |
| `ask_claude()` | ~460 | Calls Claude API with retry (exponential backoff for 429/529/5xx) |
| `write_decision_log()` | ~575 | Appends reasoning to decisions.log |
| `write_trade_log()` | ~592 | Appends trade to trades.log |
| `notify_telegram()` | ~614 | Non-blocking Telegram notification (BUY/SELL only) |
| `execute_decision()` | ~648 | Safety checks + Alpaca order submission |
| `run_cycle()` | ~745 | One full cycle: session → Phase 1 → [Phase 2] → execute |
| `__main__` loop | ~848 | Infinite loop with sleep (1h regular, 6h extended) |

---

## Claude's Two-Phase Cycle

```
Phase 1: collect_context → ask_claude
  → if REQUEST_DATA:
      fetch_market_data(tickers) → append BLOCK 6
      ask_claude [Phase 2] → execute_decision
  → else (HOLD/BUY/SELL/etc.):
      execute_decision
```

## Claude's Context Blocks

| Block | Phase | Content | Source |
|---|---|---|---|
| 1 — System Prompt | 1 + 2 | Identity, philosophy, hard constraints | `system_prompt.txt` |
| 2 — Portfolio | 1 + 2 | Cash, equity, positions, last 20 orders | Alpaca API |
| 3 — Market | 1 + 2 | SPY/QQQ/IWM, movers, news, crypto F&G | Alpaca + alternative.me |
| 4A — Decisions Log | 1 + 2 | Claude's own past reasoning (last 14 days) | `decisions.log` |
| 4B — Trades Log | 1 + 2 | Full trade history (open to close) | `trades.log` |
| 5 — Cycle Instruction | 1 + 2 | Session type + validation checklist | Generated per cycle |
| 6 — Market Data | **2 only** | Real-time OHLCV + quotes for requested tickers | Alpaca data API |

---

## Claude's Decision JSON Schema

```json
{
  "action": "BUY | SELL | HOLD | ADJUST | NEED_MORE_CONTEXT | REQUEST_DATA",
  "ticker": "AAPL",
  "qty": 7,
  "order_type": "market | limit",
  "limit_price": 221.50,
  "stop_loss": 214.00,
  "take_profit": 238.00,
  "confidence": 0.72,
  "reasoning": "Italian text — max 800 chars — thesis for future self",
  "tickers": ["AMD", "SPY"],
  "need_more_context": { "from_date": "YYYY-MM-DD", "reason": "..." }
}
```

JSON extraction: fenced `json` block → fenced block → last `{...}` in response.
Truncation fallback: regex extracts `action`; safe actions (HOLD/REQUEST_DATA/NEED_MORE_CONTEXT) use partial parse; BUY/SELL with truncated JSON are forced to HOLD.

---

## Safety Layers

| Layer | What | Managed By |
|---|---|---|
| L1 | Self-validation (constraints checklist) | Claude |
| L2 | Physical checks: qty cap, action allowlist | Python |
| L3 | Margin calls, circuit breakers | Alpaca broker |
| L4 | Daily kill switch (drawdown > 15%) | Python |
| L5 | Telegram alerts on every trade | Human |

---

## Session Schedule

| Session | When (ET) | Frequency | Sleep After |
|---|---|---|---|
| PRE_MARKET | 08:00 | Once | 6h |
| REGULAR | 09:30–16:00 | Every hour | 1h |
| AFTER_HOURS | 17:00 | Once | 6h |
| CLOSED | All other times | Check every 60s | 60s |

~45–90 Claude calls/week (up to 2 calls/cycle if REQUEST_DATA used; 9 cycles/day × 5 days).

---

## Dashboard (dashboard.py)

Separate Flask app on port 8080, served by gunicorn. Completely independent from main.py — no shared memory or IPC. Uses a **read-only Alpaca key** (`ALPACA_DATA_KEY`).

All `/api/*` responses include `Cache-Control: no-cache, no-store` headers to prevent browser/proxy caching. Log endpoints use efficient tail reading (seek from end) instead of loading entire files into memory.

| Endpoint | Returns |
|---|---|
| `GET /` | HTML dashboard (index.html) |
| `GET /api/portfolio` | Account + positions from Alpaca |
| `GET /api/log` | Last 200 merged decisions.log + trades.log entries (tail read) |
| `GET /api/applog` | Last 200 lines of app.log, INFO+ only (tail read, DEBUG filtered out) |

Frontend (`script.js`) refreshes all data immediately on tab visibility change and shows stale-data indicators when fetches fail.

---

## Environment Variables (.env)

```
ALPACA_KEY, ALPACA_SECRET    # Trading API keys
ALPACA_PAPER                 # true = paper, false = LIVE
ANTHROPIC_KEY                # Claude API key
TELEGRAM_BOT_TOKEN           # Telegram notifications
TELEGRAM_CHAT_ID
MAX_ORDER_QTY                # Hard cap on order quantity (default 500)
KILL_SWITCH_DRAWDOWN         # Daily drawdown threshold (default 0.15)
PRE_MARKET_TIME              # ET time for pre-market cycle (default 08:00)
AFTER_HOURS_TIME             # ET time for after-hours cycle (default 17:00)
LOG_PROMPTS                  # true = log full Claude prompts to app.log at DEBUG
ALPACA_DATA_KEY              # Read-only key for dashboard
ALPACA_DATA_SECRET
DASHBOARD_PORT               # Default 8080
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `anthropic>=0.21.0` | Claude API client |
| `alpaca-py>=0.30.0` | Broker — portfolio, orders, market data, news |
| `python-dotenv>=1.0.1` | .env loading |
| `requests>=2.31.0` | HTTP calls (Fear & Greed, Alpaca screener, Telegram) |
| `pytz>=2024.1` | ET timezone with DST handling |
| `flask>=3.0.2` | Dashboard web server |
| `gunicorn>=21.2.0` | Dashboard production WSGI server |

---

## Deployment & Operations

```bash
# Deploy to VM
sudo bash install.sh https://github.com/your-username/portfolio-manager
nano /opt/portfolio_manager/.env
sudo systemctl start portfolio_manager
sudo systemctl start dashboard

# Update code on VM
cd /opt/portfolio_manager && sudo git pull
sudo systemctl restart portfolio_manager
sudo systemctl restart dashboard

# Live logs
sudo journalctl -u portfolio_manager -f
sudo journalctl -u dashboard -f
```

---

## Key Design Decisions

1. **System prompt from file** — strategy changes = edit `system_prompt.txt`, not code
2. **No database** — append-only flat files, grep-able, scp-able
3. **Paper equity = budget** — Alpaca tracks P&L correctly, no virtual cash counter
4. **Model pinned to `claude-sonnet-4-6`** — no surprise behavior changes from model updates
5. **Telegram non-blocking** — 5s timeout, exceptions swallowed to WARNING
6. **Two sleep durations** — simpler than a scheduler, clock drift irrelevant at hourly scale
7. **Retry with exponential backoff** — transient API errors (429, 529, 5xx) retried up to 4 times (30s → 60s → 120s → 240s) before giving up
8. **Two-phase cycle** — Phase 1 identifies candidates, Phase 2 gets real prices via BLOCK 6 before deciding; prevents stale training-memory prices from poisoning orders

---

## Common Bugs & Fixes Applied

| Fix | Issue | Resolution |
|---|---|---|
| #1 | decisions.log contained full prompt → exponential self-poisoning | Write reasoning only |
| #2 | Market briefing was a stub | Real Alpaca data (snapshots, movers, news) |
| #3 | `data_client` never instantiated | Added `StockHistoricalDataClient` init |
| #4 | News not filtered by portfolio tickers | Positions passed to `get_market_briefing()` |
| #5 | Context retry count off by one | `MAX_CONTEXT_RETRY = 2`, loop `<= 2` |
| #6 | Kill switch was monthly (unreliable without start-of-month equity) | Daily only; monthly delegated to Claude |
| #7 | Bracket orders (stop/TP) dropped on market orders | Supported for both limit and market BUY |
| #9 | Telegram SELL missing TP/SL/confidence | Same details for BUY and SELL |
| #10 | `get_session()` called twice per cycle | `run_cycle()` returns session |
| #12 | Crypto detection via `/` heuristic was fragile | Alpaca asset class lookup with cache |
| API retry | 529/429 errors caused full cycle skip | Exponential backoff (30s/60s/120s/240s, max 4 retries); explicit `OverloadedError` handler |
| #13 | Claude guessed stale training prices (AMD $121 vs actual $210) → unfilled orders | Two-phase cycle: REQUEST_DATA → fetch real OHLCV → Phase 2 decides with BLOCK 6 |
| #14 | Truncated JSON on long responses caused full cycle skip | Regex partial parse: safe actions recovered; BUY/SELL truncated → forced HOLD |

---

## Git Conventions

- Never commit: `.env`, `decisions.log`, `trades.log`, `app.log`, `crash.log`, `venv/`, `__pycache__/`
- Commit messages: short imperative description of what changed and why
- Single branch: `main`
