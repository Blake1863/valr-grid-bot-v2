#!/usr/bin/env python3
"""
VALR Grid Bot Backtester

Fetches historical OHLCV data from VALR and simulates grid trading strategies
to find optimal spacing and levels parameters.

Usage:
    python3 backtest.py --pair BTCUSDTPERP [--days 14] [--output results/]

Outputs:
    - JSON report with optimal parameters
    - CSV with all tested combinations

Symbol-agnostic: Works with any VALR futures pair (BTCUSDTPERP, SOLUSDTPERP, ETHUSDTPERP, etc.)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Tuple
import requests
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_HALF_UP

# Add parent directory to path for valr-futures-pnl skill
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "skills" / "valr-futures-pnl"))

VALR_BASE = "https://api.valr.com"

# Tick size mapping for common VALR perpetual futures
# Key: base asset, Value: tick size in quote currency (USDT)
# Source: VALR instrument specifications
TICK_SIZES = {
    "BTC": Decimal("1"),        # BTC moves in $1 increments
    "ETH": Decimal("0.1"),      # ETH moves in $0.10 increments
    "SOL": Decimal("0.01"),     # SOL moves in $0.01 increments
    "XRP": Decimal("0.0001"),   # XRP moves in $0.0001 increments
    "DOGE": Decimal("0.00001"), # DOGE moves in $0.00001 increments
    "ADA": Decimal("0.0001"),   # ADA moves in $0.0001 increments
    "AVAX": Decimal("0.01"),    # AVAX moves in $0.01 increments
    "DOT": Decimal("0.001"),    # DOT moves in $0.001 increments
    "MATIC": Decimal("0.0001"), # MATIC moves in $0.0001 increments
    "LINK": Decimal("0.001"),   # LINK moves in $0.001 increments
    "UNI": Decimal("0.001"),    # UNI moves in $0.001 increments
    "ATOM": Decimal("0.001"),   # ATOM moves in $0.001 increments
    "LTC": Decimal("0.01"),     # LTC moves in $0.01 increments
    "BCH": Decimal("0.01"),     # BCH moves in $0.01 increments
    "NEAR": Decimal("0.001"),   # NEAR moves in $0.001 increments
    "APT": Decimal("0.001"),    # APT moves in $0.001 increments
    "ARB": Decimal("0.0001"),   # ARB moves in $0.0001 increments
    "OP": Decimal("0.001"),     # OP moves in $0.001 increments
}

DEFAULT_TICK_SIZE = Decimal("0.01")  # Fallback for unknown symbols


def get_tick_size(pair: str) -> Decimal:
    """
    Get tick size for a given trading pair.
    
    Extracts base asset from pair name (e.g., "BTCUSDTPERP" -> "BTC")
    and returns the appropriate tick size.
    
    Args:
        pair: Trading pair symbol (e.g., "BTCUSDTPERP", "SOLUSDTPERP")
    
    Returns:
        Decimal tick size in quote currency
    """
    # Extract base asset (everything before "USD" or "USDT")
    base_asset = pair.replace("USDTPERP", "").replace("USDT", "").replace("USD", "")
    return TICK_SIZES.get(base_asset, DEFAULT_TICK_SIZE)


def round_to_tick(price: float, tick_size: Decimal) -> float:
    """
    Round a price to the nearest valid tick size.
    
    Args:
        price: Raw price value
        tick_size: Minimum price increment for the symbol
    
    Returns:
        Price rounded to valid tick
    """
    tick_float = float(tick_size)
    return round(price / tick_float) * tick_float


@dataclass
class Bucket:
    """OHLCV candle data"""
    timestamp: int  # epoch seconds
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass
class GridParams:
    """Grid configuration to test"""
    levels: int
    spacing_pct: float
    leverage: int
    balance_usage_pct: float
    stop_loss_pct: float  # Stop-loss % from entry


@dataclass
class BacktestResult:
    """Result of a single backtest run"""
    params: GridParams
    total_pnl: float
    max_drawdown: float
    total_fills: int
    win_rate: float
    avg_fill_pnl: float
    sharpe_ratio: float
    final_balance: float
    stopped_out: int = 0  # Count of stop-loss triggers


def fetch_buckets(pair: str, days: int = 14, period_seconds: int = 900) -> List[Bucket]:
    """
    Fetch OHLCV buckets from VALR with pagination and rate limiting.
    
    VALR allows max 300 buckets per call and 30 req/min on public endpoints.
    For 15-minute candles (900s):
    - 14 days = 1344 buckets (need 5 calls)
    - 7 days = 672 buckets (need 3 calls)
    
    Default: 15-minute candles over 14 days for better granularity.
    
    Rate limiting: Sleep between calls to stay under 30 req/min (2 sec/request).
    Includes retry logic with exponential backoff for 429 errors.
    """
    import time
    
    end_time = int(datetime.now(timezone.utc).timestamp())
    start_time = end_time - (days * 24 * 3600)
    
    url = f"{VALR_BASE}/v1/public/{pair}/buckets"
    
    print(f"Fetching {days} days of {period_seconds}s ({period_seconds//60}min) buckets for {pair}...")
    print(f"  Rate limit: 30 req/min (sleeping 2.1s between calls)")
    
    all_buckets = []
    batch_start = start_time
    batch_size = 300 * period_seconds  # Max 300 buckets per call
    request_count = 0
    max_retries = 3
    base_delay = 2.1  # seconds between requests (30 req/min = 1 per 2s)
    
    while batch_start < end_time:
        batch_end = min(batch_start + batch_size, end_time)
        params = {
            "periodSeconds": period_seconds,
            "startTime": int(batch_start),
            "endTime": int(batch_end),
            "includeEmpty": "false"
        }
        
        # Rate limiting: sleep before each request (except first)
        if request_count > 0:
            time.sleep(base_delay)
        
        # Retry logic with exponential backoff
        for attempt in range(max_retries):
            try:
                resp = requests.get(url, params=params, timeout=30)
                request_count += 1
                
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get('Retry-After', base_delay * (2 ** attempt)))
                    print(f"  Rate limited (429). Waiting {retry_after}s...")
                    time.sleep(retry_after)
                    continue
                
                resp.raise_for_status()
                data = resp.json()
                break  # Success, exit retry loop
                
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"  Request failed: {e}. Retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    raise  # Re-raise on final attempt
        
        for b in data:
            all_buckets.append(Bucket(
                timestamp=int(datetime.fromisoformat(b["startTime"].replace("Z", "+00:00")).timestamp()),
                open=Decimal(b["open"]),
                high=Decimal(b["high"]),
                low=Decimal(b["low"]),
                close=Decimal(b["close"]),
                volume=Decimal(b["volume"])
            ))
        
        print(f"  Batch: {batch_start} → {batch_end} ({len(data)} buckets)")
        batch_start = batch_end
    
    # Sort by timestamp ascending
    all_buckets.sort(key=lambda x: x.timestamp)
    print(f"  Total: {len(all_buckets)} buckets ({len(all_buckets)/24:.1f} days)")
    
    return all_buckets


def simulate_grid(buckets: List[Bucket], params: GridParams, tick_size: Decimal, initial_capital: float = 100.0) -> BacktestResult:
    """
    Simulate grid trading over historical data.
    
    Strategy:
    - At each candle, calculate grid levels around the open price
    - Track which levels would have been filled based on high/low
    - When price crosses a level, record a fill
    - Long fills are closed when price rises back above entry
    - Short fills are closed when price falls back below entry
    
    Simplified model:
    - Assume all fills happen at the grid price (not exact, but close enough for optimization)
    - Track open positions and close them when price reverses
    - Calculate PnL on each closed position
    
    Symbol-agnostic:
    - Grid levels are rounded to the symbol's tick size
    - Works with any price range (BTC ~$100k, SOL ~$100, XRP ~$0.50, etc.)
    
    Fees & Slippage:
    - Trading fee: 0.04% (0.0004) per fill on notional (VALR futures taker fee)
    - Slippage: 0.1% (0.001) on each fill
      - Long entries fill at price * 1.001 (worse)
      - Long exits fill at price * 0.999 (worse)
      - Short entries fill at price * 0.999 (worse)
      - Short exits fill at price * 1.001 (worse)
    - Fees apply to both entry AND exit
    
    Args:
        buckets: OHLCV candle data
        params: Grid configuration
        tick_size: Minimum price increment for the symbol
        initial_capital: Starting capital in USDT
    """
    
    # Fee and slippage constants
    TAKER_FEE = 0.0004  # 0.04% per fill
    SLIPPAGE = 0.001    # 0.1% (10 bps)
    
    levels = params.levels
    spacing = params.spacing_pct / 100.0
    stop_loss = params.stop_loss_pct / 100.0
    leverage = float(params.leverage)
    capital = initial_capital
    usage = params.balance_usage_pct / 100.0
    deployable = capital * usage * leverage
    
    # Grid state - store tuples of (entry_price, position_size)
    buy_levels: List[float] = []
    sell_levels: List[float] = []
    open_longs: List[Tuple[float, float]] = []  # (entry_price, position_size)
    open_shorts: List[Tuple[float, float]] = []
    
    total_pnl = 0.0
    total_fees = 0.0
    total_fills = 0
    winning_fills = 0
    stopped_out = 0  # Count stop-loss triggers
    
    # Track equity curve properly for drawdown calculation
    equity_curve = []  # List of (timestamp, total_equity)
    
    fill_pnls: List[float] = []
    
    for i, bucket in enumerate(buckets):
        mid = float(bucket.open)
        high = float(bucket.high)
        low = float(bucket.low)
        
        # Recalculate grid levels at each candle (re-centring model)
        # Round each level to valid tick size for the symbol
        buy_levels = []
        sell_levels = []
        for l in range(1, levels + 1):
            raw_buy = mid * (1.0 - spacing * l)
            raw_sell = mid * (1.0 + spacing * l)
            buy_levels.append(round_to_tick(raw_buy, tick_size))
            sell_levels.append(round_to_tick(raw_sell, tick_size))
        
        # Check if price touched any buy levels (long entries)
        for level in buy_levels:
            if low <= level:
                # Apply slippage: long entry fills at price * 1.001 (worse)
                fill_price = level * (1.0 + SLIPPAGE)
                position_size = deployable / float(max(len(buy_levels), 1))
                open_longs.append((fill_price, position_size))
                total_fills += 1
                # Fee on entry
                entry_fee = fill_price * position_size * TAKER_FEE
                total_pnl -= entry_fee
                total_fees += entry_fee
        
        # Check if price touched any sell levels (short entries)
        for level in sell_levels:
            if high >= level:
                # Apply slippage: short entry fills at price * 0.999 (worse)
                fill_price = level * (1.0 - SLIPPAGE)
                position_size = deployable / float(len(sell_levels) if sell_levels else 1)
                open_shorts.append((fill_price, position_size))
                total_fills += 1
                # Fee on entry
                entry_fee = fill_price * position_size * TAKER_FEE
                total_pnl -= entry_fee
                total_fees += entry_fee
        
        # Check stop-losses BEFORE closing profitable positions
        # Long stop-loss: price drops below entry * (1 - stop_loss)
        closed_longs_sl = []
        for entry_price, position_size in open_longs:
            stop_price = entry_price * (1.0 - stop_loss)
            if low <= stop_price:
                # Stop-loss triggered - apply slippage on exit: long exits at price * 0.999
                exit_price = stop_price * (1.0 - SLIPPAGE)
                pnl = (exit_price - entry_price) * position_size
                # Fee on exit
                exit_fee = exit_price * position_size * TAKER_FEE
                pnl -= exit_fee
                total_pnl += pnl
                total_fees += exit_fee
                fill_pnls.append(pnl)
                stopped_out += 1
                closed_longs_sl.append((entry_price, position_size))
        for item in closed_longs_sl:
            open_longs.remove(item)
        
        # Short stop-loss: price rises above entry * (1 + stop_loss)
        closed_shorts_sl = []
        for entry_price, position_size in open_shorts:
            stop_price = entry_price * (1.0 + stop_loss)
            if high >= stop_price:
                # Stop-loss triggered - apply slippage on exit: short exits at price * 1.001
                exit_price = stop_price * (1.0 + SLIPPAGE)
                pnl = (entry_price - exit_price) * position_size
                # Fee on exit
                exit_fee = exit_price * position_size * TAKER_FEE
                pnl -= exit_fee
                total_pnl += pnl
                total_fees += exit_fee
                fill_pnls.append(pnl)
                stopped_out += 1
                closed_shorts_sl.append((entry_price, position_size))
        for item in closed_shorts_sl:
            open_shorts.remove(item)
        
        # Close longs when price rises above entry (profitable)
        closed_longs = []
        for entry_price, position_size in open_longs:
            if high > entry_price:
                # Apply slippage: long exits at price * 0.999 (worse)
                exit_price = high * (1.0 - SLIPPAGE)
                pnl = (exit_price - entry_price) * position_size
                # Fee on exit
                exit_fee = exit_price * position_size * TAKER_FEE
                pnl -= exit_fee
                total_pnl += pnl
                total_fees += exit_fee
                fill_pnls.append(pnl)
                if pnl > 0:
                    winning_fills += 1
                closed_longs.append((entry_price, position_size))
        for item in closed_longs:
            open_longs.remove(item)
        
        # Close shorts when price falls below entry
        closed_shorts = []
        for entry_price, position_size in open_shorts:
            if float(bucket.low) < entry_price:
                # Position profitable - apply slippage: short exits at price * 1.001 (worse)
                exit_price = float(bucket.low) * (1.0 + SLIPPAGE)
                pnl = (entry_price - exit_price) * position_size
                # Fee on exit
                exit_fee = exit_price * position_size * TAKER_FEE
                pnl -= exit_fee
                total_pnl += pnl
                total_fees += exit_fee
                fill_pnls.append(pnl)
                if pnl > 0:
                    winning_fills += 1
                closed_shorts.append((entry_price, position_size))
        for item in closed_shorts:
            open_shorts.remove(item)
        
        # Calculate unrealized PnL on open positions (mark-to-market)
        unrealized_pnl = 0.0
        
        # Long positions: (current_price - entry_price) * position_size
        for entry_price, position_size in open_longs:
            unrealized_pnl += (mid - entry_price) * position_size
        
        # Short positions: (entry_price - current_price) * position_size
        for entry_price, position_size in open_shorts:
            unrealized_pnl += (entry_price - mid) * position_size
        
        # Total equity = capital + realized PnL + unrealized PnL
        total_equity = capital + total_pnl + unrealized_pnl
        
        # Track equity curve for proper drawdown calculation
        equity_curve.append((i, total_equity))
    
    # Calculate max drawdown properly from equity curve
    # Peak = highest equity point reached
    # Trough = lowest equity point AFTER that peak
    # Drawdown = (Peak - Trough) / Peak
    max_drawdown = 0.0
    peak_equity = equity_curve[0][1] if equity_curve else capital
    
    for timestamp, equity in equity_curve:
        if equity > peak_equity:
            peak_equity = equity
        else:
            # This is a trough after the peak
            dd = (peak_equity - equity) / peak_equity
            if dd > max_drawdown:
                max_drawdown = dd
    
    # Cap at 100% (total loss) - max_drawdown is a ratio, so cap at 1.0
    max_drawdown = min(max_drawdown, 1.0) * 100.0  # Convert to percentage
    win_rate = winning_fills / total_fills * 100.0 if total_fills > 0 else 0.0
    avg_fill_pnl = total_pnl / total_fills if total_fills > 0 else 0.0
    
    # Sharpe ratio (simplified - daily returns)
    if len(fill_pnls) > 1:
        import statistics
        returns = fill_pnls
        if statistics.mean(returns) != 0 and statistics.stdev(returns) != 0:
            sharpe = statistics.mean(returns) / statistics.stdev(returns)
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0
    
    return BacktestResult(
        params=params,
        total_pnl=total_pnl,
        max_drawdown=max_drawdown,
        total_fills=total_fills,
        win_rate=win_rate,
        avg_fill_pnl=avg_fill_pnl,
        sharpe_ratio=sharpe,
        final_balance=capital + total_pnl
    )


def run_backtest(pair: str, days: int = 14) -> Dict[str, Any]:
    """Run full backtest with parameter sweep"""
    
    # Get tick size for the symbol
    tick_size = get_tick_size(pair)
    
    # Fetch data
    buckets = fetch_buckets(pair, days)
    
    if len(buckets) < 2:
        print("ERROR: Not enough data for backtest")
        return {}
    
    # Parameter ranges to test
    level_options = [3, 4, 5, 6, 8, 10, 12]
    spacing_options = [0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1.0]
    stop_loss_options = [2.0, 3.0, 5.0, 7.5, 10.0, 15.0, 20.0, 25.0]  # Stop-loss % from entry
    leverage = 5  # Fixed for now
    balance_usage = 90.0
    
    total_combos = len(level_options) * len(spacing_options) * len(stop_loss_options)
    print(f"\nTesting {total_combos} combinations:")
    print(f"  {len(level_options)} levels × {len(spacing_options)} spacings × {len(stop_loss_options)} stop-loss %")
    print(f"Pair: {pair} | Tick size: ${tick_size} | Days: {days} | Leverage: {leverage}x\n")
    
    results: List[BacktestResult] = []
    combo = 0
    
    for levels in level_options:
        for spacing in spacing_options:
            for stop_loss in stop_loss_options:
                combo += 1
                params = GridParams(
                    levels=levels,
                    spacing_pct=spacing,
                    leverage=leverage,
                    balance_usage_pct=balance_usage,
                    stop_loss_pct=stop_loss
                )
                result = simulate_grid(buckets, params, tick_size)
                results.append(result)
                if combo % 20 == 0 or combo == total_combos:
                    print(f"  [{combo}/{total_combos}] L={levels:2d} | spacing={spacing:.2f}% | stop={stop_loss:5.1f}% | PnL=${result.total_pnl:>8.2f} | DD={result.max_drawdown:>5.1f}% | fills={result.total_fills:3d}")
    
    # Find optimal by Sharpe ratio (risk-adjusted returns)
    results.sort(key=lambda r: r.sharpe_ratio, reverse=True)
    best_by_sharpe = results[0]
    
    # Also find best by raw PnL
    results_by_pnl = sorted(results, key=lambda r: r.total_pnl, reverse=True)
    best_by_pnl = results_by_pnl[0]
    
    # Find best by PnL/dd ratio (profit per unit of drawdown)
    results_by_efficiency = sorted(
        results, 
        key=lambda r: r.total_pnl / r.max_drawdown if r.max_drawdown > 0 else 0.0,
        reverse=True
    )
    best_efficiency = results_by_efficiency[0]
    
    report = {
        "pair": pair,
        "backtest_date": datetime.now(timezone.utc).isoformat(),
        "data_range": {
            "start": datetime.fromtimestamp(buckets[0].timestamp, tz=timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(buckets[-1].timestamp, tz=timezone.utc).isoformat(),
            "days": days,
            "candles": len(buckets)
        },
        "best_by_sharpe": {
            "params": asdict(best_by_sharpe.params),
            "metrics": {
                "total_pnl": round(best_by_sharpe.total_pnl, 2),
                "max_drawdown_pct": round(best_by_sharpe.max_drawdown, 2),
                "total_fills": best_by_sharpe.total_fills,
                "win_rate_pct": round(best_by_sharpe.win_rate, 2),
                "avg_fill_pnl": round(best_by_sharpe.avg_fill_pnl, 4),
                "sharpe_ratio": round(best_by_sharpe.sharpe_ratio, 4),
                "final_balance": round(best_by_sharpe.final_balance, 2)
            }
        },
        "best_by_pnl": {
            "params": asdict(best_by_pnl.params),
            "metrics": {
                "total_pnl": round(best_by_pnl.total_pnl, 2),
                "max_drawdown_pct": round(best_by_pnl.max_drawdown, 2),
                "total_fills": best_by_pnl.total_fills,
                "win_rate_pct": round(best_by_pnl.win_rate, 2),
                "avg_fill_pnl": round(best_by_pnl.avg_fill_pnl, 4),
                "sharpe_ratio": round(best_by_pnl.sharpe_ratio, 4),
                "final_balance": round(best_by_pnl.final_balance, 2)
            }
        },
        "best_efficiency": {
            "params": asdict(best_efficiency.params),
            "metrics": {
                "total_pnl": round(best_efficiency.total_pnl, 2),
                "max_drawdown_pct": round(best_efficiency.max_drawdown, 2),
                "total_fills": best_efficiency.total_fills,
                "win_rate_pct": round(best_efficiency.win_rate, 2),
                "avg_fill_pnl": round(best_efficiency.avg_fill_pnl, 4),
                "sharpe_ratio": round(best_efficiency.sharpe_ratio, 4),
                "final_balance": round(best_efficiency.final_balance, 2)
            }
        },
        "all_results": [
            {
                "params": asdict(r.params),
                "total_pnl": round(r.total_pnl, 2),
                "max_drawdown_pct": round(r.max_drawdown, 2),
                "total_fills": r.total_fills,
                "sharpe_ratio": round(r.sharpe_ratio, 4)
            }
            for r in results
        ]
    }
    
    return report


def main():
    parser = argparse.ArgumentParser(description="VALR Grid Bot Backtester")
    parser.add_argument("--pair", default="SOLUSDTPERP", help="Currency pair (default: SOLUSDTPERP)")
    parser.add_argument("--days", type=int, default=14, help="Number of days to backtest (default: 14)")
    parser.add_argument("--output", default="results", help="Output directory (default: results)")
    args = parser.parse_args()
    
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Run backtest
    report = run_backtest(args.pair, args.days)
    
    if not report:
        print("Backtest failed")
        sys.exit(1)
    
    # Save JSON report
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"{args.pair}_{timestamp}.json"
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)
    
    # Save CSV of all results
    csv_path = output_dir / f"{args.pair}_{timestamp}.csv"
    with open(csv_path, "w") as f:
        f.write("levels,spacing_pct,total_pnl,max_drawdown_pct,total_fills,sharpe_ratio\n")
        for r in report["all_results"]:
            f.write(f"{r['params']['levels']},{r['params']['spacing_pct']},{r['total_pnl']},{r['max_drawdown_pct']},{r['total_fills']},{r['sharpe_ratio']}\n")
    
    # Print summary
    print("\n" + "="*60)
    print("BACKTEST COMPLETE")
    print("="*60)
    print(f"Pair: {report['pair']}")
    print(f"Period: {report['data_range']['start'][:10]} to {report['data_range']['end'][:10]} ({report['data_range']['days']} days)")
    print()
    print("🏆 BEST BY SHARPE (risk-adjusted):")
    b = report["best_by_sharpe"]
    print(f"   Levels: {b['params']['levels']} | Spacing: {b['params']['spacing_pct']}%")
    print(f"   PnL: ${b['metrics']['total_pnl']} | DD: {b['metrics']['max_drawdown_pct']}% | Sharpe: {b['metrics']['sharpe_ratio']}")
    print()
    print("💰 BEST BY RAW PnL:")
    b = report["best_by_pnl"]
    print(f"   Levels: {b['params']['levels']} | Spacing: {b['params']['spacing_pct']}%")
    print(f"   PnL: ${b['metrics']['total_pnl']} | DD: {b['metrics']['max_drawdown_pct']}% | Sharpe: {b['metrics']['sharpe_ratio']}")
    print()
    print("⚡ BEST EFFICIENCY (PnL/DD ratio):")
    b = report["best_efficiency"]
    print(f"   Levels: {b['params']['levels']} | Spacing: {b['params']['spacing_pct']}%")
    print(f"   PnL: ${b['metrics']['total_pnl']} | DD: {b['metrics']['max_drawdown_pct']}% | Ratio: {b['metrics']['total_pnl'] / b['metrics']['max_drawdown_pct'] if b['metrics']['max_drawdown_pct'] > 0 else 0:.2f}")
    print()
    print(f"Results saved to: {json_path}")
    print(f"CSV saved to: {csv_path}")


if __name__ == "__main__":
    main()
