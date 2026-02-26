import os
import re
import time
import json
import logging
import traceback
import requests
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

# Alpaca
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, GetOrdersRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import (
    StockSnapshotRequest, StockLatestQuoteRequest, StockBarsRequest,
    CryptoLatestQuoteRequest, CryptoBarsRequest,
)
from alpaca.data.timeframe import TimeFrame

# Anthropic
from anthropic import Anthropic, APIStatusError
try:
    from anthropic import OverloadedError
except ImportError:
    # Older SDK versions don't export OverloadedError at the top level.
    # 529s will still be caught by the APIStatusError handler below (status_code 529).
    class OverloadedError(APIStatusError):  # type: ignore[misc]
        pass

# ── Logging ───────────────────────────────────────────────────────────────────
# Console → INFO only (clean output)
# app.log  → DEBUG (includes full prompts sent to Claude when LOG_PROMPTS=true)
log = logging.getLogger('portfolio_manager')
log.setLevel(logging.DEBUG)
_fmt = logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(_fmt)
log.addHandler(_ch)

_fh = logging.FileHandler('app.log', encoding='utf-8')
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_fmt)
log.addHandler(_fh)

# ── Environment ───────────────────────────────────────────────────────────────
load_dotenv()

ALPACA_KEY           = os.getenv('ALPACA_KEY')
ALPACA_SECRET        = os.getenv('ALPACA_SECRET')
ALPACA_PAPER         = os.getenv('ALPACA_PAPER', 'true').lower() == 'true'
ANTHROPIC_KEY        = os.getenv('ANTHROPIC_KEY')
TELEGRAM_BOT_TOKEN   = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID     = os.getenv('TELEGRAM_CHAT_ID')
MAX_ORDER_QTY        = int(os.getenv('MAX_ORDER_QTY', 500))
KILL_SWITCH_DRAWDOWN = float(os.getenv('KILL_SWITCH_DRAWDOWN', 0.15))
PRE_MARKET_TIME      = os.getenv('PRE_MARKET_TIME', '08:00')
AFTER_HOURS_TIME     = os.getenv('AFTER_HOURS_TIME', '17:00')
# Set LOG_PROMPTS=false in .env to suppress full prompt from app.log
LOG_PROMPTS          = os.getenv('LOG_PROMPTS', 'true').lower() == 'true'

MAX_CONTEXT_RETRY = 2
DEFAULT_LOG_DAYS  = 14
ET                = pytz.timezone('America/New_York')

# ── Init ──────────────────────────────────────────────────────────────────────
log.info("=== AI Portfolio Manager starting ===")
if not (ALPACA_KEY and ALPACA_SECRET and ANTHROPIC_KEY):
    log.error("Missing critical env vars: ALPACA_KEY, ALPACA_SECRET, ANTHROPIC_KEY")
    exit(1)

trading_client   = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)
data_client      = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)  # FIX #3: was never instantiated
anthropic_client = Anthropic(api_key=ANTHROPIC_KEY)

ALLOWED_ACTIONS = {'BUY', 'SELL', 'HOLD', 'ADJUST', 'NEED_MORE_CONTEXT', 'REQUEST_DATA'}
_asset_cache    = {}  # FIX #12: crypto detection cache

# ── Session ───────────────────────────────────────────────────────────────────

def get_session() -> str:
    """
    Returns: REGULAR | PRE_MARKET | AFTER_HOURS | CLOSED
    Uses Alpaca clock (handles holidays/early closes) + pytz for ET time.
    """
    try:
        clock  = trading_client.get_clock()
        now_et = datetime.now(ET).strftime('%H:%M')
        if clock.is_open:
            session = 'REGULAR'
        elif now_et == PRE_MARKET_TIME:
            session = 'PRE_MARKET'
        elif now_et == AFTER_HOURS_TIME:
            session = 'AFTER_HOURS'
        else:
            session = 'CLOSED'
        log.info(f"Session: {session} | ET: {now_et} | Market open: {clock.is_open}")
        return session
    except Exception as e:
        log.error(f"Clock check failed: {e}")
        return 'CLOSED'

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_system_prompt() -> str:
    with open('system_prompt.txt', 'r', encoding='utf-8') as f:
        return f.read()


