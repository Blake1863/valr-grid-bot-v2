#!/usr/bin/env python3
"""
Inventory Manager - Manages inventory levels and rebalancing between CM1/CM2
"""
import requests
import hmac
import hashlib
import time
import json
from typing import Dict, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass

@dataclass
class Balance:
    asset: str
    available: float
    pending: float
    total: float

class InventoryManager:
    def __init__(self, cm1_key: str, cm1_secret: str, cm2_key: str, cm2_secret: str, 
                 base_url: str = "https://api.valr.com/v1"):
        self.accounts = {
            "CM1": {"key": cm1_key, "secret": cm1_secret},
            "CM2": {"key": cm2_key, "secret": cm2_secret}
        }
        self.base_url = base_url
    
    def _get_timestamp(self) -> str:
        return str(int(time.time() * 1000))
    
    def _get_signature(self, method: str, path: str, timestamp: str, body: str = "") -> str:
        """Generate HMAC signature for VALR API"""
        message = f"{method}{path}{timestamp}{body}"
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def _get_headers(self, account: str, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Get authenticated headers for a specific account"""
        acc = self.accounts[account]
        timestamp = self._get_timestamp()
        # For signature, path must include /v1 prefix
        sig_path = path if path.startswith('/v1') else f'/v1{path}'
        # VALR uses: timestamp + method + path + body, SHA512
        # Secret is used as-is (string), NOT hex-decoded
        signature = hmac.new(
            acc["secret"].encode('utf-8'),
            f"{timestamp}{method}{sig_path}{body}".encode('utf-8'),
            hashlib.sha512
        ).hexdigest()
        
        return {
            "X-VALR-API-KEY": acc["key"],
            "X-VALR-SIGNATURE": signature,
            "X-VALR-TIMESTAMP": timestamp,
            "Content-Type": "application/json"
        }
    
    def get_balances(self, account: str) -> Dict[str, Balance]:
        """Get all balances for an account"""
        try:
            # VALR endpoint: /v1/account/balances (base_url already includes /v1)
            path = "/account/balances"
            headers = self._get_headers(account, "GET", path)
            response = requests.get(f"{self.base_url}{path}", headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            balances = {}
            for bal in data:
                balances[bal["currency"]] = Balance(
                    asset=bal["currency"],
                    available=float(bal.get("available", 0)),
                    pending=float(bal.get("pending", 0)),
                    total=float(bal.get("total", 0))
                )
            
            return balances
        except Exception as e:
            print(f"[ERROR] Getting balances for {account}: {e}")
            return {}
    
    def get_asset_balance(self, account: str, asset: str) -> Optional[Balance]:
        """Get balance for a specific asset"""
        balances = self.get_balances(account)
        return balances.get(asset)
    
    def calculate_required_inventory(self, min_qty: float, cycles: int = 4, 
                                      multiplier_range: Tuple[float, float] = (1.05, 1.2)) -> float:
        """
        Calculate required inventory for N cycles
        Returns the max possible inventory needed (using max multiplier)
        """
        max_multiplier = multiplier_range[1]
        return min_qty * max_multiplier * cycles
    
    def check_inventory_sufficient(self, account: str, asset: str, 
                                    required_qty: float) -> bool:
        """Check if account has sufficient inventory for required cycles"""
        balance = self.get_asset_balance(account, asset)
        if not balance:
            return False
        return balance.available >= required_qty
    
    def internal_transfer(self, from_account: str, to_account: str, 
                          asset: str, amount: float, reason: str = "Chart maintenance rebalance") -> dict:
        """
        Transfer assets between subaccounts using VALR internal transfer
        """
        try:
            path = "/internal/transfers"
            body = json.dumps({
                "fromAccount": from_account,
                "toAccount": to_account,
                "currency": asset,
                "amount": str(amount),
                "reason": reason
            })
            
            # Use CM1 account for initiating transfer (needs internal transfer permission)
            headers = self._get_headers(from_account, "POST", path, body)
            response = requests.post(f"{self.base_url}{path}", headers=headers, json=json.loads(body), timeout=10)
            response.raise_for_status()
            
            result = response.json()
            print(f"[{datetime.now().isoformat()}] Internal transfer: {amount} {asset} from {from_account} to {to_account}")
            return {"success": True, "data": result}
        except Exception as e:
            print(f"[ERROR] Internal transfer failed: {e}")
            return {"success": False, "error": str(e)}
    
    def equalize_usdt_balances(self, min_imbalance_pct: float = 5.0) -> dict:
        """
        Equalize USDT balances between CM1 and CM2
        Triggers when imbalance exceeds min_imbalance_pct
        """
        cm1_usdt = self.get_asset_balance("CM1", "USDT")
        cm2_usdt = self.get_asset_balance("CM2", "USDT")
        
        if not cm1_usdt or not cm2_usdt:
            return {"needs_rebalance": False, "reason": "Missing balance data"}
        
        total = cm1_usdt.available + cm2_usdt.available
        if total < 1.0:  # Less than $1 total, skip
            return {"needs_rebalance": False, "reason": "Insufficient total balance"}
        
        ideal_each = total / 2
        imbalance = abs(cm1_usdt.available - cm2_usdt.available)
        imbalance_pct = (imbalance / total) * 100
        
        if imbalance_pct < min_imbalance_pct:
            return {
                "needs_rebalance": False,
                "reason": f"Imbalance ({imbalance_pct:.2f}%) below threshold ({min_imbalance_pct}%)"
            }
        
        # Calculate transfer amount to equalize
        if cm1_usdt.available > cm2_usdt.available:
            transfer_amount = (cm1_usdt.available - cm2_usdt.available) / 2
            from_acc, to_acc = "CM1", "CM2"
        else:
            transfer_amount = (cm2_usdt.available - cm1_usdt.available) / 2
            from_acc, to_acc = "CM2", "CM1"
        
        # Only transfer if meaningful amount (>0.1 USDT)
        if transfer_amount < 0.1:
            return {"needs_rebalance": False, "reason": "Transfer amount too small"}
        
        return {
            "needs_rebalance": True,
            "actions": [{
                "type": "transfer",
                "from": from_acc,
                "to": to_acc,
                "asset": "USDT",
                "amount": transfer_amount
            }],
            "cm1_usdt": cm1_usdt.available,
            "cm2_usdt": cm2_usdt.available,
            "total_usdt": total,
            "ideal_each": ideal_each,
            "imbalance_pct": imbalance_pct,
            "transfer_amount": transfer_amount
        }
    
    def rebalance_if_needed(self, pair_symbol: str, pair_info, 
                            cycles_needed: int = 4,
                            hourly_equalization: bool = True) -> dict:
        """
        Check if rebalancing is needed between CM1 and CM2 for a pair
        Returns rebalancing action if needed
        
        For perp pairs: focuses on USDT equalization (hourly)
        For spot pairs: manages base asset inventory
        """
        # Extract base and quote assets from symbol
        if pair_info.type == "futures":
            base_asset = pair_symbol.replace("USDTPERP", "").replace("USDT", "")
            quote_asset = "USDT"
        else:
            base_asset = pair_symbol.replace("USDT", "")
            quote_asset = "USDT"
        
        rebalance_actions = []
        
        # For perp pairs, prioritize hourly USDT equalization
        if hourly_equalization and pair_info.type == "futures":
            usdt_rebalance = self.equalize_usdt_balances(min_imbalance_pct=5.0)
            if usdt_rebalance["needs_rebalance"]:
                rebalance_actions.extend(usdt_rebalance["actions"])
                return {
                    "needs_rebalance": True,
                    "actions": rebalance_actions,
                    "reason": "Hourly USDT equalization"
                }
        
        # Check base asset inventory levels
        required_base = self.calculate_required_inventory(
            pair_info.min_qty, cycles_needed
        )
        
        cm1_base = self.get_asset_balance("CM1", base_asset)
        cm2_base = self.get_asset_balance("CM2", base_asset)
        cm1_usdt = self.get_asset_balance("CM1", quote_asset)
        cm2_usdt = self.get_asset_balance("CM2", quote_asset)
        
        # Check base asset balance (for spot pairs or if USDT rebalance didn't trigger)
        if cm1_base and cm2_base and not rebalance_actions:
            cm1_has_enough = cm1_base.available >= required_base
            cm2_has_enough = cm2_base.available >= required_base
            
            if not cm1_has_enough and cm2_base.available > required_base * 2:
                transfer_amount = (cm2_base.available - cm1_base.available) / 2
                rebalance_actions.append({
                    "type": "transfer",
                    "from": "CM2",
                    "to": "CM1",
                    "asset": base_asset,
                    "amount": transfer_amount
                })
            elif not cm2_has_enough and cm1_base.available > required_base * 2:
                transfer_amount = (cm1_base.available - cm2_base.available) / 2
                rebalance_actions.append({
                    "type": "transfer",
                    "from": "CM1",
                    "to": "CM2",
                    "asset": base_asset,
                    "amount": transfer_amount
                })
        
        # Emergency USDT rebalance (if one account is critically low)
        if cm1_usdt and cm2_usdt and not rebalance_actions:
            min_usdt = 50  # Emergency buffer
            if cm1_usdt.available < min_usdt and cm2_usdt.available > min_usdt * 2:
                transfer_amount = (cm2_usdt.available - cm1_usdt.available) / 2
                rebalance_actions.append({
                    "type": "transfer",
                    "from": "CM2",
                    "to": "CM1",
                    "asset": quote_asset,
                    "amount": transfer_amount
                })
            elif cm2_usdt.available < min_usdt and cm1_usdt.available > min_usdt * 2:
                transfer_amount = (cm1_usdt.available - cm2_usdt.available) / 2
                rebalance_actions.append({
                    "type": "transfer",
                    "from": "CM1",
                    "to": "CM2",
                    "asset": quote_asset,
                    "amount": transfer_amount
                })
        
        return {
            "needs_rebalance": len(rebalance_actions) > 0,
            "actions": rebalance_actions,
            "cm1_base": cm1_base.available if cm1_base else 0,
            "cm2_base": cm2_base.available if cm2_base else 0,
            "cm1_usdt": cm1_usdt.available if cm1_usdt else 0,
            "cm2_usdt": cm2_usdt.available if cm2_usdt else 0,
            "required_base": required_base
        }
    
    def execute_rebalance(self, actions: list) -> list:
        """Execute a list of rebalancing actions"""
        results = []
        for action in actions:
            result = self.internal_transfer(
                action["from"], action["to"], 
                action["asset"], action["amount"]
            )
            results.append(result)
        return results

if __name__ == "__main__":
    # Test
    import os
    from dotenv import load_dotenv
    load_dotenv("/home/admin/.openclaw/secrets/cm_secrets.env")
    
    cm1_key = os.getenv("CM1_API_KEY")
    cm1_secret = os.getenv("CM1_API_SECRET")
    cm2_key = os.getenv("CM2_API_KEY")
    cm2_secret = os.getenv("CM2_API_SECRET")
    
    manager = InventoryManager(cm1_key, cm1_secret, cm2_key, cm2_secret)
    
    print("CM1 Balances:")
    cm1_bals = manager.get_balances("CM1")
    for asset, bal in cm1_bals.items():
        print(f"  {asset}: {bal.available} available")
    
    print("\nCM2 Balances:")
    cm2_bals = manager.get_balances("CM2")
    for asset, bal in cm2_bals.items():
        print(f"  {asset}: {bal.available} available")
