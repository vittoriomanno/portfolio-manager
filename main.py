import os
import time
import json
import logging
import requests
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

# Alpaca API
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# Anthropic API
from anthropic import Anthropic

# --- Setup Logging ---
log = logging.getLogger('portfolio_manager')
log.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# Console handler
ch = logging.StreamHandler()
ch.setFormatter(formatter)
log.addHandler(ch)

# File handler
fh = logging.FileHandler('app.log')
fh.setFormatter(formatter)
log.addHandler(fh)

# --- Load Environment ---
load_dotenv()

ALPACA_KEY = os.getenv('ALPACA_KEY')
ALPACA_SECRET = os.getenv('ALPACA_SECRET')
ALPACA_PAPER = os.getenv('ALPACA_PAPER', 'true').lower() == 'true'

ANTHROPIC_KEY = os.getenv('ANTHROPIC_KEY')

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

MAX_ORDER_QTY = int(os.getenv('MAX_ORDER_QTY', 500))
KILL_SWITCH_DRAWDOWN = float(os.getenv('KILL_SWITCH_DRAWDOWN', 0.15))
PRE_MARKET_TIME = os.getenv('PRE_MARKET_TIME', '08:00')
AFTER_HOURS_TIME = os.getenv('AFTER_HOURS_TIME', '17:00')

ET = pytz.timezone('America/New_York')

# --- Initialization ---
log.info("Starting AI Portfolio Manager")
if not (ALPACA_KEY and ALPACA_SECRET and ANTHROPIC_KEY):
    log.error("Missing critical environment variables.")
    exit(1)

trading_client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=ALPACA_PAPER)
anthropic_client = Anthropic(api_key=ANTHROPIC_KEY)

ALLOWED_ACTIONS = ['BUY', 'SELL', 'HOLD', 'ADJUST', 'NEED_MORE_CONTEXT']

def load_system_prompt():
    with open('system_prompt.txt', 'r') as f:
        return f.read()

def get_session():
    try:
        clock = trading_client.get_clock()
        now_et = datetime.now(ET).strftime('%H:%M')
        if clock.is_open:
            return 'REGULAR'
        elif now_et == PRE_MARKET_TIME:
            return 'PRE_MARKET'
        elif now_et == AFTER_HOURS_TIME:
            return 'AFTER_HOURS'
        else:
            return 'CLOSED'
    except Exception as e:
        log.error(f"Error checking session clock: {e}")
        return 'CLOSED'

def check_kill_switch():
    try:
        account = trading_client.get_account()
        equity = float(account.equity)
        last_equity = float(account.last_equity) # Can be improved by tracking monthly high
        # Quick drawdown calculation against last day's equity for now
        drawdown = (last_equity - equity) / last_equity if last_equity > 0 else 0
        if drawdown > KILL_SWITCH_DRAWDOWN:
            log.warning(f"KILL SWITCH ACTIVE: Drawdown {drawdown:.2%} exceeds {KILL_SWITCH_DRAWDOWN:.2%}")
            return True
        return False
    except Exception as e:
        log.error(f"Error checking kill switch: {e}")
        return False

def get_portfolio_state():
    account = trading_client.get_account()
    positions = trading_client.get_all_positions()
    
    state = f"Cash available: ${float(account.cash):.2f}\n"
    state += f"Portfolio equity: ${float(account.equity):.2f}\n"
    state += f"Buying power: ${float(account.buying_power):.2f}\n"
    state += f"Day trade count: {account.daytrade_count}\n\nOPEN POSITIONS:\n"
    
    if not positions:
        state += "  None\n"
    for p in positions:
        pl_pct = float(p.unrealized_plpc) * 100
        state += f"  {p.symbol} | Qty: {p.qty} | Entry: ${float(p.avg_entry_price):.2f} | Current: ${float(p.current_price):.2f}\n"
        state += f"       | P&L: {'+' if pl_pct > 0 else ''}{pl_pct:.2f}% (${float(p.unrealized_pl):.2f})\n"
    
    return state

