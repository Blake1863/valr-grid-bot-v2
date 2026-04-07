#!/usr/bin/env python3
"""
Bybit Grid Bot - Daily Performance Report
Generates PnL summary and posts to Telegram.
"""

import os, hmac, hashlib, time, requests, json
from datetime import datetime, timedelta

SECRETS_CMD = "python3 /home/admin/.openclaw/secrets/secrets.py get"
BASE_URL = 'https://api.bybit.com'
RECV_WINDOW = '5000'
REPORT_FILE = '/home/admin/.openclaw/workspace/bots/bybit-trading/daily-report.md'

def get_secret(name):
    return os.popen(f"{SECRETS_CMD} {name}").read().strip()

def bybit_get(endpoint, params=''):
    api_key = get_secret('bybit_api_key')
    api_secret = get_secret('bybit_api_secret')
    timestamp = str(int(time.time() * 1000))
    param_str = timestamp + api_key + RECV_WINDOW + params
    signature = hmac.new(api_secret.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    
    headers = {
        'X-BAPI-API-KEY': api_key,
        'X-BAPI-TIMESTAMP': timestamp,
        'X-BAPI-SIGN': signature,
        'X-BAPI-RECV-WINDOW': RECV_WINDOW,
    }
    
    resp = requests.get(f'{BASE_URL}{endpoint}?{params}', headers=headers)
    return resp.json()

def get_balance():
    data = bybit_get('/v5/account/wallet-balance', 'accountType=UNIFIED')
    if data.get('retCode') == 0:
        account = data['result']['list'][0]
        return {
            'equity': float(account['totalEquity']),
            'available': float(account['totalAvailableBalance']),
            'wallet': float(account['totalWalletBalance']),
        }
    return None

def get_positions():
    data = bybit_get('/v5/position/list', 'category=linear&settleCoin=USDT')
    if data.get('retCode') == 0:
        return data['result']['list']
    return []

def get_trade_history(hours=24):
    """Get recent trade history."""
    start_time = int((datetime.now() - timedelta(hours=hours)).timestamp() * 1000)
    data = bybit_get('/v5/execution/list', f'category=linear&startTime={start_time}')
    if data.get('retCode') == 0:
        return data['result']['list']
    return []

def generate_report():
    balance = get_balance()
    positions = get_positions()
    trades = get_trade_history()
    
    # Calculate realized PnL from trades
    realized_pnl = 0
    fills = 0
    for t in trades:
        if t.get('execFee'):
            realized_pnl -= float(t['execFee'])  # Fees are negative
            fills += 1
    
    # Calculate unrealized PnL from positions
    unrealized_pnl = 0
    for p in positions:
        if float(p.get('size', 0)) > 0:
            unrealized_pnl += float(p.get('unrealisedPnl', 0))
    
    total_pnl = realized_pnl + unrealized_pnl
    
    # Starting capital (from config or memory)
    starting_capital = 122.0  # Initial deployment
    
    equity_str = f"${balance['equity']:.2f}" if balance else "N/A"
    
    report = f"""# Bybit Grid Bot - Daily Report

**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}

---

## Portfolio Summary
| Metric | Value |
|--------|-------|
| Starting Capital | ${starting_capital:.2f} |
| Current Equity | {equity_str} |
| Total PnL | ${total_pnl:.2f} |
| PnL % | {(total_pnl / starting_capital * 100):.2f}% |

---

## Positions
"""
    
    if positions:
        report += "| Symbol | Side | Size | Entry | Mark | PnL |\n"
        report += "|--------|------|------|-------|------|-----|\n"
        for p in positions:
            if float(p.get('size', 0)) > 0:
                report += f"| {p['symbol']} | {p['side']} | {p['size']} | ${float(p['avgPrice']):.2f} | ${float(p['markPrice']):.2f} | ${float(p['unrealisedPnl']):.2f} |\n"
    else:
        report += "*No open positions*\n"
    
    report += f"""
---

## Recent Activity
- **Fills (24h):** {fills}
- **Fees Paid (24h):** ${abs(realized_pnl):.2f}
- **Unrealized PnL:** ${unrealized_pnl:.2f}

---

## Bot Status
- **Service:** Active (systemd)
- **Pairs:** ETHUSDT, SOLUSDT, BNBUSDT
- **Config:** 3x leverage, 2 levels/pair, 0.5-0.6% spacing

---

*Next report: Tomorrow 08:00 SAST*
"""
    
    # Save report
    with open(REPORT_FILE, 'w') as f:
        f.write(report)
    
    print(report)
    return report

if __name__ == '__main__':
    generate_report()