def is_crypto(ticker: str) -> bool:
    """
    FIX #12: Was fragile '/' check. Now uses Alpaca asset class with cache.
    Falls back to '/' heuristic if API call fails.
    """
    if ticker not in _asset_cache:
        try:
            asset = trading_client.get_asset(ticker)
            _asset_cache[ticker] = str(asset.asset_class).lower() == 'crypto'
        except Exception:
            _asset_cache[ticker] = '/' in ticker
    return _asset_cache[ticker]


def check_kill_switch(equity: float, last_equity: float) -> bool:
    """
    FIX #6: Emergency DAILY kill switch only (for flash crashes).
    Monthly drawdown protection is delegated to Claude via decisions.log context
    and the system prompt rule: HOLD if equity < 85% of month-start equity.
    """
    if last_equity <= 0:
        return False
    daily_drawdown = (last_equity - equity) / last_equity
    if daily_drawdown > KILL_SWITCH_DRAWDOWN:
        log.warning(f"KILL SWITCH — daily drawdown {daily_drawdown:.2%} > threshold {KILL_SWITCH_DRAWDOWN:.2%}")
        return True
    return False


def get_portfolio_state() -> tuple:
    """
    FIX #4: returns positions list for market briefing.
    FIX #8: includes last 20 filled orders.
    Returns: (state_string, positions_list, equity_float, last_equity_float)
    """
    account   = trading_client.get_account()
    positions = trading_client.get_all_positions()

    # FIX #8: last 20 filled orders
    try:
        orders = trading_client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=20)
        )
    except Exception as e:
        log.warning(f"Could not fetch recent orders: {e}")
        orders = []

    state  = f"Cash available:   ${float(account.cash):.2f}\n"
    state += f"Portfolio equity: ${float(account.equity):.2f}\n"
    state += f"Buying power:     ${float(account.buying_power):.2f}\n"
    state += f"Day trade count:  {account.daytrade_count}\n"

    state += "\nOPEN POSITIONS:\n"
    if not positions:
        state += "  None\n"
    for p in positions:
        pl_pct = float(p.unrealized_plpc) * 100
        state += (
            f"  {p.symbol} | Qty: {p.qty}"
            f" | Entry: ${float(p.avg_entry_price):.2f}"
            f" | Current: ${float(p.current_price):.2f}"
            f" | P&L: {'+' if pl_pct >= 0 else ''}{pl_pct:.2f}% (${float(p.unrealized_pl):.2f})\n"
        )

    state += "\nLAST 20 FILLED ORDERS:\n"
    if not orders:
        state += "  None\n"
    for o in (orders or [])[:20]:
        filled_at = o.filled_at.strftime('%Y-%m-%d %H:%M') if o.filled_at else '?'
        price     = float(o.filled_avg_price) if o.filled_avg_price else 0.0
        state += (
            f"  {filled_at} | {o.side.value.upper()} {o.filled_qty} {o.symbol}"
            f" @ ${price:.2f} | {o.order_type.value}\n"
        )

    equity      = float(account.equity)
    last_equity = float(account.last_equity)
    log.info(f"Portfolio: equity=${equity:.2f} | positions={len(positions)} | cash={float(account.cash):.2f}")
    return state, positions, equity, last_equity


