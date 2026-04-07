#!/usr/bin/env python3
"""
VALR Futures Grid Bot — Backtest Engine

Simulates grid trading strategy on historical data.
Optimizes for Sharpe ratio, total profit, and maximum drawdown.

Usage:
    python3 backtest.py --pair SOLUSDTPERP --days 90
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional
import random

# ──────────────────────────────────────────────
# Data Structures
# ──────────────────────────────────────────────

@dataclass
class Trade:
    timestamp: datetime
    type: str  # 'buy' or 'sell'
    price: float
    quantity: float
    pnl: float = 0.0
    fees: float = 0.0

@dataclass
class Position:
    side: str = ''  # 'long' or 'short'
    quantity: float = 0.0
    avg_entry: float = 0.0
    unrealized_pnl: float = 0.0

@dataclass
class BacktestResult:
    params: dict
    total_pnl: float = 0.0
    total_fees: float = 0.0
    total_funding: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    num_trades: int = 0
    equity_curve: List[float] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)

# ──────────────────────────────────────────────
# Market Data Generator (Synthetic for now)
# ──────────────────────────────────────────────

def generate_synthetic_data(days: int = 90, start_price: float = 100.0, 
                           volatility: float = 0.03, seed: int = 42) -> List[dict]:
    """
    Generate synthetic OHLCV data with realistic price action.
    
    Uses geometric Brownian motion with mean reversion.
    """
    random.seed(seed)
    
    data = []
    price = start_price
    current_time = datetime.now() - timedelta(days=days)
    
    # Generate 5-minute candles
    intervals_per_day = 288  # 24*60/5
    total_intervals = days * intervals_per_day
    
    # Parameters for mean reversion
    long_term_mean = start_price
    mean_reversion_speed = 0.001
    
    for i in range(total_intervals):
        # Mean-reverting component
        drift = mean_reversion_speed * (long_term_mean - price)
        
        # Random component (GBM)
        shock = random.gauss(0, volatility)
        
        # Price evolution
        price_change = price * (drift + shock)
        open_price = price
        close_price = price + price_change
        
        # Generate high/low
        high_price = max(open_price, close_price) * (1 + abs(random.gauss(0, volatility/2)))
        low_price = min(open_price, close_price) * (1 - abs(random.gauss(0, volatility/2)))
        
        # Volume (higher during volatility)
        volume = random.uniform(1000, 10000) * (1 + abs(shock) * 10)
        
        # Funding rate (random, mean zero)
        funding_rate = random.gauss(0, 0.0001)  # ~0.01% per 8 hours
        
        data.append({
            'timestamp': current_time,
            'open': open_price,
            'high': high_price,
            'low': low_price,
            'close': close_price,
            'volume': volume,
            'funding_rate': funding_rate
        })
        
        price = close_price
        current_time += timedelta(minutes=5)
    
    return data

# ──────────────────────────────────────────────
# Grid Bot Simulation
# ──────────────────────────────────────────────

class GridBotSimulator:
    def __init__(self, params: dict, initial_capital: float = 1000.0):
        self.params = params
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.position = Position()
        self.trades: List[Trade] = []
        self.equity_curve: List[float] = []
        self.grid_levels: Dict[str, List[float]] = {'buy': [], 'sell': []}
        self.stop_loss_price: Optional[float] = None
        self.fees_paid = 0.0
        self.funding_paid = 0.0
        
        # Extract params
        self.levels = params['levels']
        self.spacing_pct = params['spacing_pct'] / 100.0
        self.max_loss_pct = params['max_loss_pct'] / 100.0
        self.leverage = params['leverage']
        self.balance_usage = params['balance_usage_pct'] / 100.0
        
        # Fee rate (VALR futures maker fee is typically 0%)
        self.maker_fee = 0.0  # Post-only orders
        self.taker_fee = 0.0005  # 0.05% if needed
    
    def setup_grid(self, mid_price: float):
        """Initialize grid levels around mid price."""
        self.grid_levels = {'buy': [], 'sell': []}
        
        # Calculate quantity per level
        usable_capital = self.capital * self.balance_usage
        notional_per_level = usable_capital / self.levels
        leveraged_notional = notional_per_level * self.leverage
        quantity = leveraged_notional / mid_price
        
        self.quantity_per_order = quantity
        
        # Generate grid prices
        for i in range(1, self.levels + 1):
            buy_price = mid_price * (1 - self.spacing_pct * i)
            sell_price = mid_price * (1 + self.spacing_pct * i)
            self.grid_levels['buy'].append(buy_price)
            self.grid_levels['sell'].append(sell_price)
        
        self.mid_price = mid_price
        self.initial_mid = mid_price
    
    def update_stop_loss(self):
        """Update stop-loss based on position and max loss %."""
        if self.position.quantity == 0 or self.position.avg_entry == 0:
            self.stop_loss_price = None
            return
        
        if self.position.side == 'long':
            self.stop_loss_price = self.position.avg_entry * (1 - self.max_loss_pct)
        else:  # short
            self.stop_loss_price = self.position.avg_entry * (1 + self.max_loss_pct)
    
    def execute_buy(self, price: float, timestamp: datetime):
        """Execute a buy order."""
        quantity = self.quantity_per_order
        
        # Update position
        if self.position.side == 'short':
            # Closing short
            pnl = (self.position.avg_entry - price) * min(quantity, self.position.quantity)
            self.position.quantity -= quantity
            if self.position.quantity <= 0:
                self.position.side = ''
                self.position.quantity = 0
        else:
            # Opening/increasing long
            total_value = (self.position.quantity * self.position.avg_entry) + (quantity * price)
            self.position.quantity += quantity
            self.position.avg_entry = total_value / self.position.quantity if self.position.quantity > 0 else 0
            self.position.side = 'long'
            pnl = 0.0
        
        # Fees
        fee = quantity * price * self.maker_fee
        self.fees_paid += fee
        
        self.trades.append(Trade(
            timestamp=timestamp,
            type='buy',
            price=price,
            quantity=quantity,
            pnl=pnl,
            fees=fee
        ))
        
        self.update_stop_loss()
    
    def execute_sell(self, price: float, timestamp: datetime):
        """Execute a sell order."""
        quantity = self.quantity_per_order
        
        # Update position
        if self.position.side == 'long':
            # Closing long
            pnl = (price - self.position.avg_entry) * min(quantity, self.position.quantity)
            self.position.quantity -= quantity
            if self.position.quantity <= 0:
                self.position.side = ''
                self.position.quantity = 0
        else:
            # Opening/increasing short
            total_value = (self.position.quantity * self.position.avg_entry) + (quantity * price)
            self.position.quantity += quantity
            self.position.avg_entry = total_value / self.position.quantity if self.position.quantity > 0 else 0
            self.position.side = 'short'
            pnl = 0.0
        
        # Fees
        fee = quantity * price * self.maker_fee
        self.fees_paid += fee
        
        self.trades.append(Trade(
            timestamp=timestamp,
            type='sell',
            price=price,
            quantity=quantity,
            pnl=pnl,
            fees=fee
        ))
        
        self.update_stop_loss()
    
    def execute_stop_loss(self, price: float, timestamp: datetime):
        """Execute stop-loss, closing entire position."""
        if self.position.quantity == 0:
            return
        
        if self.position.side == 'long':
            pnl = (price - self.position.avg_entry) * self.position.quantity
        else:
            pnl = (self.position.avg_entry - price) * self.position.quantity
        
        # Fees (taker fee for stop-loss market order)
        fee = self.position.quantity * price * self.taker_fee
        self.fees_paid += fee
        
        self.trades.append(Trade(
            timestamp=timestamp,
            type=f'stop_loss_{self.position.side}',
            price=price,
            quantity=self.position.quantity,
            pnl=pnl,
            fees=fee
        ))
        
        self.position = Position()
        self.stop_loss_price = None
    
    def apply_funding(self, funding_rate: float, timestamp: datetime):
        """Apply funding payment."""
        if self.position.quantity == 0:
            return
        
        # Funding = position_size × mark_price × funding_rate
        # Long pays if rate positive, short receives
        # Short receives if rate positive, long pays
        
        notional = self.position.quantity * self.mid_price
        
        if self.position.side == 'long':
            funding_payment = -notional * funding_rate  # Long pays
        else:
            funding_payment = notional * funding_rate  # Short receives
        
        self.funding_paid += funding_payment
        self.capital += funding_payment
    
    def calculate_equity(self, current_price: float) -> float:
        """Calculate total equity including unrealized PnL."""
        unrealized = 0.0
        
        if self.position.quantity > 0:
            if self.position.side == 'long':
                unrealized = (current_price - self.position.avg_entry) * self.position.quantity
            else:
                unrealized = (self.position.avg_entry - current_price) * self.position.quantity
        
        return self.capital + unrealized
    
    def run(self, data: List[dict]) -> BacktestResult:
        """Run backtest on historical data."""
        if not data:
            return BacktestResult(params=self.params)
        
        # Initialize grid on first candle
        self.setup_grid(data[0]['close'])
        
        # Track active orders (which grid levels have open orders)
        active_buys = set(range(self.levels))  # All buy levels active
        active_sells = set(range(self.levels))  # All sell levels active
        
        funding_counter = 0
        
        for candle in data:
            timestamp = candle['timestamp']
            high = candle['high']
            low = candle['low']
            close = candle['close']
            funding_rate = candle.get('funding_rate', 0)
            
            self.mid_price = close
            
            # Check for grid fills (price crossing levels)
            # Check buy levels (price went below)
            for i in list(active_buys):
                buy_price = self.grid_levels['buy'][i]
                if low <= buy_price:
                    self.execute_buy(buy_price, timestamp)
                    active_buys.discard(i)
                    active_sells.add(i)  # Re-enable corresponding sell
            
            # Check sell levels (price went above)
            for i in list(active_sells):
                sell_price = self.grid_levels['sell'][i]
                if high >= sell_price:
                    self.execute_sell(sell_price, timestamp)
                    active_sells.discard(i)
                    active_buys.add(i)  # Re-enable corresponding buy
            
            # Check stop-loss
            if self.stop_loss_price:
                if self.position.side == 'long' and low <= self.stop_loss_price:
                    self.execute_stop_loss(self.stop_loss_price, timestamp)
                elif self.position.side == 'short' and high >= self.stop_loss_price:
                    self.execute_stop_loss(self.stop_loss_price, timestamp)
            
            # Apply funding every 288 candles (8 hours = 96 × 5-min candles... actually 288 for 24h)
            # VALR funding is every 8 hours
            funding_counter += 1
            if funding_counter >= 96:  # 96 × 5 min = 8 hours
                self.apply_funding(funding_rate, timestamp)
                funding_counter = 0
            
            # Record equity
            equity = self.calculate_equity(close)
            self.equity_curve.append(equity)
        
        # Close any remaining position at end
        if self.position.quantity > 0 and data:
            final_price = data[-1]['close']
            if self.position.side == 'long':
                pnl = (final_price - self.position.avg_entry) * self.position.quantity
            else:
                pnl = (self.position.avg_entry - final_price) * self.position.quantity
            self.trades.append(Trade(
                timestamp=data[-1]['timestamp'],
                type='close_final',
                price=final_price,
                quantity=self.position.quantity,
                pnl=pnl
            ))
            self.capital += pnl
        
        return self._compile_results()
    
    def _compile_results(self) -> BacktestResult:
        """Compile backtest results."""
        # Total PnL
        total_pnl = sum(t.pnl for t in self.trades)
        
        # Calculate metrics
        if len(self.equity_curve) < 2:
            return BacktestResult(params=self.params)
        
        # Daily returns for Sharpe
        daily_equity = []
        if self.equity_curve:
            current_day = None
            for eq in self.equity_curve:
                # Simplified: just sample every 288 candles
                if len(daily_equity) < len(self.equity_curve) // 288 + 1:
                    daily_equity.append(eq)
        
        if len(daily_equity) > 1:
            returns = [(daily_equity[i] - daily_equity[i-1]) / daily_equity[i-1] 
                      for i in range(1, len(daily_equity))]
            avg_return = sum(returns) / len(returns) if returns else 0
            std_return = (sum((r - avg_return)**2 for r in returns) / len(returns))**0.5 if returns else 0
            sharpe = (avg_return / std_return * (252**0.5)) if std_return > 0 else 0
        else:
            sharpe = 0
        
        # Max drawdown
        peak = self.equity_curve[0]
        max_dd = 0.0
        for equity in self.equity_curve:
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        
        # Win rate
        winning_trades = [t for t in self.trades if t.pnl > 0]
        losing_trades = [t for t in self.trades if t.pnl < 0]
        win_rate = len(winning_trades) / len(self.trades) if self.trades else 0
        
        # Profit factor
        gross_profit = sum(t.pnl for t in winning_trades)
        gross_loss = abs(sum(t.pnl for t in losing_trades))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        return BacktestResult(
            params=self.params,
            total_pnl=total_pnl + self.funding_paid,
            total_fees=self.fees_paid,
            total_funding=self.funding_paid,
            sharpe_ratio=sharpe,
            max_drawdown=max_dd,
            win_rate=win_rate,
            profit_factor=profit_factor,
            num_trades=len(self.trades),
            equity_curve=self.equity_curve,
            trades=self.trades
        )

# ──────────────────────────────────────────────
# Parameter Optimization
# ──────────────────────────────────────────────

def generate_param_grid() -> List[dict]:
    """Generate valid parameter combinations."""
    param_grid = []
    
    levels_options = [3, 5, 7, 10]
    spacing_options = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0]
    max_loss_options = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0]
    leverage_options = [3, 5, 10]
    balance_options = [70, 90]
    
    for levels in levels_options:
        for spacing in spacing_options:
            grid_range = levels * spacing
            for max_loss in max_loss_options:
                # CONSTRAINT: SL must exceed grid range
                if max_loss <= grid_range:
                    continue
                
                for leverage in leverage_options:
                    for balance in balance_options:
                        param_grid.append({
                            'levels': levels,
                            'spacing_pct': spacing,
                            'max_loss_pct': max_loss,
                            'leverage': leverage,
                            'balance_usage_pct': balance
                        })
    
    return param_grid


def run_optimization(data: List[dict], initial_capital: float = 1000.0) -> List[BacktestResult]:
    """Run backtest on all valid parameter combinations."""
    param_grid = generate_param_grid()
    results = []
    
    print(f"🔬 Running optimization on {len(param_grid)} parameter combinations...")
    print(f"   Data points: {len(data)}")
    print(f"   Initial capital: ${initial_capital:,.0f}")
    print()
    
    for i, params in enumerate(param_grid):
        if (i + 1) % 50 == 0:
            print(f"   Progress: {i+1}/{len(param_grid)}")
        
        bot = GridBotSimulator(params, initial_capital)
        result = bot.run(data)
        results.append(result)
    
    return results


def rank_results(results: List[BacktestResult]) -> dict:
    """Rank results by different objectives."""
    # Filter out invalid results
    valid = [r for r in results if r.total_pnl != 0 or r.num_trades > 0]
    
    if not valid:
        return {}
    
    # Rank by Sharpe
    by_sharpe = sorted(valid, key=lambda x: x.sharpe_ratio, reverse=True)[:10]
    
    # Rank by Profit
    by_profit = sorted(valid, key=lambda x: x.total_pnl, reverse=True)[:10]
    
    # Rank by Drawdown (ascending)
    by_drawdown = sorted(valid, key=lambda x: x.max_drawdown)[:10]
    
    # Composite score: Sharpe × 2 + Profit_norm - DD_norm
    max_profit = max(r.total_pnl for r in valid)
    max_dd = max(r.max_drawdown for r in valid)
    
    for r in valid:
        profit_norm = r.total_pnl / max_profit if max_profit > 0 else 0
        dd_norm = r.max_drawdown / max_dd if max_dd > 0 else 0
        r.composite_score = (r.sharpe_ratio * 2) + profit_norm - dd_norm
    
    by_composite = sorted(valid, key=lambda x: getattr(x, 'composite_score', 0), reverse=True)[:10]
    
    return {
        'by_sharpe': by_sharpe,
        'by_profit': by_profit,
        'by_drawdown': by_drawdown,
        'by_composite': by_composite
    }


def print_results(ranked: dict):
    """Print optimization results."""
    print("\n" + "="*80)
    print("📊 BACKTEST RESULTS")
    print("="*80)
    
    for category, results in ranked.items():
        print(f"\n{'─'*80}")
        print(f"TOP 10 BY {category.upper().replace('_', ' ')}")
        print(f"{'─'*80}")
        print(f"{'Rank':<5} {'Lvls':<5} {'Space':<7} {'SL%':<6} {'Lev':<5} {'Sharpe':<8} {'Profit':<12} {'Max DD':<10} {'Trades':<8}")
        print(f"{'─'*80}")
        
        for i, r in enumerate(results, 1):
            p = r.params
            print(f"{i:<5} {p['levels']:<5} {p['spacing_pct']:<7.2f}% {p['max_loss_pct']:<6.1f}% "
                  f"{p['leverage']:<5} {r.sharpe_ratio:<8.2f} ${r.total_pnl:<11,.0f} "
                  f"{r.max_drawdown*100:<9.1f}% {r.num_trades:<8}")
    
    # Risk profile recommendations
    print(f"\n{'='*80}")
    print("🎯 RISK PROFILE RECOMMENDATIONS")
    print(f"{'='*80}")
    
    if ranked.get('by_drawdown'):
        conservative = ranked['by_drawdown'][0]
        cp = conservative.params
        print(f"\n🟢 CONSERVATIVE (Lowest Drawdown)")
        print(f"   Config: {cp['levels']} levels, {cp['spacing_pct']:.2f}% spacing, {cp['max_loss_pct']:.1f}% SL, {cp['leverage']}x lev")
        print(f"   Expected: Sharpe {conservative.sharpe_ratio:.2f}, +${conservative.total_pnl:,.0f}, -{conservative.max_drawdown*100:.1f}% DD")
    
    if ranked.get('by_composite'):
        balanced = ranked['by_composite'][0]
        bp = balanced.params
        print(f"\n🟡 BALANCED (Best Risk-Adjusted)")
        print(f"   Config: {bp['levels']} levels, {bp['spacing_pct']:.2f}% spacing, {bp['max_loss_pct']:.1f}% SL, {bp['leverage']}x lev")
        print(f"   Expected: Sharpe {balanced.sharpe_ratio:.2f}, +${balanced.total_pnl:,.0f}, -{balanced.max_drawdown*100:.1f}% DD")
    
    if ranked.get('by_profit'):
        aggressive = ranked['by_profit'][0]
        ap = aggressive.params
        print(f"\n🔴 AGGRESSIVE (Max Profit)")
        print(f"   Config: {ap['levels']} levels, {ap['spacing_pct']:.2f}% spacing, {ap['max_loss_pct']:.1f}% SL, {ap['leverage']}x lev")
        print(f"   Expected: Sharpe {aggressive.sharpe_ratio:.2f}, +${aggressive.total_pnl:,.0f}, -{aggressive.max_drawdown*100:.1f}% DD")
    
    print(f"\n{'='*80}")


def export_configs(ranked: dict, output_path: str):
    """Export top configs to JSON files."""
    # Export by composite score
    if ranked.get('by_composite'):
        configs = []
        for i, r in enumerate(ranked['by_composite'][:5], 1):
            configs.append({
                'rank': i,
                'profile': 'balanced',
                'params': r.params,
                'metrics': {
                    'sharpe': round(r.sharpe_ratio, 2),
                    'total_pnl': round(r.total_pnl, 2),
                    'max_drawdown': round(r.max_drawdown, 4),
                    'win_rate': round(r.win_rate, 4),
                    'num_trades': r.num_trades
                }
            })
        
        with open(output_path, 'w') as f:
            json.dump(configs, f, indent=2)
        
        print(f"\n✅ Exported top 5 configs to: {output_path}")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='VALR Futures Grid Bot Backtester')
    parser.add_argument('--pair', default='SOLUSDTPERP', help='Trading pair')
    parser.add_argument('--days', type=int, default=90, help='Backtest period (days)')
    parser.add_argument('--capital', type=float, default=1000.0, help='Initial capital (USDT)')
    parser.add_argument('--volatility', type=float, default=0.03, help='Price volatility')
    parser.add_argument('--output', default='results/top_configs.json', help='Output file for configs')
    parser.add_argument('--seed', type=int, default=42, help='Random seed for reproducibility')
    
    args = parser.parse_args()
    
    print(f"🤖 VALR Futures Grid Bot — Backtest Engine")
    print(f"   Pair: {args.pair}")
    print(f"   Period: {args.days} days")
    print(f"   Capital: ${args.capital:,.0f}")
    print(f"   Volatility: {args.volatility*100:.1f}%")
    print()
    
    # Generate synthetic data
    print("📈 Generating synthetic market data...")
    data = generate_synthetic_data(
        days=args.days,
        start_price=100.0,
        volatility=args.volatility,
        seed=args.seed
    )
    print(f"   Generated {len(data)} candles ({len(data)/288:.1f} days)")
    
    # Run optimization
    results = run_optimization(data, args.capital)
    
    # Rank and display
    ranked = rank_results(results)
    print_results(ranked)
    
    # Export configs
    export_configs(ranked, args.output)
    
    print(f"\n✅ Backtest complete!")


if __name__ == '__main__':
    main()
