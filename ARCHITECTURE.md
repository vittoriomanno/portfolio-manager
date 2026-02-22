# Architecture

This document describes the technical design of the AI Portfolio Manager — the decisions made, the reasoning behind them, and the tradeoffs accepted. Read the [README](README.md) first for operational context.

---

## Table of Contents

- [Design Philosophy](#design-philosophy)
- [System Overview](#system-overview)
- [Component Map](#component-map)
- [The Trading Loop](#the-trading-loop)
- [Session Management](#session-management)
- [Claude's Briefing — Prompt Structure](#claudes-briefing--prompt-structure)
- [Decision Protocol](#decision-protocol)
- [Memory — The Three Log Files](#memory--the-three-log-files)
- [Context Request Mechanism](#context-request-mechanism)
- [Telegram Notifications](#telegram-notifications)
- [Safety Architecture](#safety-architecture)
- [Dashboard Architecture](#dashboard-architecture)
- [Data Flow Diagram](#data-flow-diagram)
- [Key Design Decisions](#key-design-decisions)
- [What Deliberately Doesn't Exist](#what-deliberately-doesnt-exist)

---

## Design Philosophy

The entire system rests on a single architectural principle:

> **Python is a dumb courier. Claude is the only brain.**

Python collects data, assembles a briefing, delivers it to Claude, and executes whatever Claude decides. Python contains no trading logic, no market interpretation, no signal generation. It has exactly four conditional branches: skip closed sessions, block oversized orders, block unknown actions, and handle context retries.

Every if-statement added to Python is an architectural defect. It means trading logic has leaked out of Claude and into code that can't reason about markets.

This separation has concrete benefits:

- **Auditability** — every decision lives in `decisions.log` as Claude's own reasoning in natural language
- **Adaptability** — changing strategy means editing `system_prompt.txt`, not refactoring code
- **Debuggability** — if Claude makes a bad decision, you read the log and understand exactly why
- **Testability** — you can replay any scenario by replaying the briefing without running live infrastructure

---

## System Overview

```
┌────────────────────────────────────────────────────────────────┐
│                         GCP e2-micro VM                        │
│                                                                │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │                     main.py                             │  │
│  │                                                         │  │
│  │  get_session() → collect_context() → ask_claude()      │  │
│  │       │               │                   │            │  │
│  │       │               │                   ▼            │  │
│  │       │               │         ┌──────────────────┐   │  │
│  │       │               │         │  Claude Sonnet   │   │  │
│  │       │               │         │  4.6 (Anthropic) │   │  │
│  │       │               │         └────────┬─────────┘   │  │
│  │       │               │                  │ JSON        │  │
│  │       ▼               ▼                  ▼            │  │
│  │  ┌─────────┐   ┌────────────┐   execute() + notify() │  │
│  │  │  pytz   │   │   Alpaca   │          │              │  │
│  │  │  clock  │   │    API     │          ▼              │  │
│  │  └─────────┘   └────────────┘   ┌──────────────┐     │  │
│  │                      │          │  Alpaca API  │     │  │
│  │                      │          │  (orders)    │     │  │
│  │                      ▼          └──────────────┘     │  │
│  │               ┌────────────┐                         │  │
│  │               │  app.log   │                         │  │
│  │               │decisions.lg│                         │  │
│  │               │ trades.log │                         │  │
│  │               └────────────┘                         │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                                │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │               dashboard.py (port 8080)                  │  │
│  │   Reads logs + Alpaca read-only key — no write access   │  │
│  └─────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
                              │
                  ┌───────────┴────────────┐
                  ▼                        ▼
           Telegram Bot             Browser (you)
           (trade alerts)          (dashboard)
```

---

## Component Map

| Component | File | Role |
|---|---|---|
| Portfolio Manager | `main.py` | Core loop — data collection, Claude orchestration, order execution |
| Dashboard | `dashboard.py` | Read-only web interface — separate process, separate systemd service |
| System Prompt | `system_prompt.txt` | Claude's permanent identity and hard constraints — loaded fresh each cycle |
| Market diary | `decisions.log` | Claude's reasoning log — rotates monthly, passed to Claude (last 14 days) |
| Trade register | `trades.log` | Permanent trade history — never rotates, always passed to Claude in full |
| App log | `app.log` | Python operational log — rotates monthly, never passed to Claude |

---

## The Trading Loop

```
while True:
    session = get_session()          # Step 1 — what session are we in?

    if session == 'CLOSED':
        sleep(60)                    # Step 2 — check again in 1 minute
        continue

    context = collect_context()      # Steps 3–7 — assemble briefing
    decision = ask_claude(context)   # Step 8 — Claude decides

    # Steps 9–11 — context retry logic (see below)

    log_decision(decision)           # Step 12 — write decisions.log
    if BUY/SELL: log_trade(decision) # Step 13 — write trades.log
    execute(decision)                # Steps 14–15 — safety checks + order
    if BUY/SELL: notify_telegram()   # Step 16 — Telegram alert

    sleep(3600 if REGULAR else 6h)   # Step 17 — wait for next cycle
```

The loop has no break condition. It runs forever under systemd supervision with automatic restart on failure.

---

## Session Management

Market timing uses two independent signals:

1. **`broker.get_clock()`** — Alpaca's clock endpoint knows about NYSE holidays, early closures, and schedule changes. If it says the market is open, it is. No hardcoded calendars.

2. **`datetime.now(ET)`** via `pytz` — for pre-market and after-hours cycles, Python compares the current ET time against two `.env` values. `pytz` handles US daylight saving automatically regardless of VM timezone (Rome, UTC+1/+2).

```python
ET = pytz.timezone('America/New_York')

def get_session():
    clock  = broker.get_clock()
    now_et = datetime.now(ET).strftime('%H:%M')
    if clock.is_open:           return 'REGULAR'
    elif now_et == PRE_MARKET:  return 'PRE_MARKET'
    elif now_et == AFTER_HOURS: return 'AFTER_HOURS'
    else:                       return 'CLOSED'
```

**Why separate pre/after-hours cycles instead of continuous extended hours monitoring?**

Pre-market and after-hours price movements are driven by discrete events (overnight news, earnings releases) rather than continuous price action. One cycle at 08:00 ET captures everything relevant before the open. One cycle at 17:00 ET processes the day's earnings and sets up overnight positions. Continuous monitoring would add ~120 Claude calls per week for marginal informational gain.

Total Claude calls: ~9/day × 5 days = **~45/week** (vs ~168 with 24/7 continuous loop).

---

## Claude's Briefing — Prompt Structure

Each cycle, Python assembles a structured 5-block prompt:

### Block 1 — System Prompt (static, loaded from file)

Claude's permanent identity. Defines philosophy, capabilities, and hard constraints. Never changes at runtime — only via `git pull` + service restart.

```
PHILOSOPHY:
  1. Capital preservation first
  2. Alpha generation is secondary
  3. When in doubt, HOLD is always correct
  4. Every decision must be explainable in plain language

HARD CONSTRAINTS:
  - Max 5 open positions (stock + crypto combined)
  - Max 10% equity per single position
  - No leverage if confidence < 0.75
  - Stop loss required on every open position
  - No day trades if daytrade_count >= 3 and equity < $25,000
  - No action 48h before CPI, Fed meetings, or held ticker earnings
  - Extended hours: limit orders only
```

### Block 2 — Live Portfolio State (Alpaca, every cycle)

```
Cash available: $X
Portfolio equity: $X
Buying power: $X
Day trade count: N

OPEN POSITIONS:
  NVDA | Qty: 5 | Entry: $820.00 | Current: $847.50
       | P&L: +3.35% (+$137.50)
       | Stop: $795.00 | TP: $900.00 | Days held: 4
```

### Block 3 — Market Briefing (Alpaca + alternative.me, every cycle)

- SPY / QQQ / VIX snapshots (macro proxy)
- Top movers and most active names
- News feed — first filtered by open position tickers, then general market (Alpaca `/v1beta1/news`, last 24h)
- BTC / ETH bars
- Fear & Greed Index (crypto sentiment)

**Why Alpaca for news instead of a third-party provider?**  
Zero additional cost — included in the Alpaca account. News is already filtered by ticker relevance. No separate API key to manage.

### Block 4A — Market Diary (decisions.log, last 14 days)

Claude's own previous reasoning — every HOLD with its rationale, every macro observation. This gives Claude continuity across cycles without holding state in memory.

### Block 4B — Trade Register (trades.log, full history)

Every trade from open to close. When Claude evaluates an existing position, it can read the original thesis, intermediate monitoring notes, and any prior adjustments. This prevents Claude from abandoning a position because it "forgot" why it was opened.

### Block 5 — Cycle Instruction (current cycle)

```
ACTIVE SESSION: REGULAR

Phase 1 — HUNTING: identify themes, sectors, candidates
Phase 2 — ANALYSIS: evaluate candidates + open positions
Phase 3 — VALIDATION:
  [ ] Positions after this order <= 5
  [ ] Position size <= 10% equity
  [ ] Stop loss present
  [ ] Extended hours: limit orders only
  [ ] daytrade_count respected
  [ ] Confidence >= 0.65
```

---

## Decision Protocol

Claude responds with a single JSON object:

```json
{
  "action": "BUY",
  "ticker": "AAPL",
  "qty": 7,
  "order_type": "limit",
  "limit_price": 221.50,
  "stop_loss": 214.00,
  "take_profit": 238.00,
  "confidence": 0.72,
  "reasoning": "Apple breaking out of 3-week consolidation on above-average volume..."
}
```

**Allowed actions:** `BUY` | `SELL` | `HOLD` | `ADJUST` | `NEED_MORE_CONTEXT`

Python's response to each:

| Action | Python does |
|---|---|
| `BUY` | Submit order + bracket (stop + TP). Log to both files. Telegram. |
| `SELL` | Submit order. Log to both files. Telegram. |
| `ADJUST` | Submit modification (e.g. trail stop). Log to decisions.log only. |
| `HOLD` | Log reasoning to decisions.log. Nothing else. |
| `NEED_MORE_CONTEXT` | Extend log window, retry (max 2). Then force HOLD. |

---

## Memory — The Three Log Files

The system has no database. All persistence is append-only flat files.

### Why separate decisions.log and trades.log?

They have fundamentally different lifecycles:

- **Market context ages.** What Claude thought about macro conditions 45 days ago is irrelevant today. `decisions.log` rotates monthly — passing only 14 days of it keeps the prompt lean without losing recent context.

- **Trade memory never ages.** A position opened 6 weeks ago needs its original thesis to be evaluated correctly today. If Claude can't read why NVDA was bought, it might sell on a dip that was anticipated in the original analysis. `trades.log` is permanent and always passed in full.

```
decisions.log                          trades.log
─────────────────────────────          ─────────────────────────────
2026-02-21 09:30 | HOLD                2026-01-15 | BUY NVDA
  SPY down 0.8%, VIX elevated.           Thesis: AI capex supercycle...
  Waiting for clearer direction.         Stop: $795 | TP: $900
                                         Confidence: 0.74
2026-02-21 10:30 | BUY AAPL
  Breaking out of consolidation...     2026-01-15 | MONITOR NVDA
                                         Up 2.1%. Thesis intact.
2026-02-21 11:30 | HOLD                  Trailing stop to $810.
  Holding AAPL. No new setups.
                                       2026-02-08 | SELL NVDA
                                         +12.3% ($247). Thesis reached.
                                         TP triggered at $900.
```

### app.log — operational transparency

`app.log` uses Python's stdlib `logging` with two handlers simultaneously:
- `FileHandler` → `/opt/portfolio_manager/app.log` (dashboard reads this)
- `StreamHandler` → stdout (journald captures this via systemd)

Level conventions: `INFO` for normal flow, `WARNING` for recoverable anomalies (blocked orders, failed Telegram), `ERROR` for unhandled exceptions.

---

## Context Request Mechanism

Sometimes Claude needs more history than the default 14 days to reason about an open position. Instead of always passing 60 days (bloating every prompt), Claude can request more:

```json
{
  "action": "NEED_MORE_CONTEXT",
  "need_more_context": {
    "from_date": "2025-12-01",
    "reason": "Position opened in December — need original thesis"
  }
}
```

Python re-reads `decisions.log` from the requested date and retries. Max 2 retries per cycle. If the third attempt would require more context, Python forces `HOLD` — the conservative choice when information is genuinely insufficient.

```
Cycle N:  collect(14 days) → Claude: NEED_MORE_CONTEXT from 2025-12-01
Retry 1:  collect(from Dec 1) → Claude: BUY / SELL / HOLD / ...  ✓
                                                    OR
Retry 2:  collect(from Dec 1) → Claude: NEED_MORE_CONTEXT again
Retry 3:  → Python forces HOLD — no third Claude call made
```

The decision of "how much history do I need" belongs to Claude, not to Python. Python passes a reasonable default; Claude escalates if necessary.

---

## Telegram Notifications

Sent only on `BUY` and `SELL` — not on HOLD or ADJUST. Expected volume: ~10 notifications/week.

```python
def notify_telegram(decision):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return
    # build message with emoji, ticker, reasoning, TP/SL/confidence
    try:
        requests.post(..., timeout=5)
    except Exception as e:
        log.warning(f'Telegram failed (non-blocking): {e}')
```

The `try/except` with `timeout=5` is intentional — Telegram availability must never block the trading loop. A failed notification is logged as WARNING and the cycle continues.

Message format:
```
🟢 BUY AAPL
━━━━━━━━━━━━━━━━━━━━━━
Reasoning: Apple breaking out of 3-week consolidation...

Expected: TP $238.00 | SL $214.00 | Confidence 0.72
```

---

## Safety Architecture

Five independent protection layers. Failing one does not bypass the others.

```
Layer 1 — Claude self-validation
  Claude checks its own output against hard constraints before emitting JSON.
  If a proposed trade violates a constraint, Claude emits HOLD instead.

Layer 2 — Python physical checks
  After Claude responds, Python applies two hard filters:
    · qty > MAX_ORDER_QTY → block regardless of Claude's reasoning
    · action not in ALLOWED_ACTIONS → block regardless of Claude's reasoning
  These cannot be overridden by the prompt.

Layer 3 — Alpaca broker guardrails
  Margin calls, pattern day trader enforcement, circuit breakers.
  Operates entirely outside this codebase.

Layer 4 — Kill switch (Python)
  If monthly portfolio drawdown exceeds KILL_SWITCH_DRAWDOWN (default 15%),
  Python halts all new orders. Claude continues to analyze and log,
  but execute() returns immediately without sending any order.
  Reactivates automatically at start of next calendar month.

Layer 5 — Human oversight
  Every BUY and SELL triggers a Telegram notification.
  You have full visibility into every trade as it happens.
  You can halt the system at any time: sudo systemctl stop portfolio_manager
```

**Why is the kill switch a percentage, not a dollar amount?**  
A percentage scales correctly regardless of account size. 15% on a $6,000 paper account ($900) and 15% on a $50,000 live account ($7,500) represent the same level of risk relative to capital.

---

## Dashboard Architecture

The dashboard is a completely independent process — separate Python file, separate systemd service, separate port. It has no shared memory or IPC with `main.py`. Communication happens exclusively through the filesystem (log files) and the Alpaca read-only API.

```
dashboard.py
  │
  ├── GET /api/portfolio
  │     └── Alpaca REST (ALPACA_DATA_KEY — read only, never trading key)
  │
  ├── GET /api/log
  │     ├── Read decisions.log
  │     ├── Read trades.log
  │     └── Merge by timestamp → unified timeline
  │
  └── GET /api/applog
        └── Read app.log → last N lines, filterable by level
```

**Why a separate read-only Alpaca key?**  
The dashboard needs market data but must never be able to submit orders — not by accident, not through a bug, not through a compromised dashboard process. Using a data-only key enforces this at the API credential level, independent of any application logic.

**Crash isolation:**  
If the dashboard crashes, `portfolio_manager.service` continues trading without interruption. If the portfolio manager crashes, the dashboard still serves the last known state from log files. Systemd restarts both independently.

---

## Data Flow Diagram

```
                        ┌───────────────┐
                        │  Alpaca API   │
                        │  /v2/account  │
                        │  /v2/positions│
                        │  /v2/orders   │
                        │  /v2/snapshots│
                        │  /v2/actives  │
                        │  /v1beta1/news│
                        └──────┬────────┘
                               │ market data
                               ▼
┌──────────┐          ┌────────────────┐
│decisions │─────────▶│                │
│  .log    │ 14 days  │  collect_      │
│(14+ days)│          │  context()     │
└──────────┘          │                │
                      │  builds 5-     │         ┌───────────────┐
┌──────────┐          │  block prompt  │────────▶│ Claude Sonnet │
│ trades   │─────────▶│                │         │    4.6        │
│  .log    │  full    └────────────────┘         └───────┬───────┘
│(all time)│                                             │ JSON
└──────────┘          ┌──────────────────────────────────▼───┐
                      │           Python                      │
┌──────────┐          │                                       │
│alternative│────────▶│  · log_decision() → decisions.log    │
│   .me    │  F&G     │  · log_trade()    → trades.log       │
│  F&G API │          │  · execute()      → Alpaca orders    │
└──────────┘          │  · notify()       → Telegram         │
                      │  · log.*()        → app.log          │
                      └───────────────────────────────────────┘
```

---

## Key Design Decisions

### 1. System prompt loaded from file, not hardcoded

`system_prompt.txt` is read fresh at the start of every Claude call via `load_system_prompt()`. This means updating Claude's identity, constraints, or strategy requires only a file edit and service reload — no code deployment.

### 2. No ORM, no database

All persistence is append-only flat files. This eliminates an entire class of operational complexity (schema migrations, connection pooling, backup procedures). Log files are grep-able, human-readable, and trivially backed up with `scp`.

### 3. Paper account equity as the budget constraint

Rather than maintaining a virtual cash counter in code (which desynchronizes on position closes and P&L changes), the Alpaca paper account itself serves as the ground truth for available capital. Setting initial equity to $6,000 at account creation gives a correctly maintained budget that accounts for gains, losses, and partial fills without any application logic.

### 4. Claude's model pinned to a specific version

`claude-sonnet-4-6` is hardcoded, not resolved via an alias like `claude-sonnet-latest`. This ensures behavior consistency — a model update won't silently change trading decisions. Model upgrades are deliberate, tested, and committed.

### 5. Telegram failure is non-blocking by design

The notification system uses `timeout=5` and swallows all exceptions into a WARNING log. The trading system must not depend on a messaging service for its core loop. Notifications are observability tooling, not part of the execution path.

### 6. Two sleep durations, not a scheduler

Rather than a cron-style scheduler with precise timing, the main loop uses `sleep(3600)` for regular hours and `sleep(21600)` for extended sessions. This is simpler, more resilient to clock drift, and requires no scheduler dependency. The slight imprecision in cycle timing (a few seconds) is irrelevant for hourly decision cycles.

---

## What Deliberately Doesn't Exist

These are not oversights — they are intentional architectural choices.

| Missing component | Why it's missing |
|---|---|
| Technical indicators (RSI, MACD, Bollinger) | Claude reasons from price data and news context. Pre-computed indicators would bias Claude's analysis and introduce a second opinion that can't explain itself. |
| Backtesting engine | The system is designed for live learning, not historical optimization. Backtesting would require a separate data pipeline and risks overfitting. |
| Position sizing formula | Claude decides size based on confidence, volatility context, and available capital. A formula would encode assumptions that may not hold across all market conditions. |
| WebSocket real-time feeds | Hourly cycles do not require sub-second data. REST polling is simpler, cheaper, and sufficient for the decision frequency. |
| Multi-strategy support | One strategy, one Claude identity, one system prompt. Complexity would make decisions harder to audit and debug. |
| Watchlist persistence | Claude builds its own watchlist each cycle from market data and news. A persisted watchlist would anchor Claude to past ideas rather than current opportunities. |