def get_market_briefing(positions: list) -> str:
    """
    FIX #2: was a stub. Now fetches real data.
    FIX #3: uses data_client for snapshots.
    FIX #4: receives positions to filter news by portfolio tickers.
    """
    briefing = "MACRO / MARKET BRIEFING:\n"

    # 1. Macro proxies: SPY, QQQ, IWM
    try:
        snap_req  = StockSnapshotRequest(symbol_or_symbols=['SPY', 'QQQ', 'IWM'])
        snapshots = data_client.get_stock_snapshot(snap_req)
        briefing += "\nMARKET SNAPSHOTS:\n"
        for sym, snap in snapshots.items():
            price     = f"${snap.latest_trade.price:.2f}" if snap.latest_trade else "N/A"
            day_open  = snap.daily_bar.open  if snap.daily_bar else None
            day_close = snap.daily_bar.close if snap.daily_bar else None
            change    = f"{day_close - day_open:+.2f}" if (day_open and day_close) else "N/A"
            briefing += f"  {sym}: {price} ({change} today)\n"
    except Exception as e:
        log.warning(f"Snapshots unavailable: {e}")
        briefing += "  Snapshots: unavailable\n"

    # 2. Most actives (screener — direct API, not yet in SDK)
    try:
        resp = requests.get(
            'https://data.alpaca.markets/v1beta1/screener/stocks/most-actives',
            headers={
                'APCA-API-KEY-ID':     ALPACA_KEY,
                'APCA-API-SECRET-KEY': ALPACA_SECRET
            },
            timeout=5
        )
        if resp.ok:
            movers = resp.json().get('most_actives', [])[:10]
            briefing += "\nMOST ACTIVES:\n"
            for m in movers:
                briefing += (
                    f"  {m['symbol']}"
                    f" | Vol: {m.get('volume', '?')}"
                    f" | Change: {m.get('change_percent', '?')}%\n"
                )
    except Exception as e:
        log.warning(f"Most actives unavailable: {e}")

    # 3. News — portfolio tickers first, then general market
    try:
        alpaca_headers = {
            'APCA-API-KEY-ID':     ALPACA_KEY,
            'APCA-API-SECRET-KEY': ALPACA_SECRET
        }
        news_items = []

        # Portfolio tickers (if any open positions)
        open_tickers = [p.symbol for p in positions]
        if open_tickers:
            r = requests.get(
                'https://data.alpaca.markets/v1beta1/news',
                headers=alpaca_headers,
                params={'symbols': ','.join(open_tickers), 'limit': 20, 'sort': 'desc'},
                timeout=5
            )
            if r.ok:
                news_items += r.json().get('news', [])
            log.info(f"News (portfolio tickers {open_tickers}): {len(news_items)} articles")

        # General market news
        r = requests.get(
            'https://data.alpaca.markets/v1beta1/news',
            headers=alpaca_headers,
            params={'limit': 20, 'sort': 'desc'},
            timeout=5
        )
        if r.ok:
            news_items += r.json().get('news', [])

        # Deduplicate by id
        seen, unique = set(), []
        for n in news_items:
            if n['id'] not in seen:
                seen.add(n['id'])
                unique.append(n)

        briefing += f"\nNEWS (last 24h — {len(unique)} articles):\n"
        for n in unique[:30]:
            syms     = ', '.join(n.get('symbols', [])) or 'general'
            headline = n.get('headline', '')
            date     = n.get('created_at', '')[:10]
            briefing += f"  [{syms}] {headline} ({date})\n"

    except Exception as e:
        log.warning(f"News fetch failed: {e}")
        briefing += "  News: unavailable\n"

    # 4. Crypto Fear & Greed Index
    try:
        fg  = requests.get('https://api.alternative.me/fng/?limit=1', timeout=5).json()
        val = fg['data'][0]['value']
        cls = fg['data'][0]['value_classification']
        briefing += f"\nCrypto Fear & Greed: {val} ({cls})\n"
    except Exception:
        briefing += "\nCrypto Fear & Greed: unavailable\n"

    return briefing


def _format_ticker_block(symbol: str, quote, bars: list) -> str:
    """Format one ticker's market data as a human-readable string block."""
    try:
        bid = f"${quote.bid_price:.2f}" if (quote and quote.bid_price) else "N/A"
        ask = f"${quote.ask_price:.2f}" if (quote and quote.ask_price) else "N/A"

        if bars:
            latest     = bars[-1]
            change_str = "N/A"
            if len(bars) >= 2 and bars[-2].close:
                pct        = (latest.close - bars[-2].close) / bars[-2].close * 100
                change_str = f"{pct:+.2f}%"
            vol     = latest.volume or 0
            vol_str = f"{vol / 1e6:.1f}M" if vol >= 1_000_000 else str(int(vol))
            hist    = [f"{b.close:.2f}" for b in bars[-21:-1]] if len(bars) > 1 else []
            return (
                f"{symbol} | Last: ${latest.close:.2f} | Bid: {bid} | Ask: {ask}\n"
                f"     Today: O ${latest.open:.2f} H ${latest.high:.2f} "
                f"L ${latest.low:.2f} C ${latest.close:.2f}"
                f" | Change: {change_str} | Vol: {vol_str}\n"
                f"     Last {len(hist)} days close: {', '.join(hist) if hist else 'N/A'}\n\n"
            )
        return f"{symbol} | No bar data | Bid: {bid} | Ask: {ask}\n\n"
    except Exception as e:
        log.warning(f"Error formatting data for {symbol}: {e}")
        return f"{symbol} | Data unavailable\n\n"