def get_market_briefing(positions):
    # Dummy implementation for Fear & Greed, can be expanded to full Alternative.me API
    try:
        fg_req = requests.get('https://api.alternative.me/fng/?limit=1', timeout=5)
        fg_data = fg_req.json()
        fg_val = fg_data['data'][0]['value']
        fg_str = fg_data['data'][0]['value_classification']
        fg_text = f"Crypto Fear & Greed: {fg_val} ({fg_str})"
    except:
        fg_text = "Crypto Fear & Greed: Unavailable"

    # Minimal Market summary (could fetch SPY/QQQ quotes here)
    brief = f"MACRO / CONTEXT:\n{fg_text}\n(Market snapshots skipped in this MVP script version, assume current real-time state).\n"
    return brief

def read_log(filename, from_date=None, default_days=14):
    if not os.path.exists(filename):
        return ""
    
    try:
        with open(filename, 'r') as f:
            lines = f.readlines()
        
        # Simple date filtering based on line prefix assumption (YYYY-MM-DD)
        if from_date:
            target_date = from_date
        else:
            target_date = (datetime.now() - timedelta(days=default_days)).strftime('%Y-%m-%d')
            
        filtered = [l for l in lines if len(l)>10 and l[:10] >= target_date]
        if not filtered and lines:
            # Fallback if parsing fails, just return tail end
            return "".join(lines[-100:])
        return "".join(filtered)
    except Exception as e:
        log.error(f"Error reading {filename}: {e}")
        return ""

def collect_context(session, from_date=None):
    sys_prompt = load_system_prompt()
    portfolio = get_portfolio_state()
    market = get_market_briefing([])
    
    decisions_log = read_log('decisions.log', from_date=from_date, default_days=14)
    trades_log = read_log('trades.log', default_days=3650) # essentially full history
    
    cycle_instruction = f"""
ACTIVE SESSION: {session}

Phase 1 — HUNTING: identify themes, sectors, candidates
Phase 2 — ANALYSIS: evaluate candidates + open positions
Phase 3 — VALIDATION:
  [ ] Positions after this order <= 5
  [ ] Position size <= 10% equity
  [ ] Stop loss present
  [ ] Extended hours: limit orders only
  [ ] daytrade_count respected
  [ ] Confidence >= 0.75
"""
    
    prompt = f"--- BLOCK 2: PORTFOLIO ---\n{portfolio}\n\n"
    prompt += f"--- BLOCK 3: MARKET ---\n{market}\n\n"
    prompt += f"--- BLOCK 4A: DECISIONS LOG (Recent) ---\n{decisions_log}\n\n"
    prompt += f"--- BLOCK 4B: TRADES LOG (Full) ---\n{trades_log}\n\n"
    prompt += f"--- BLOCK 5: INSTRUCTION ---\n{cycle_instruction}\n"
    
    return sys_prompt, prompt

def ask_claude(sys_prompt, user_prompt):
    try:
        response = anthropic_client.messages.create(
            model="claude-3-7-sonnet-20250219", # Hardcoded model
            max_tokens=2000,
            system=sys_prompt,
            messages=[
                {"role": "user", "content": user_prompt}
            ]
        )
        content = response.content[0].text
        # Extract JSON from block if needed
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        
        return json.loads(content)
    except Exception as e:
        log.error(f"Error communicating with Claude: {e}")
        return None

def write_log(filename, content):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    try:
        with open(filename, 'a') as f:
            f.write(f"{timestamp} | {content}\n")
    except Exception as e:
        log.error(f"Error writing to {filename}: {e}")

def notify_telegram(decision):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    action = decision.get('action')
    ticker = decision.get('ticker', 'UNKNOWN')
    
    icon = "🟢" if action == "BUY" else "🔴"
    msg = f"{icon} {action} {ticker}\n━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"Reasoning: {decision.get('reasoning', '')}\n\n"
    
    if action == "BUY":
        msg += f"Expected: TP ${decision.get('take_profit', 'N/A')} | SL ${decision.get('stop_loss', 'N/A')} | Confidence {decision.get('confidence', '')}"
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except Exception as e:
        log.warning(f"Telegram failed (non-blocking): {e}")

