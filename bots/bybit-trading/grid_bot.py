#!/usr/bin/env python3
"""
Bybit Grid Bot v5 — Symmetric grid (VALR pattern).

Logic (exactly like VALR grid bot):
1. Place N buys + N sells symmetric around mid
2. Grid is naturally hedged: position + bids - asks = 0
3. When position opens, add native stop-loss
4. NEVER cancel orders unless completely flat
5. Only replenish when flat AND all orders gone

Reference: skills/valr-futures-grid-bot/SKILL.md
"""

import os, json, hmac, hashlib, time, requests, logging
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / 'config.json'
STATE_FILE = BASE_DIR / 'state.json'
LOG_FILE = BASE_DIR / 'logs' / 'grid-bot.log'

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

MAX_EXPOSURE_PCT = 0.8
SECRETS_CMD = "python3 /home/admin/.openclaw/secrets/secrets.py get"

def get_secret(name):
    return os.popen(f"{SECRETS_CMD} {name}").read().strip()

BASE_URL = 'https://api.bybit.com'
RECV_WINDOW = '5000'

def get_api_key():
    return get_secret('bybit_api_key')

def get_api_secret():
    return get_secret('bybit_api_secret')

def bybit_get(endpoint, params=''):
    api_key = get_api_key()
    api_secret = get_api_secret()
    timestamp = str(int(time.time() * 1000))
    param_str = timestamp + api_key + RECV_WINDOW + params
    signature = hmac.new(api_secret.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    
    headers = {
        'X-BAPI-API-KEY': api_key,
        'X-BAPI-TIMESTAMP': timestamp,
        'X-BAPI-SIGN': signature,
        'X-BAPI-RECV-WINDOW': RECV_WINDOW,
    }
    
    resp = requests.get(f'{BASE_URL}{endpoint}?{params}', headers=headers, timeout=10)
    return resp.json()

def bybit_post(endpoint, body):
    api_key = get_api_key()
    api_secret = get_api_secret()
    timestamp = str(int(time.time() * 1000))
    param_str = timestamp + api_key + RECV_WINDOW + json.dumps(body)
    signature = hmac.new(api_secret.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    
    headers = {
        'X-BAPI-API-KEY': api_key,
        'X-BAPI-TIMESTAMP': timestamp,
        'X-BAPI-SIGN': signature,
        'X-BAPI-RECV-WINDOW': RECV_WINDOW,
        'Content-Type': 'application/json',
    }
    
    resp = requests.post(f'{BASE_URL}{endpoint}', headers=headers, json=body, timeout=10)
    return resp.json()

_instrument_cache = {}

def get_instrument(symbol):
    if symbol in _instrument_cache:
        data, ts = _instrument_cache[symbol]
        if time.time() - ts < 7200:
            return data
    
    resp = requests.get(f'{BASE_URL}/v5/market/instruments-info?category=linear&symbol={symbol}', timeout=10)
    data = resp.json()
    if data.get('retCode') == 0 and data['result']['list']:
        inst = data['result']['list'][0]
        _instrument_cache[symbol] = (inst, time.time())
        return inst
    return None

def get_ticker(symbol):
    resp = requests.get(f'{BASE_URL}/v5/market/tickers?category=linear&symbol={symbol}', timeout=10)
    data = resp.json()
    if data.get('retCode') == 0 and data['result']['list']:
        return float(data['result']['list'][0]['markPrice'])
    return None

def get_position(symbol):
    params = f'category=linear&symbol={symbol}'
    data = bybit_get('/v5/position/list', params)
    if data.get('retCode') == 0 and data['result']['list']:
        return data['result']['list'][0]
    return None

def get_open_orders(symbol):
    params = f'category=linear&symbol={symbol}'
    data = bybit_get('/v5/order/realtime', params)
    if data.get('retCode') == 0:
        return data['result']['list']
    return []

def cancel_all_orders(symbol):
    body = {'category': 'linear', 'symbol': symbol}
    data = bybit_post('/v5/order/cancel-all', body)
    return data.get('retCode') == 0

def place_limit(symbol, side, qty, price, reduce_only=False):
    body = {
        'category': 'linear',
        'symbol': symbol,
        'side': side,
        'orderType': 'Limit',
        'qty': str(qty),
        'price': str(price),
        'timeInForce': 'GTC',
        'reduceOnly': reduce_only,
        'positionIdx': 0,
    }
    return bybit_post('/v5/order/create', body)

def set_stop_loss(symbol, side, stop_price):
    body = {
        'category': 'linear',
        'symbol': symbol,
        'side': side,
        'stopLoss': str(stop_price),
        'stopLossOrderType': 'Market',
    }
    return bybit_post('/v5/position/trading-stop', body)

def get_balance():
    data = bybit_get('/v5/account/wallet-balance', 'accountType=UNIFIED')
    if data.get('retCode') == 0:
        account = data['result']['list'][0]
        for coin in account.get('coin', []):
            if coin['coin'] == 'USDT':
                return {
                    'wallet': float(coin.get('walletBalance', 0) or 0),
                    'available': float(coin.get('availableToWithdraw', 0) or 0),
                    'equity': float(account.get('totalEquity', 0) or 0),
                    'margin': float(account.get('totalInitialMargin', 0) or 0),
                }
    return None

class GridBot:
    def __init__(self, config):
        self.config = config
        self.state = self.load_state()
        self.running = True
    
    def load_state(self):
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                return json.load(f)
        return {'grids': {}}
    
    def save_state(self):
        with open(STATE_FILE, 'w') as f:
            json.dump(self.state, f, indent=2)
    
    def calc_qty(self, symbol, price):
        pair_cfg = self.config['pairs'].get(symbol)
        if not pair_cfg:
            return 0
        
        inst = get_instrument(symbol)
        if not inst:
            return 0
        
        # Capital per level with leverage
        capital_per_level = self.config['capital_per_pair'] / pair_cfg['levels']
        notional = capital_per_level * self.config['leverage']
        qty = notional / price
        
        lot = float(inst['lotSizeFilter']['qtyStep'])
        qty = round(qty / lot) * lot
        qty = max(qty, float(inst['lotSizeFilter']['minOrderQty']))
        
        return round(qty, 6)
    
    def calc_grid_prices(self, symbol, mid):
        """Calculate symmetric buy/sell levels around mid."""
        pair_cfg = self.config['pairs'].get(symbol)
        if not pair_cfg:
            return [], []
        
        inst = get_instrument(symbol)
        if not inst:
            return [], []
        
        tick = float(inst['priceFilter']['tickSize'])
        spacing = pair_cfg['spacing_pct'] / 100
        
        buys = []
        sells = []
        
        for i in range(1, pair_cfg['levels'] + 1):
            buy = mid * (1 - spacing * i)
            sell = mid * (1 + spacing * i)
            
            buy = round(buy / tick) * tick
            sell = round(sell / tick) * tick
            
            buys.append(buy)
            sells.append(sell)
        
        return buys, sells
    
    def setup_grid(self, symbol):
        """Place symmetric buy + sell grid. DO NOT call this if position open!"""
        mid = get_ticker(symbol)
        if not mid:
            logger.error(f"Can't get price for {symbol}")
            return False
        
        # Cancel existing orders (only safe when flat with no position)
        cancel_all_orders(symbol)
        time.sleep(0.3)
        
        buys, sells = self.calc_grid_prices(symbol, mid)
        logger.info(f"Setting up {symbol} grid: mid=${mid:.2f}, {len(buys)} buys, {len(sells)} sells")
        
        # Place buy orders
        for price in buys:
            qty = self.calc_qty(symbol, price)
            if qty <= 0:
                continue
            
            result = place_limit(symbol, 'Buy', qty, price, reduce_only=False)
            if result.get('retCode') == 0:
                logger.info(f"  ✓ BUY {qty} @ ${price:.2f}")
            else:
                logger.error(f"  ✗ Buy failed: {result.get('retMsg')}")
            time.sleep(0.15)
        
        # Place sell orders
        for price in sells:
            qty = self.calc_qty(symbol, price)
            if qty <= 0:
                continue
            
            result = place_limit(symbol, 'Sell', qty, price, reduce_only=False)
            if result.get('retCode') == 0:
                logger.info(f"  ✓ SELL {qty} @ ${price:.2f}")
            else:
                logger.error(f"  ✗ Sell failed: {result.get('retMsg')}")
            time.sleep(0.15)
        
        self.state['grids'][symbol] = {
            'mid_price': mid,
            'buy_levels': buys,
            'sell_levels': sells,
            'created_at': datetime.now().isoformat()
        }
        self.save_state()
        
        return True
    
    def manage_position(self, symbol):
        """Ensure stop-loss is set on open position."""
        pos = get_position(symbol)
        if not pos or float(pos.get('size', 0)) == 0:
            return
        
        side = pos.get('side')
        entry = float(pos.get('avgPrice', 0))
        stop_loss_pct = self.config['pairs'].get(symbol, {}).get('stop_loss_pct', 3) / 100
        
        if side == 'Buy':
            stop = entry * (1 - stop_loss_pct)
        else:
            stop = entry * (1 + stop_loss_pct)
        
        inst = get_instrument(symbol)
        if inst:
            tick = float(inst['priceFilter']['tickSize'])
            stop = round(stop / tick) * tick
        
        result = set_stop_loss(symbol, side, stop)
        if result.get('retCode') == 0:
            logger.debug(f"🛡️  SL for {symbol}: ${stop:.2f}")
        else:
            logger.debug(f"SL update: {result.get('retMsg')}")
    
    def check_exposure(self, symbol):
        """Log net exposure (VALR health check pattern)."""
        pos = get_position(symbol)
        orders = get_open_orders(symbol)
        
        pos_qty = float(pos.get('size', 0)) if pos else 0
        if pos and pos.get('side') == 'Sell':
            pos_qty = -pos_qty
        
        buy_qty = sum(float(o['qty']) for o in orders if o['side'] == 'Buy')
        sell_qty = sum(float(o['qty']) for o in orders if o['side'] == 'Sell')
        
        net = pos_qty + buy_qty - sell_qty
        
        if abs(net) > 0.5:
            logger.warning(f"HEALTH CHECK {symbol}: net = {net:+.2f} (pos={pos_qty:+.1f}, bids={buy_qty:.1f}, asks={sell_qty:.1f})")
        
        return net
    
    def maybe_replenish(self, symbol):
        """Only replenish when FLAT and NO orders exist."""
        pos = get_position(symbol)
        has_position = pos and float(pos.get('size', 0)) > 0
        
        if has_position:
            return  # Have position — NEVER touch the grid
        
        orders = get_open_orders(symbol)
        if len(orders) == 0:
            # Completely flat with no orders → safe to replenish
            logger.info(f"Flat with no orders → replenishing {symbol}")
            self.setup_grid(symbol)
    
    def run_cycle(self, symbol):
        # Always ensure SL on any position
        self.manage_position(symbol)
        
        # Log exposure
        net = self.check_exposure(symbol)
        
        # Only replenish if completely flat
        self.maybe_replenish(symbol)
    
    def run(self):
        symbols = list(self.config['pairs'].keys())
        
        logger.info("Grid bot v5 starting (VALR symmetric pattern)...")
        logger.info(f"Config: {len(symbols)} pairs, {self.config['capital_per_pair']} USDT/pair, {self.config['leverage']}x leverage")
        
        # Initial setup
        for symbol in symbols:
            self.setup_grid(symbol)
            time.sleep(1)
        
        cycle = 0
        while self.running:
            cycle += 1
            logger.info(f"--- Cycle {cycle} ---")
            
            for symbol in symbols:
                self.run_cycle(symbol)
                time.sleep(0.3)
            
            bal = get_balance()
            if bal:
                pct = bal['margin'] / bal['equity'] * 100 if bal['equity'] > 0 else 0
                logger.info(f"Equity: ${bal['equity']:.2f} | Margin: ${bal['margin']:.2f} ({pct:.1f}%)")
            
            time.sleep(self.config['cycle_interval_seconds'])

def main():
    if not CONFIG_FILE.exists():
        logger.error(f"Config not found: {CONFIG_FILE}")
        return
    
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    
    bot = GridBot(config)
    
    try:
        bot.run()
    except KeyboardInterrupt:
        logger.info("Stopped by user")
        bot.running = False
    except Exception as e:
        logger.exception(f"Crashed: {e}")
        raise

if __name__ == '__main__':
    main()