def fetch_market_data(tickers: list) -> str:
    """
    Phase 2 data fetch: OHLCV (last 21 daily bars) + latest quote for each requested ticker.
    Returns formatted BLOCK 6 string to append to the Phase 2 prompt.
    Handles stock and crypto tickers separately; errors per-ticker are non-fatal.
    """
    now_et = datetime.now(ET)
    block  = (
        f"--- BLOCK 6: MARKET DATA (Real-Time) ---\n"
        f"Fetched at: {now_et.strftime('%Y-%m-%d %H:%M')} ET\n\n"
    )

    stock_tickers  = [t for t in tickers if not is_crypto(t)]
    crypto_tickers = [t for t in tickers if is_crypto(t)]

    # ── Stocks ────────────────────────────────────────────────────────────────
    if stock_tickers:
        quotes   = {}
        bars_map = {sym: [] for sym in stock_tickers}

        try:
            quotes = data_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=stock_tickers)
            )
        except Exception as e:
            log.warning(f"Stock latest quotes unavailable: {e}")

        try:
            b_res = data_client.get_stock_bars(
                StockBarsRequest(symbol_or_symbols=stock_tickers, timeframe=TimeFrame.Day, limit=21)
            )
            for sym in stock_tickers:
                try:
                    bars_map[sym] = list(b_res[sym])
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"Stock bars unavailable: {e}")

        for sym in stock_tickers:
            block += _format_ticker_block(sym, quotes.get(sym), bars_map[sym])

    # ── Crypto ────────────────────────────────────────────────────────────────
    if crypto_tickers:
        c_quotes   = {}
        c_bars_map = {sym: [] for sym in crypto_tickers}

        try:
            cc      = CryptoHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
            c_quotes = cc.get_crypto_latest_quote(
                CryptoLatestQuoteRequest(symbol_or_symbols=crypto_tickers)
            )
            c_res = cc.get_crypto_bars(
                CryptoBarsRequest(symbol_or_symbols=crypto_tickers, timeframe=TimeFrame.Day, limit=21)
            )
            for sym in crypto_tickers:
                try:
                    c_bars_map[sym] = list(c_res[sym])
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"Crypto market data unavailable: {e}")

        for sym in crypto_tickers:
            block += _format_ticker_block(sym, c_quotes.get(sym), c_bars_map[sym])

    return block


def read_log(filename: str, from_date: str = None, default_days: int = DEFAULT_LOG_DAYS) -> str:
    """Read log file filtered by date. Falls back to last 100 lines if no matches."""
    if not os.path.exists(filename):
        return f"[{filename} not found]\n"
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        cutoff   = from_date or (datetime.now() - timedelta(days=default_days)).strftime('%Y-%m-%d')
        filtered = [l for l in lines if len(l) >= 10 and l[:10] >= cutoff]
        if not filtered and lines:
            log.warning(f"{filename}: no lines after {cutoff}, returning last 100 lines as fallback")
            return "".join(lines[-100:])
        return "".join(filtered)
    except Exception as e:
        log.error(f"Error reading {filename}: {e}")
        return ""