def execute_decision(decision):
    action = decision.get('action')
    ticker = decision.get('ticker')
    qty = decision.get('qty', 0)
    
    if action not in ALLOWED_ACTIONS:
        log.warning(f"ORDER BLOCKED — unknown action: {action}")
        return
        
    if action == 'HOLD':
        reason = decision.get('reasoning', 'No reason provided')
        write_log('decisions.log', f"HOLD: {reason}")
        log.info("Claude decided to HOLD.")
        return
        
    if action == 'ADJUST':
        reason = decision.get('reasoning', 'No reason provided')
        write_log('decisions.log', f"ADJUST {ticker}: {reason}")
        log.info(f"Claude decided to ADJUST {ticker}.")
        # (Trailing stop adjust logic would go here in a full implementation)
        return
    
    if qty > MAX_ORDER_QTY:
        log.warning(f"ORDER BLOCKED — qty {qty} exceeds MAX_ORDER_QTY {MAX_ORDER_QTY}")
        return
        
    if check_kill_switch():
        log.warning("ORDER BLOCKED — Kill switch is active.")
        return

    # Execute BUY / SELL via Alpaca
    try:
        side = OrderSide.BUY if action == 'BUY' else OrderSide.SELL
        order_type_str = decision.get('order_type', 'market').lower()
        
        # Build bracket if BUY
        take_profit = None
        stop_loss = None
        
        if action == 'BUY' and decision.get('take_profit'):
            take_profit = {"limit_price": float(decision.get('take_profit'))}
        if action == 'BUY' and decision.get('stop_loss'):
            stop_loss = {"stop_price": float(decision.get('stop_loss'))}
            
        kwargs = {
            "symbol": ticker,
            "qty": qty,
            "side": side,
            "time_in_force": TimeInForce.DAY,
        }
        
        if order_type_str == 'limit':
            kwargs['limit_price'] = float(decision.get('limit_price', 0))
            kwargs['type'] = 'limit'
            if side == OrderSide.BUY:
                kwargs['order_class'] = OrderClass.BRACKET
                kwargs['take_profit'] = take_profit
                kwargs['stop_loss'] = stop_loss
            trading_client.submit_order(**kwargs)
        else:
            kwargs['type'] = 'market'
            trading_client.submit_order(**kwargs)
            # Cannot normally market bracket directly with basic func easily, would need nested objects, 
            # assuming limit logic or simple market order for MVP
            
        # Logging & Notify
        summary = f"{action} {qty} {ticker} | Conf: {decision.get('confidence')} | Reason: {decision.get('reasoning')}"
        write_log('decisions.log', summary)
        write_log('trades.log', f"{action} {ticker} | Qty: {qty} | SL: {decision.get('stop_loss')} | TP: {decision.get('take_profit')} | Conf: {decision.get('confidence')} | Thesis: {decision.get('reasoning')}")
        notify_telegram(decision)
        log.info(f"Executed: {summary}")
        
    except Exception as e:
        log.error(f"Alpaca Order failed: {e}")

def run_cycle():
    session = get_session()
    log.info(f"=== Start cycle | session: {session} ===")
    
    if session == 'CLOSED':
        log.info("Market closed. Sleeping 60s.")
        time.sleep(60)
        return False
        
    retries = 0
    from_date = None
    
    while retries < 3:
        sys_prompt, prompt = collect_context(session, from_date)
        decision = ask_claude(sys_prompt, prompt)
        
        if not decision:
            log.error("Failed to get valid decision from Claude.")
            return True # cycle ran, but failed
            
        action = decision.get('action')
        
        if action == 'NEED_MORE_CONTEXT':
            retries += 1
            from_date = decision.get('need_more_context', {}).get('from_date')
            reason = decision.get('need_more_context', {}).get('reason')
            log.info(f"Claude requested more context (attempt {retries}/2) from {from_date}: {reason}")
            if retries >= 3:
                log.warning("Max context retries reached. Forcing HOLD.")
                decision = {"action": "HOLD", "reasoning": "Forced hold due to max context retries exceeded."}
                execute_decision(decision)
                break
        else:
            execute_decision(decision)
            break
            
    return True

if __name__ == "__main__":
    while True:
        try:
            ran_cycle = run_cycle()
            if ran_cycle:
                # Sleep based on session. REGULAR = 1 hour, PRE/AFTER = wait out until next cycle
                session = get_session()
                if session == 'REGULAR':
                    log.info("=== End cycle | next in 1h ===")
                    time.sleep(3600)
                else:
                    log.info("=== End cycle | next in ~6h (extended session) ===")
                    time.sleep(21600)
        except Exception as e:
            log.error(f"Critical error in main loop: {e}")
            time.sleep(60)
