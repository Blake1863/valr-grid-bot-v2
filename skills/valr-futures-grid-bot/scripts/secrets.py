#!/usr/bin/env python3
"""
VALR Futures Grid Bot - Secrets Management

Stores API credentials encrypted at rest using Fernet (AES-128-CBC).

Setup:
    1. Generate key: openssl rand -base64 32 > ~/.valr-bot/.key
    2. Initialize vault: python3 secrets.py init <api_key> <api_secret>
    3. Retrieve: python3 secrets.py get valr_api_key

Usage:
    python3 secrets.py init <api_key> <api_secret>
    python3 secrets.py get <name>
    python3 secrets.py list
"""

import sys
import os
import json
from pathlib import Path
from cryptography.fernet import Fernet

VAULT_DIR = Path.home() / ".valr-bot"
KEYFILE = VAULT_DIR / ".key"
VAULT = VAULT_DIR / "vault.json"


def ensure_vault_dir():
    """Create vault directory if it doesn't exist."""
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    VAULT_DIR.chmod(0o700)


def load_fernet():
    """Load Fernet cipher from key file."""
    if not KEYFILE.exists():
        print(f"❌ Key file not found: {KEYFILE}", file=sys.stderr)
        print("   Generate one: openssl rand -base64 32 > ~/.valr-bot/.key", file=sys.stderr)
        sys.exit(1)
    
    with open(KEYFILE, 'rb') as f:
        return Fernet(f.read())


def init_vault(api_key: str, api_secret: str):
    """Initialize vault with encrypted credentials."""
    ensure_vault_dir()
    
    if not KEYFILE.exists():
        print(f"❌ Key file not found: {KEYFILE}", file=sys.stderr)
        print("   Generate one: openssl rand -base64 32 > ~/.valr-bot/.key", file=sys.stderr)
        sys.exit(1)
    
    f = load_fernet()
    vault = {
        'valr_api_key': f.encrypt(api_key.encode()).decode(),
        'valr_api_secret': f.encrypt(api_secret.encode()).decode()
    }
    
    with open(VAULT, 'w') as vf:
        json.dump(vault, vf, indent=2)
    VAULT.chmod(0o600)
    
    print("✅ Credentials stored securely")
    print(f"   Vault: {VAULT}")
    print(f"   Key: {KEYFILE}")
    print("   Keep both files safe - losing the key means losing access!")


def get_secret(name: str) -> str:
    """Retrieve and decrypt a secret."""
    if not VAULT.exists():
        print(f"❌ Vault not found: {VAULT}", file=sys.stderr)
        print("   Initialize: python3 secrets.py init <key> <secret>", file=sys.stderr)
        sys.exit(1)
    
    f = load_fernet()
    with open(VAULT) as vf:
        vault = json.load(vf)
    
    if name not in vault:
        print(f"❌ Secret not found: {name}", file=sys.stderr)
        print(f"   Available: {', '.join(vault.keys())}", file=sys.stderr)
        sys.exit(1)
    
    return f.decrypt(vault[name].encode()).decode()


def list_secrets():
    """List available secrets (not values)."""
    if not VAULT.exists():
        print("(vault not initialized)")
        return
    
    with open(VAULT) as vf:
        vault = json.load(vf)
    
    if not vault:
        print("(vault is empty)")
        return
    
    print("Available secrets:")
    for k in sorted(vault.keys()):
        print(f"  • {k}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == 'init' and len(sys.argv) == 4:
        init_vault(sys.argv[2], sys.argv[3])
    
    elif cmd == 'get' and len(sys.argv) == 3:
        print(get_secret(sys.argv[2]))
    
    elif cmd == 'list':
        list_secrets()
    
    else:
        print(__doc__)
        sys.exit(1)