def collect_context(session: str, from_date: str = None) -> tuple:
    """
    Assemble full briefing for Claude.
    FIX #4: positions passed to market briefing.
    Returns: (sys_prompt, user_prompt, equity, last_equity)
    """
    sys_prompt                          = load_system_prompt()
    portfolio_state, positions, equity, last_equity = get_portfolio_state()
    market                              = get_market_briefing(positions)  # FIX #4
    decisions                           = read_log('decisions.log', from_date=from_date)
    trades                              = read_log('trades.log', default_days=9999)

    cycle_instr = f"""
ACTIVE SESSION: {session}

Phase 1 — HUNTING:
  Scan themes, sectors, catalysts. Do not limit to current portfolio.

Phase 2 — ANALYSIS:
  For each open position: verify original thesis in Block 4B is still valid.
  For candidates: evaluate momentum, risk, sizing.
  In PRE_MARKET / AFTER_HOURS: assess if action is urgent or can wait for REGULAR session.

Phase 3 — VALIDATION (self-check before emitting JSON):
  [ ] Positions after this order <= 5 (stock + crypto combined)
  [ ] Position size <= 10% equity
  [ ] Stop loss present on every new BUY
  [ ] In extended hours: limit orders only, conservative size
  [ ] daytrade_count respected (no day trades if count >= 3 and equity < $25,000)
  [ ] Confidence >= 0.65 for any action other than HOLD
  [ ] Monthly drawdown: if current equity < 85% of equity recorded at month start
      (visible in Block 4A decisions.log), emit HOLD only — no new positions until month resets.

Output: reasoning in plain language, then a single JSON block.
If you need more history than Block 4A provides, emit NEED_MORE_CONTEXT (max 2 times per cycle).
If you need current price data to size a position correctly, emit REQUEST_DATA (see system prompt for schema and rules).
"""

    user_prompt  = f"--- BLOCK 2: PORTFOLIO ---\n{portfolio_state}\n\n"
    user_prompt += f"--- BLOCK 3: MARKET ---\n{market}\n\n"
    user_prompt += f"--- BLOCK 4A: DECISIONS LOG (last {DEFAULT_LOG_DAYS} days) ---\n{decisions}\n\n"
    user_prompt += f"--- BLOCK 4B: TRADES LOG (full history) ---\n{trades}\n\n"
    user_prompt += f"--- BLOCK 5: CYCLE INSTRUCTION ---\n{cycle_instr}\n"

    return sys_prompt, user_prompt, equity, last_equity


# ── Claude ────────────────────────────────────────────────────────────────────

def ask_claude(sys_prompt: str, user_prompt: str) -> dict:
    """
    Call Claude and return parsed JSON decision.
    Logs full prompt to app.log at DEBUG level (controlled by LOG_PROMPTS env var).
    """
    # Log prompt to app.log (DEBUG → only in file, not console)
    if LOG_PROMPTS:
        log.debug(
            f"\n{'=' * 60}\n"
            f"[PROMPT → CLAUDE START]\n"
            f"{'=' * 60}\n"
            f"SYSTEM PROMPT:\n{sys_prompt}\n\n"
            f"USER PROMPT:\n{user_prompt}\n"
            f"{'=' * 60}\n"
            f"[PROMPT → CLAUDE END]\n"
            f"{'=' * 60}"
        )

    max_retries = 4
    base_delay  = 30  # seconds

    for attempt in range(1, max_retries + 1):
        try:
            response = anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                system=sys_prompt,
                messages=[{"role": "user", "content": user_prompt}]
            )
            raw     = response.content[0].text
            in_tok  = response.usage.input_tokens
            out_tok = response.usage.output_tokens
            log.info(f"Claude raw response received | tokens: {in_tok} in + {out_tok} out")

            # Log full Claude response to app.log
            log.debug(
                f"\n{'=' * 60}\n"
                f"[CLAUDE RESPONSE]\n"
                f"{'=' * 60}\n"
                f"{raw}\n"
                f"{'=' * 60}"
            )

            # Extract JSON — three strategies in order of priority:
            # 1. Fenced ```json ... ``` block
            # 2. Fenced ``` ... ``` block
            # 3. Last { ... } block in the response (handles: reasoning prose + raw JSON at end)
            content = raw
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            else:
                # Find the last top-level JSON object in the response.
                # Claude writes prose first, then the JSON — so we scan from the end.
                start = content.rfind('{')
                end   = content.rfind('}')
                if start != -1 and end != -1 and end > start:
                    content = content[start:end + 1].strip()
                # If no braces found, json.loads will raise and we log the error below.

            decision = json.loads(content)
            log.info(f"Claude decided: {decision.get('action')} {decision.get('ticker', '')}")
            return decision

        except OverloadedError:
            # Explicit handling for 529 Overloaded (subclass of APIStatusError, but named for clarity)
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                log.warning(f"Claude API overloaded (attempt {attempt}/{max_retries}), retry in {delay}s")
                time.sleep(delay)
                continue
            log.error("Claude API overloaded — max retries reached, skipping cycle")
            return None
        except APIStatusError as e:
            if e.status_code in (429, 500, 502, 503, 529) and attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))  # 30s, 60s, 120s
                log.warning(f"Claude API transient error (HTTP {e.status_code}), retry {attempt}/{max_retries} in {delay}s")
                time.sleep(delay)
                continue
            log.error(f"Claude API error: {e}\n{traceback.format_exc()}")
            return None
        except json.JSONDecodeError as e:
            # Attempt partial parse via regex before giving up
            action_m = re.search(r'"action"\s*:\s*"([A-Z_]+)"', raw)
            conf_m   = re.search(r'"confidence"\s*:\s*([\d.]+)', raw)
            reason_m = re.search(r'"reasoning"\s*:\s*"([^"]{0,400})', raw)
            if action_m:
                ext_action = action_m.group(1)
                ext_conf   = float(conf_m.group(1)) if conf_m else 0.0
                ext_reason = (reason_m.group(1) if reason_m else "JSON truncated") + " [partial parse]"
                if ext_action in ('HOLD', 'REQUEST_DATA', 'NEED_MORE_CONTEXT'):
                    partial = {"action": ext_action, "confidence": ext_conf, "reasoning": ext_reason}
                    if ext_action == 'REQUEST_DATA':
                        t_m = re.search(r'"tickers"\s*:\s*\[([^\]]*)\]', raw)
                        if t_m:
                            partial['tickers'] = [
                                t.strip().strip('"') for t in t_m.group(1).split(',')
                                if t.strip().strip('"')
                            ]
                    log.warning(f"JSON truncated — safe partial parse for action '{ext_action}'")
                    return partial
                # BUY/SELL with truncated JSON → risk fields missing → force HOLD
                log.error(f"JSON truncated on unsafe action '{ext_action}' — forcing HOLD")
                return {"action": "HOLD", "confidence": ext_conf,
                        "reasoning": f"Forced HOLD: JSON truncated on {ext_action} (risk fields missing)"}
            log.error(f"Claude returned invalid JSON: {e}\nRaw response: {raw[:500]}")
            return None
        except Exception as e:
            log.error(f"Claude API error: {e}\n{traceback.format_exc()}")
            return None


