#!/usr/bin/env python3
"""
Daily Bot Health Monitor

Checks all running trading bots and reports/fixes issues.
Runs once daily via cron.

DOES NOT change trading config - only restarts services or triggers rebalancing.
"""

import subprocess
import json
import time
import hmac
import hashlib
import requests
from datetime import datetime
from pathlib import Path

LOG_FILE = Path("/home/admin/.openclaw/workspace/bots/logs/bot-monitor.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def run_cmd(cmd):
    """Run shell command and return output."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        return False, str(e)

def check_service(name):
    """Check if systemd service is running."""
    ok, output = run_cmd(f"systemctl --user is-active {name}")
    return ok and "active" in output

def restart_service(name):
    """Restart a systemd service."""
    log(f"  → Restarting {name}...")
    ok, output = run_cmd(f"systemctl --user restart {name}")
    if ok:
        log(f"  ✅ {name} restarted")
        return True
    else:
        log(f"  ❌ {name} restart failed: {output[:100]}")
        return False

def check_valr_grid_bot():
    """Check VALR grid bot (SOLUSDTPERP)."""
    log("\n📊 VALR Grid Bot (SOLUSDTPERP)")
    
    if not check_service("valr-grid-bot.service"):
        log("  ❌ Service not running")
        return restart_service("valr-grid-bot.service")
    
    log("  ✅ Service running")
    
    # Check logs for recent errors (last 20 lines, excluding old WS warnings)
    ok, output = run_cmd("tail -20 /home/admin/.openclaw/workspace/bots/valr-grid-bot/logs/grid-bot.stdout | grep -iE 'ERROR|fail|crash|exception' | tail -3")
    if output.strip():
        log(f"  ⚠️ Recent errors found:")
        for line in output.strip().split("\n")[:3]:
            log(f"     {line[:100]}")
        # Restart to recover
        return restart_service("valr-grid-bot.service")
    
    # Check if grid is placed
    ok, output = run_cmd("tail -50 /home/admin/.openclaw/workspace/bots/valr-grid-bot/logs/grid-bot.stdout | grep 'Grid live'")
    if not output.strip():
        log("  ⚠️ Grid not placed - restarting")
        return restart_service("valr-grid-bot.service")
    
    log("  ✅ Grid active, no errors")
    return True

def check_cm_bot_v2():
    """Check CM-Bot-V2 (perp wash trading)."""
    log("\n📊 CM-Bot-V2 (Perp Pairs)")
    
    if not check_service("cm-bot-v2.service"):
        log("  ❌ Service not running")
        return restart_service("cm-bot-v2.service")
    
    log("  ✅ Service running")
    
    # Check for balance errors
    ok, output = run_cmd("tail -200 /home/admin/.openclaw/workspace/bots/cm-bot-v2/logs/cm-bot-v2.log | grep 'Insufficient Balance' | tail -3")
    if output.strip():
        log(f"  ⚠️ Balance errors detected - needs funding:")
        for line in output.strip().split("\n")[:2]:
            log(f"     {line[:100]}")
        log("  → Skipping restart (needs manual funding)")
        return False
    
    # Check if trading
    ok, output = run_cmd("tail -50 /home/admin/.openclaw/workspace/bots/cm-bot-v2/logs/cm-bot-v2.log | grep 'Cycle recorded' | tail -1")
    if output.strip():
        log("  ✅ Trading active")
        return True
    else:
        log("  ⚠️ No recent trades - restarting")
        return restart_service("cm-bot-v2.service")

def check_cm_bot_spot():
    """Check CM-Bot-Spot (spot wash trading)."""
    log("\n📊 CM-Bot-Spot (Spot Pairs)")
    
    if not check_service("cm-bot-spot.service"):
        log("  ❌ Service not running")
        return restart_service("cm-bot-spot.service")
    
    log("  ✅ Service running")
    
    # Check for excessive errors
    ok, output = run_cmd("tail -200 /home/admin/.openclaw/workspace/bots/cm-bot-spot/logs/cm-bot-spot.log | grep -c 'Insufficient Balance'")
    if output.strip():
        count = int(output.strip())
        if count > 10:
            log(f"  ⚠️ {count} balance errors - restarting to trigger rebalance")
            return restart_service("cm-bot-spot.service")
        else:
            log(f"  ℹ️ {count} errors (acceptable)")
    
    # Check if trading
    ok, output = run_cmd("tail -50 /home/admin/.openclaw/workspace/bots/cm-bot-spot/logs/cm-bot-spot.log | grep 'Cycle recorded' | tail -1")
    if output.strip():
        log("  ✅ Trading active")
        return True
    else:
        log("  ⚠️ No recent trades - restarting")
        return restart_service("cm-bot-spot.service")

def check_bybit_grid_bot():
    """Check Bybit grid bot status."""
    log("\n📊 Bybit Grid Bot")
    
    if not check_service("bybit-grid-bot.service"):
        log("  ℹ️ Service disabled (intentional)")
        return True
    
    log("  ⚠️ Service running but should be disabled")
    run_cmd("systemctl --user stop bybit-grid-bot.service")
    log("  → Stopped (should be disabled)")
    return True

def get_valr_balance():
    """Check VALR account balance."""
    try:
        secrets_cmd = "python3 /home/admin/.openclaw/secrets/secrets.py get"
        api_key = subprocess.run(f"{secrets_cmd} valr_api_key", shell=True, capture_output=True, text=True).stdout.strip()
        api_secret = subprocess.run(f"{secrets_cmd} valr_api_secret", shell=True, capture_output=True, text=True).stdout.strip()
        
        import time
        timestamp = str(int(time.time() * 1000))
        msg = f"{timestamp}GET/v1/account/balances"
        signature = hmac.new(api_secret.encode(), msg.encode(), hashlib.sha512).hexdigest()
        
        headers = {
            "X-VALR-API-KEY": api_key,
            "X-VALR-SIGNATURE": signature,
            "X-VALR-TIMESTAMP": timestamp,
        }
        
        resp = requests.get("https://api.valr.com/v1/account/balances", headers=headers, timeout=10)
        if resp.status_code == 200:
            balances = resp.json()
            usdt = next((b for b in balances if b["currency"] == "USDT"), None)
            if usdt:
                avail = float(usdt["available"])
                log(f"\n💰 VALR Balance: ${avail:.2f} USDT")
                return avail
    except Exception as e:
        log(f"  ⚠️ Could not fetch balance: {e}")
    return None

def main():
    log("=" * 60)
    log("🤖 Daily Bot Health Monitor")
    log("=" * 60)
    
    results = {
        "valr-grid": check_valr_grid_bot(),
        "cm-bot-v2": check_cm_bot_v2(),
        "cm-bot-spot": check_cm_bot_spot(),
        "bybit-grid": check_bybit_grid_bot(),
    }
    
    # Check VALR balance
    get_valr_balance()
    
    # Summary
    log("\n" + "=" * 60)
    log("📋 Summary")
    log("=" * 60)
    
    all_ok = True
    for name, ok in results.items():
        status = "✅ OK" if ok else "❌ Issues"
        log(f"  {name}: {status}")
        if not ok:
            all_ok = False
    
    if all_ok:
        log("\n✅ All bots healthy")
    else:
        log("\n⚠️ Some bots need attention - check logs above")
    
    log("=" * 60)
    
    return 0 if all_ok else 1

if __name__ == "__main__":
    exit(main())
