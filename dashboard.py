import os
import json
from flask import Flask, jsonify, render_template
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
    data = []
    # Read decisions
    if os.path.exists('decisions.log'):
        with open('decisions.log', 'r', encoding='utf-8') as f:
            for line in f:
                if '|' in line:
                    parts = line.split('|', 1)
                    data.append({"type": "decision", "time": parts[0].strip(), "msg": parts[1].strip()})
    
    # Read trades
    if os.path.exists('trades.log'):
        with open('trades.log', 'r', encoding='utf-8') as f:
            for line in f:
                if '|' in line:
                    parts = line.split('|', 1)
                    data.append({"type": "trade", "time": parts[0].strip(), "msg": parts[1].strip()})
                    
    # Sort by time desc
    data.sort(key=lambda x: x['time'], reverse=True)
    return jsonify(data)

@app.route('/api/applog')
def applog():
    lines = []
    if os.path.exists('app.log'):
        with open('app.log', 'r', encoding='utf-8') as f:
            lines = f.readlines()[-50:] # last 50 lines
    return jsonify({"logs": lines})

if __name__ == '__main__':
    port = int(os.getenv('DASHBOARD_PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=True)