# ── Logging ───────────────────────────────────────────────────────────────────

def write_decision_log(decision: dict):
    """
    FIX #1: Write ONLY Claude's reasoning to decisions.log.
    Never writes the prompt — that was causing exponential self-poisoning.
    """
    action = decision.get('action', 'UNKNOWN')
    ticker = decision.get('ticker', '')
    reason = decision.get('reasoning', '')
    ts     = datetime.now().strftime('%Y-%m-%d %H:%M')
    line   = f"{ts} | {action}{' ' + ticker if ticker else ''} | {reason}\n"
    try:
        with open('decisions.log', 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception as e:
        log.error(f"Failed to write decisions.log: {e}")


def write_trade_log(decision: dict):
    """Write trade event to trades.log (BUY / SELL / ADJUST)."""
    action = decision.get('action', '')
    ticker = decision.get('ticker', '')
    ts     = datetime.now().strftime('%Y-%m-%d %H:%M')
    line   = (
        f"{ts} | {action} {ticker}"
        f" | Qty: {decision.get('qty', '?')}"
        f" | SL: {decision.get('stop_loss', '?')}"
        f" | TP: {decision.get('take_profit', '?')}"
        f" | Conf: {decision.get('confidence', '?')}"
        f" | Thesis: {decision.get('reasoning', '')}\n"
    )
    try:
        with open('trades.log', 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception as e:
        log.error(f"Failed to write trades.log: {e}")


# ── Telegram ──────────────────────────────────────────────────────────────────

def notify_telegram(decision: dict):
    """
    FIX #9: same details (TP, SL, confidence) for both BUY and SELL.
    Non-blocking: Telegram failure never stops the trading loop.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    action = decision.get('action', '?')
    ticker = decision.get('ticker', '?')
    icon   = "🟢" if action == "BUY" else "🔴"
    tp     = decision.get('take_profit', 'N/A')
    sl     = decision.get('stop_loss',   'N/A')
    conf   = decision.get('confidence',  'N/A')

    msg = (
        f"{icon} *{action} {ticker}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*Reasoning:* {decision.get('reasoning', '')}\n\n"
        f"*TP:* ${tp}  |  *SL:* ${sl}  |  *Confidence:* {conf}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5
        )
        log.info(f"Telegram sent: {action} {ticker}")
    except Exception as e:
        log.warning(f"Telegram failed (non-blocking): {e}")


# ── Execution ─────────────────────────────────────────────────────────────────

def execute_decision(decision: dict, equity: float, last_equity: float):
    """
    Execute Claude's decision. Safety checks then Alpaca order submission.
    FIX #1:  write_decision_log writes reasoning only, never the prompt.
    FIX #6:  kill switch is daily-only; monthly handled by Claude.
    FIX #7:  bracket (stop + TP) supported for both limit AND market BUY orders.
    FIX #12: crypto TIF via asset class lookup, not '/' heuristic.
    """
    action = decision.get('action')
    ticker = decision.get('ticker')
    qty    = int(decision.get('qty') or 0)

    # Always log reasoning (Fix #1: reasoning only, not the prompt)
    write_decision_log(decision)

    if action not in ALLOWED_ACTIONS:
        log.warning(f"ORDER BLOCKED — unknown action: {action}")
        return

    if action in ('HOLD', 'NEED_MORE_CONTEXT', 'REQUEST_DATA'):
        log.info(f"No order executed — action: {action}")
        return

    if action == 'ADJUST':
        log.info(f"ADJUST {ticker}: {decision.get('reasoning', '')}")
        write_trade_log(decision)
        # Trailing stop / modification logic goes here
        return

    # Physical safety checks
    if qty <= 0:
        log.warning(f"ORDER BLOCKED — qty {qty} is invalid")
        return
    if qty > MAX_ORDER_QTY:
        log.warning(f"ORDER BLOCKED — qty {qty} exceeds MAX_ORDER_QTY {MAX_ORDER_QTY}")
        return

    # FIX #6: daily kill switch (flash-crash guard only)
    if check_kill_switch(equity, last_equity):
        log.warning("ORDER BLOCKED — daily kill switch active")
        return

    try:
        side           = OrderSide.BUY if action == 'BUY' else OrderSide.SELL
        order_type_str = decision.get('order_type', 'market').lower()

        # FIX #12: proper crypto detection via asset class cache
        tif = TimeInForce.GTC if is_crypto(ticker) else TimeInForce.DAY

        # FIX #7: build bracket for BUY regardless of order type
        take_profit = None
        stop_loss   = None
        if action == 'BUY':
            if decision.get('take_profit'):
                take_profit = {"limit_price": float(decision['take_profit'])}
            if decision.get('stop_loss'):
                stop_loss   = {"stop_price": float(decision['stop_loss'])}

        has_bracket = bool(take_profit or stop_loss)
        order_class = OrderClass.BRACKET if (action == 'BUY' and has_bracket) else OrderClass.SIMPLE

        base_kwargs = {
            "symbol":        ticker,
            "qty":           qty,
            "side":          side,
            "time_in_force": tif,
            "order_class":   order_class,
        }
        if action == 'BUY' and take_profit: base_kwargs['take_profit'] = take_profit
        if action == 'BUY' and stop_loss:   base_kwargs['stop_loss']   = stop_loss

        if order_type_str == 'limit':
            limit_price = float(decision.get('limit_price', 0))
            if limit_price <= 0:
                log.warning("ORDER BLOCKED — limit order missing valid limit_price")
                return
            base_kwargs['limit_price'] = limit_price
            req = LimitOrderRequest(**base_kwargs)
        else:
            # FIX #7: MarketOrderRequest with bracket — stop loss no longer silently dropped
            req = MarketOrderRequest(**base_kwargs)

        trading_client.submit_order(order_data=req)
        log.info(
            f"Order submitted: {action} {qty} {ticker} ({order_type_str})"
            f" | SL={decision.get('stop_loss')} TP={decision.get('take_profit')}"
        )

        write_trade_log(decision)
        notify_telegram(decision)

    except Exception as e:
        log.error(f"Alpaca order failed: {e}\n{traceback.format_exc()}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_cycle() -> tuple:
    """
    One full cycle: session detection → data collection → Claude → execution.

    Two-phase support:
      Phase 1 — Claude may emit REQUEST_DATA listing tickers it wants priced.
                 Python fetches real-time OHLCV and appends BLOCK 6 to the prompt.
      Phase 2 — Claude receives BLOCK 6 and makes the final BUY/SELL/HOLD decision.
    REQUEST_DATA is allowed only once per cycle; a second REQUEST_DATA forces HOLD.

    FIX #5:  MAX_CONTEXT_RETRY = 2 (was effectively 3).
    FIX #10: returns session so main loop doesn't call get_session() a second time.
    Returns: (ran: bool, session: str)
    """
    session = get_session()
    log.info(f"=== Cycle start | session: {session} ===")

    if session == 'CLOSED':
        log.info("Market closed — sleeping 60s")
        time.sleep(60)
        return False, session

    retry_count = 0
    from_date   = None
    phase1_used = False  # REQUEST_DATA may only occur once per cycle

    while retry_count <= MAX_CONTEXT_RETRY:  # FIX #5: 0,1,2 → max 2 retries + initial = 3 calls max
        sys_prompt, user_prompt, equity, last_equity = collect_context(session, from_date)
        decision = ask_claude(sys_prompt, user_prompt)

        if decision is None:
            log.error("No valid decision from Claude — skipping execution this cycle")
            return True, session

        action = decision.get('action')

        if action == 'NEED_MORE_CONTEXT':
            if retry_count < MAX_CONTEXT_RETRY:
                from_date    = decision.get('need_more_context', {}).get('from_date')
                reason       = decision.get('need_more_context', {}).get('reason', '')
                retry_count += 1
                log.info(f"Context retry {retry_count}/{MAX_CONTEXT_RETRY} — from: {from_date} | reason: {reason}")
                continue
            else:
                log.warning("Max context retries reached — forcing HOLD")
                decision = {
                    "action":    "HOLD",
                    "reasoning": "Forced HOLD: context still insufficient after 2 retries."
                }

        elif action == 'REQUEST_DATA':
            if phase1_used:
                # Second REQUEST_DATA in the same cycle → guard against infinite loop
                log.warning("REQUEST_DATA received after Phase 1 already consumed — forcing HOLD")
                decision = {
                    "action":    "HOLD",
                    "reasoning": "Forced HOLD: REQUEST_DATA is not allowed in Phase 2."
                }
            else:
                tickers   = decision.get('tickers', [])
                reasoning = decision.get('reasoning', '')
                log.info(f"Phase 1 REQUEST_DATA | tickers: {tickers} | reason: {reasoning}")

                if not tickers:
                    log.warning("REQUEST_DATA with empty tickers list — forcing HOLD")
                    decision = {"action": "HOLD", "reasoning": "Forced HOLD: REQUEST_DATA had no tickers."}
                else:
                    market_data_block = fetch_market_data(tickers)
                    phase2_prompt     = user_prompt + f"\n{market_data_block}"
                    phase1_used       = True

                    log.info(f"Market data fetched for {len(tickers)} ticker(s) — calling Claude Phase 2")
                    decision = ask_claude(sys_prompt, phase2_prompt)

                    if decision is None:
                        log.error("No valid decision from Claude Phase 2 — skipping cycle")
                        return True, session

                    # Guard: Phase 2 must not emit REQUEST_DATA again
                    if decision.get('action') == 'REQUEST_DATA':
                        log.warning("REQUEST_DATA returned in Phase 2 — forcing HOLD to prevent loop")
                        decision = {
                            "action":    "HOLD",
                            "reasoning": "Forced HOLD: REQUEST_DATA is not allowed in Phase 2."
                        }

        execute_decision(decision, equity, last_equity)
        break

    log.info(f"=== Cycle end | session: {session} ===")
    return True, session


if __name__ == "__main__":
    while True:
        try:
            ran, session = run_cycle()  # FIX #10: session from run_cycle, not re-fetched

            if ran:
                if session == 'REGULAR':
                    log.info("Next cycle in 1h")
                    time.sleep(3600)
                elif session == 'PRE_MARKET':
                    # Sleep until regular market open (09:30 ET) so the full session is covered
                    now_et   = datetime.now(ET)
                    open_et  = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
                    secs     = max(60, (open_et - now_et).total_seconds())
                    log.info(f"PRE_MARKET done. Next cycle at market open in {secs/60:.0f}m")
                    time.sleep(secs)
                elif session == 'AFTER_HOURS':
                    log.info("Next cycle in ~6h")
                    time.sleep(21600)
                # CLOSED: sleep(60) already done inside run_cycle

        except Exception as e:
            log.error(f"Critical error in main loop: {e}\n{traceback.format_exc()}")
            time.sleep(60)