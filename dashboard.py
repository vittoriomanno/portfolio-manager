import os
import json
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv()

ALPACA_DATA_KEY = os.getenv('ALPACA_DATA_KEY', os.getenv('ALPACA_KEY'))
ALPACA_DATA_SECRET = os.getenv('ALPACA_DATA_SECRET', os.getenv('ALPACA_SECRET'))
ALPACA_PAPER = os.getenv('ALPACA_PAPER', 'true').lower() == 'true'

app = Flask(__name__)

# Fallback fake data if missing keys locally
dummy_mode = not ALPACA_DATA_KEY
if not dummy_mode:
    try:
        trading_client = TradingClient(ALPACA_DATA_KEY, ALPACA_DATA_SECRET, paper=ALPACA_PAPER)
    except Exception as e:
        print(f"Failed to load alpaca data API: {e}")
        dummy_mode = True


@app.after_request
def add_cache_headers(response):
    """Prevent browser/proxy caching on all API responses."""
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


def tail_lines(filepath, max_lines=200, filter_fn=None):
    """Read last max_lines from a file efficiently using seek from end.

    Optional filter_fn applied per line. Returns list of strings.
    """
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, 'rb') as f:
            f.seek(0, 2)  # seek to end
            file_size = f.tell()
            if file_size == 0:
                return []

            # Read in chunks from the end
            chunk_size = 8192
            lines = []
            remaining = b''
            pos = file_size

            while pos > 0 and len(lines) < max_lines * 3:  # over-read to account for filtering
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size) + remaining
                split = chunk.split(b'\n')
                remaining = split[0]
                for raw_line in reversed(split[1:]):
                    try:
                        line = raw_line.decode('utf-8', errors='replace')
                    except Exception:
                        continue
                    if filter_fn and not filter_fn(line):
                        continue
                    lines.append(line)
                    if len(lines) >= max_lines:
                        break
                if len(lines) >= max_lines:
                    break

            # Handle the very first line of the file (leftover in remaining)
            if len(lines) < max_lines and remaining:
                try:
                    line = remaining.decode('utf-8', errors='replace')
                    if not filter_fn or filter_fn(line):
                        lines.append(line)
                except Exception:
                    pass

            lines.reverse()
            return lines
    except Exception:
        return []


def tail_log_entries(filepath, max_entries=200):
    """Read last max_entries pipe-delimited log entries from a file efficiently."""
    lines = tail_lines(filepath, max_lines=max_entries, filter_fn=lambda l: '|' in l)
    entries = []
    for line in lines:
        parts = line.split('|', 1)
        if len(parts) == 2:
            entries.append((parts[0].strip(), parts[1].strip()))
    return entries


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/portfolio')
def portfolio():
    if dummy_mode:
        return jsonify({
            "equity": 3615.86,
            "cash": 293.0187,
            "buying_power": 9853.00,
            "daytrade_count": 0,
            "positions": [
                {"symbol": "ETH", "qty": 12.695, "entry": 2500, "current": 3615.86, "pl_pct": 3.27, "pl_usd": 14164.71}
            ],
            "orders": []
        })
    try:
        account = trading_client.get_account()
        positions = trading_client.get_all_positions()

        pos_list = []
        for p in positions:
            pos_list.append({
                "symbol": p.symbol,
                "qty": float(p.qty),
                "entry": float(p.avg_entry_price),
                "current": float(p.current_price),
                "pl_pct": float(p.unrealized_plpc) * 100,
                "pl_usd": float(p.unrealized_pl)
            })

        return jsonify({
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "daytrade_count": account.daytrade_count,
            "positions": pos_list,
            "orders": []
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/log')
def logs():
    decisions = tail_log_entries('decisions.log', max_entries=200)
    trades = tail_log_entries('trades.log', max_entries=200)

    data = []
    for time_str, msg in decisions:
        data.append({"type": "decision", "time": time_str, "msg": msg})
    for time_str, msg in trades:
        data.append({"type": "trade", "time": time_str, "msg": msg})

    # Sort by time desc
    data.sort(key=lambda x: x['time'], reverse=True)
    return jsonify(data[:200])

@app.route('/api/applog')
def applog():
    def is_info_or_above(line):
        """Filter out DEBUG lines (which contain full Claude prompts)."""
        if not line.strip():
            return False
        # Standard logging format: "2025-01-15 08:00:00 - portfolio_manager - DEBUG - ..."
        # Accept lines that contain INFO, WARNING, ERROR, CRITICAL; reject DEBUG
        if ' - DEBUG - ' in line:
            return False
        return True

    lines = tail_lines('app.log', max_lines=200, filter_fn=is_info_or_above)
    return jsonify({"logs": lines})

if __name__ == '__main__':
    port = int(os.getenv('DASHBOARD_PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
