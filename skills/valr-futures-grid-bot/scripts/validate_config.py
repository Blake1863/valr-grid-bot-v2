#!/usr/bin/env python3
"""
VALR Futures Grid Bot — Config Validator

Validates grid configuration against best practices:
- Spacing within 0.1–3.0%
- Stop-loss exceeds grid range
- Reasonable leverage and capital usage

Usage:
    python3 validate_config.py config/config.json
"""

import json
import sys
from pathlib import Path


def validate_config(config_path: str) -> tuple[bool, list[str]]:
    """
    Validate grid bot configuration.
    
    Returns:
        (is_valid, list of warnings/errors)
    """
    errors = []
    warnings = []
    
    # Load config
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return False, [f"❌ Config file not found: {config_path}"]
    except json.JSONDecodeError as e:
        return False, [f"❌ Invalid JSON: {e}"]
    
    # Extract parameters
    levels = cfg.get('levels', 3)
    spacing = cfg.get('spacing_pct', 0.5)
    max_loss = cfg.get('max_loss_pct', 3.0)
    leverage = cfg.get('leverage', 5)
    balance_usage = cfg.get('balance_usage_pct', 90)
    pair = cfg.get('pair', 'SOLUSDTPERP')
    
    print(f"📋 Validating config for {pair}")
    print(f"   Levels: {levels} | Spacing: {spacing}% | SL: {max_loss}%")
    print(f"   Leverage: {leverage}x | Capital: {balance_usage}%")
    print()
    
    # Validation rules
    
    # 1. Spacing must be 0.1–3.0%
    if spacing < 0.1:
        errors.append(f"❌ Spacing {spacing}% too tight (<0.1%). Fees will eat profits.")
    elif spacing > 3.0:
        errors.append(f"❌ Spacing {spacing}% too wide (>3.0%). Few fills, low efficiency.")
    elif spacing < 0.3:
        warnings.append(f"⚠️  Spacing {spacing}% is very tight. Ensure fees <0.1%.")
    elif spacing > 2.0:
        warnings.append(f"⚠️  Spacing {spacing}% is wide. Expect fewer fills.")
    
    # 2. Stop-loss must exceed grid range
    grid_range = levels * spacing
    if max_loss <= grid_range:
        errors.append(
            f"❌ Stop-loss {max_loss}% ≤ grid range {grid_range}% ({levels}×{spacing}%). "
            f"You'll stop out within normal grid oscillation! "
            f"Increase SL to >{grid_range + 0.5}%"
        )
    elif max_loss < grid_range * 1.5:
        warnings.append(
            f"⚠️  Stop-loss {max_loss}% is close to grid range {grid_range}%. "
            f"Consider SL >{grid_range * 1.5:.1f}% for safety margin."
        )
    
    # 3. Leverage sanity check
    if leverage > 20:
        errors.append(f"❌ Leverage {leverage}x is extremely risky. Liquidation imminent.")
    elif leverage > 10:
        warnings.append(f"⚠️  Leverage {leverage}x is high. Liquidation price is close.")
    elif leverage < 2:
        warnings.append(f"⚠️  Leverage {leverage}x is very low. Capital inefficient.")
    
    # 4. Capital usage
    if balance_usage > 95:
        warnings.append(f"⚠️  Using {balance_usage}% of balance. No buffer for volatility.")
    elif balance_usage < 50:
        warnings.append(f"⚠️  Using only {balance_usage}% of balance. Low capital efficiency.")
    
    # 5. Levels sanity check
    if levels > 15:
        warnings.append(f"⚠️  {levels} levels is a lot. Each order will be small.")
    elif levels < 2:
        errors.append(f"❌ {levels} level is not a grid. Minimum 2 levels per side.")
    
    # 6. Combined risk check
    risk_score = leverage * (grid_range / max_loss) * (balance_usage / 100)
    if risk_score > 15:
        warnings.append(
            f"⚠️  High risk configuration (score: {risk_score:.1f}). "
            f"Expected drawdown >30%. Consider reducing leverage or SL."
        )
    
    # Summary
    print("─" * 50)
    if errors:
        print("❌ VALIDATION FAILED")
        print()
        for err in errors:
            print(err)
        if warnings:
            print()
            print("Warnings:")
            for warn in warnings:
                print(warn)
        return False, errors + warnings
    
    elif warnings:
        print("✅ VALIDATION PASSED (with warnings)")
        print()
        for warn in warnings:
            print(warn)
        return True, warnings
    
    else:
        print("✅ VALIDATION PASSED")
        print()
        print("Configuration looks solid! Safe to deploy.")
        return True, []


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(__doc__)
        print("Usage: python3 validate_config.py <config.json>")
        sys.exit(1)
    
    config_path = sys.argv[1]
    is_valid, messages = validate_config(config_path)
    
    sys.exit(0 if is_valid else 1)
