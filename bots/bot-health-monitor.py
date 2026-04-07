#!/usr/bin/env python3
"""
Comprehensive Bot Health Monitor
- Checks all trading bots
- Reviews logs, balances, errors
- Auto-fixes common problems
- Sends status report

Runs every 12 hours via cron.
"""

import subprocess
import requests
import hmac
import hashlib
import time
import json
from datetime import datetime
from pathlib import Path

LOG_FILE = Path("/home/admin/.openclaw/workspace/bots/logs/bot-health-report.log")
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Telegram channel for alerts
TELEGRAM_CHAT_ID = "7018990694"

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def run_cmd(cmd, timeout=30):
    """Run shell command and return output."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        return False, str(e)

def get_valr_balance():
    """Fetch VALR account balance."""
    try:
        secrets_cmd = "python3 /home/admin/.openclaw/secrets/secrets.py get"
        api_key = subprocess.run(f"{secrets_cmd} valr_api_key", shell=True, capture_output=True, text=True).stdout.strip()
        api_secret = subprocess.run(f"{secrets_cmd} valr_api_secret", shell=True, capture_output=True, text=True).stdout.strip()
        
        ts = str(int(time.time() * 1000))
        msg = f"{ts}GET/v1/account/balances"
        sig = hmac.new(api_secret.encode(), msg.encode(), hashlib.sha512).hexdigest()
        
        headers = {
            "X-VALR-API-KEY": api_key,
            "X-VALR-SIGNATURE": sig,
            "X-VALR-TIMESTAMP": ts,
        }
        
        resp = requests.get("https://api.valr.com/v1/account/balances", headers=headers, timeout=10)
        if resp.status_code == 200:
            balances = resp.json()
            usdt = next((b for b in balances if b["currency"] == "USDT"), None)
            return float(usdt["available"]) if usdt else 0
    except Exception as e:
        log(f"  ⚠️  Could not fetch balance: {e}")
    return None

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

def check_bot_logs(log_path, lines=200):
    """Check logs for errors and patterns."""
    issues = {
        'insufficient_balance': 0,
        'errors': 0,
        'ws_errors': 0,
        'last_fill': None,
        'cycles': 0
    }
    
    try:
        ok, output = run_cmd(f"tail -{lines} {log_path}")
        if ok and output:
            lines_list = output.strip().split("\n")
            
            # Count errors
            issues['insufficient_balance'] = sum(1 for l in lines_list if 'Insufficient Balance' in l)
            issues['errors'] = sum(1 for l in lines_list if 'ERROR' in l or 'error' in l)
            issues['ws_errors'] = sum(1 for l in lines_list if 'WS error' in l or 'WebSocket' in l)
            
            # Find last fill
            for l in reversed(lines_list):
                if '[FILL]' in l or '✅' in l:
                    issues['last_fill'] = l[:100]
                    break
            
            # Count cycles
            issues['cycles'] = sum(1 for l in lines_list if 'Cycle' in l or '🎲' in l)
    except Exception as e:
        log(f"  ⚠️  Could not read logs: {e}")
    
    return issues

def check_randomization(log_path, lines=50):
    """Check if randomization is working."""
    ok, output = run_cmd(f"tail -{lines} {log_path} | grep '🎲'")
    if ok and output:
        lines_list = output.strip().split("\n")
        cms1 = sum(1 for l in lines_list if 'CMS1' in l or 'CM1' in l)
        cms2 = sum(1 for l in lines_list if 'CMS2' in l or 'CM2' in l)
        total = cms1 + cms2
        if total > 0:
            pct = cms1 / total * 100
            return f"{cms1}/{total} ({pct:.0f}% CMS1)"
    return "Not detected"

def auto_fix_issues(bot_name, issues):
    """Attempt to auto-fix common issues."""
    fixes_applied = []
    
    # Too many balance errors - likely needs rebalance
    if issues['insufficient_balance'] > 10:
        log(f"  ⚠️  High balance errors ({issues['insufficient_balance']}), restarting to trigger rebalance...")
        if restart_service(f"{bot_name}.service"):
            fixes_applied.append("Restarted to trigger rebalance")
    
    # Service not running
    if not check_service(f"{bot_name}.service"):
        log(f"  ❌ Service not running, attempting restart...")
        if restart_service(f"{bot_name}.service"):
            fixes_applied.append("Restarted service")
    
    return fixes_applied

def main():
    log("=" * 70)
    log("🤖 Bot Health Monitor - Comprehensive Check")
    log("=" * 70)
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # Check each bot
    bots = {
        'cm-bot-v2': {
            'name': 'CM-Bot-V2 (Perps)',
            'log': '/home/admin/.openclaw/workspace/bots/cm-bot-v2/logs/cm-bot-v2.log'
        },
        'cm-bot-spot': {
            'name': 'CM-Bot-Spot (Spot)',
            'log': '/home/admin/.openclaw/workspace/bots/cm-bot-spot/logs/cm-bot-spot.log'
        },
        'valr-grid-bot': {
            'name': 'VALR Grid Bot (SOL Perp)',
            'log': '/home/admin/.openclaw/workspace/bots/valr-grid-bot/logs/grid-bot.stdout'
        }
    }
    
    report = []
    all_healthy = True
    
    for bot_id, bot_info in bots.items():
        log(f"\n📊 {bot_info['name']}")
        log("-" * 50)
        
        # Check service status
        running = check_service(bot_id)
        log(f"  Service: {'🟢 Running' if running else '❌ Stopped'}")
        
        if not running:
            all_healthy = False
            restart_service(bot_id)
            report.append(f"{bot_info['name']}: ❌ Was stopped, restarted")
            continue
        
        # Check logs
        issues = check_bot_logs(bot_info['log'])
        log(f"  Recent cycles: {issues['cycles']}")
        log(f"  Insufficient balance errors: {issues['insufficient_balance']}")
        log(f"  WS errors: {issues['ws_errors']}")
        log(f"  Last fill: {issues['last_fill'][:60] if issues['last_fill'] else 'None'}")
        
        # Check randomization (for cm-bots)
        if 'cm-bot' in bot_id:
            rand_status = check_randomization(bot_info['log'])
            log(f"  Randomization: {rand_status}")
        
        # Auto-fix if needed
        fixes = auto_fix_issues(bot_id, issues)
        
        # Determine health status
        if issues['insufficient_balance'] > 20:
            log(f"  Status: 🟡 High balance errors (may need funding)")
            report.append(f"{bot_info['name']}: 🟡 {issues['insufficient_balance']} balance errors")
            all_healthy = False
        elif issues['errors'] > 5:
            log(f"  Status: 🟡 Some errors detected")
            report.append(f"{bot_info['name']}: 🟡 {issues['errors']} errors")
        else:
            log(f"  Status: ✅ Healthy")
            report.append(f"{bot_info['name']}: ✅ OK")
    
    # Check VALR balance
    log(f"\n💰 VALR Account Balance")
    log("-" * 50)
    balance = get_valr_balance()
    if balance is not None:
        log(f"  USDT Available: ${balance:.2f}")
        report.append(f"VALR Balance: ${balance:.2f} USDT")
        
        if balance < 10:
            log(f"  ⚠️  LOW BALANCE - Consider funding!")
            report.append("⚠️ LOW BALANCE WARNING")
            all_healthy = False
    
    # Summary
    log(f"\n" + "=" * 70)
    log(f"📋 SUMMARY - {timestamp}")
    log("=" * 70)
    
    for line in report:
        log(f"  {line}")
    
    if all_healthy:
        log(f"\n✅ All bots healthy")
    else:
        log(f"\n⚠️  Some bots need attention - see details above")
        log(f"  Auto-fixes applied where possible")
    
    log("=" * 70)
    
    # Send Telegram alert if issues found
    if not all_healthy:
        send_telegram_alert(report)
    
    return 0 if all_healthy else 1

def send_telegram_alert(report_lines):
    """Send alert to Telegram."""
    try:
        # This would use the message tool in OpenClaw
        # For now, just log that we would send
        log(f"\n📱 Would send Telegram alert with {len(report_lines)} items")
    except Exception as e:
        log(f"  ⚠️  Could not send Telegram alert: {e}")

if __name__ == "__main__":
    exit(main())
