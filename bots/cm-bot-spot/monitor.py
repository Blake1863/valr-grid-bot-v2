#!/usr/bin/env python3
"""
Spot Bot Monitor — watches cm-bot-spot logs and triggers replenishment on failures.

Triggers when:
- 3+ consecutive maker order failures for an account (Insufficient Balance)
- Runs quote replenishment + base rebalancing

Usage: python3 monitor.py
"""

import subprocess
import sys
import time
import os
from datetime import datetime

# Configuration
LOG_FILE = "/home/admin/.openclaw/workspace/bots/cm-bot-spot/logs/cm-bot-spot.log"
FAILURE_THRESHOLD = 3
REPLENISH_SCRIPT = "/home/admin/.openclaw/workspace/scripts/quote_replenish.py"
REBALANCE_SCRIPT = "/home/admin/.openclaw/workspace/scripts/spot_rebalance_manual.py"

# Track failure counts per (account, pair)
failure_counts = {}  # Key: "CMS1:PAIR", Value: count
last_failure_time = {}

def log(msg):
    """Print timestamped log message."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")
    sys.stdout.flush()

def parse_failure(line):
    """Check if line contains an Insufficient Balance failure and extract account + pair."""
    if "Insufficient Balance" not in line:
        return None
    
    # Extract account
    account = None
    if "[CMS1]" in line:
        account = "CMS1"
    elif "[CMS2]" in line:
        account = "CMS2"
    
    if not account:
        return None
    
    # Extract pair from currencyPair field
    pair = None
    if 'currencyPair":"' in line:
        try:
            start = line.find('currencyPair":"') + len('currencyPair":"')
            end = line.find('"', start)
            pair = line[start:end]
        except:
            pass
    
    return (account, pair)

def run_replenishment():
    """Run the quote replenishment script."""
    log("🔄 Running quote replenishment...")
    try:
        result = subprocess.run(
            ["python3", REPLENISH_SCRIPT],
            capture_output=True,
            text=True,
            timeout=120
        )
        log(f"Replenishment output:\n{result.stdout}")
        if result.stderr:
            log(f"Replenishment errors:\n{result.stderr}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log("❌ Replenishment timed out")
        return False
    except Exception as e:
        log(f"❌ Replenishment failed: {e}")
        return False

def run_rebalance():
    """Run the base asset rebalancing script."""
    log("🔄 Running base asset rebalancing...")
    try:
        result = subprocess.run(
            ["python3", REBALANCE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=120
        )
        log(f"Rebalance output:\n{result.stdout}")
        if result.stderr:
            log(f"Rebalance errors:\n{result.stderr}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log("❌ Rebalance timed out")
        return False
    except Exception as e:
        log(f"❌ Rebalance failed: {e}")
        return False

def tail_log():
    """Tail the log file and process new lines."""
    log(f"👁️  Monitoring {LOG_FILE}")
    log(f"   Failure threshold: {FAILURE_THRESHOLD}")
    log(f"   Replenish script: {REPLENISH_SCRIPT}")
    log(f"   Rebalance script: {REBALANCE_SCRIPT}")
    
    # Check if log file exists
    if not os.path.exists(LOG_FILE):
        log(f"❌ Log file not found: {LOG_FILE}")
        sys.exit(1)
    
    # Start tailing
    process = subprocess.Popen(
        ["tail", "-F", LOG_FILE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    log("✅ Monitoring started...")
    
    try:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            
            # Check for failures
            result = parse_failure(line)
            if result:
                account, pair = result
                key = f"{account}:{pair}" if pair else account
                
                failure_counts[key] = failure_counts.get(key, 0) + 1
                last_failure_time[key] = time.time()
                
                pair_str = f" ({pair})" if pair else ""
                log(f"⚠️  {account}{pair_str} failure #{failure_counts[key]}: Insufficient Balance")
                
                # Check if threshold reached for this specific pair
                if failure_counts[key] >= FAILURE_THRESHOLD:
                    log(f"🚨 {account}{pair_str} reached {FAILURE_THRESHOLD} failures - triggering replenishment + rebalance!")
                    
                    # Run replenishment first (sells base assets for quote)
                    run_replenishment()
                    
                    # Then rebalance (redistributes base assets across accounts)
                    run_rebalance()
                    
                    # Reset failure counter for this pair
                    failure_counts[key] = 0
                    log(f"✅ {account}{pair_str} replenishment complete - counter reset")
            
            # Check for successful orders (reset counter for that pair)
            elif "✅ CMS1 Maker:" in line or "✅ CMS2 Maker:" in line:
                account = "CMS1" if "CMS1" in line else "CMS2"
                # Extract pair if possible
                pair = None
                if 'currencyPair":"' in line:
                    try:
                        start = line.find('currencyPair":"') + len('currencyPair":"')
                        end = line.find('"', start)
                        pair = line[start:end]
                    except:
                        pass
                key = f"{account}:{pair}" if pair else account
                if failure_counts.get(key, 0) > 0:
                    log(f"✅ {account} successful maker for {pair or 'unknown'} - resetting counter")
                    failure_counts[key] = 0
            
            # Also reset on taker success
            elif "✅ CMS1 Taker:" in line or "✅ CMS2 Taker:" in line:
                account = "CMS1" if "CMS1" in line else "CMS2"
                pair = None
                if 'currencyPair":"' in line:
                    try:
                        start = line.find('currencyPair":"') + len('currencyPair":"')
                        end = line.find('"', start)
                        pair = line[start:end]
                    except:
                        pass
                key = f"{account}:{pair}" if pair else account
                if failure_counts.get(key, 0) > 0:
                    failure_counts[key] = 0
    
    except KeyboardInterrupt:
        log("\n👋 Monitor stopped by user")
        process.terminate()
    except Exception as e:
        log(f"❌ Monitor error: {e}")
        process.terminate()
        raise

if __name__ == "__main__":
    tail_log()
